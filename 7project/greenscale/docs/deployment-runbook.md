# GreenScale — GKE Deployment Runbook

## 1. Purpose

This runbook documents how to deploy, verify, and operate the GreenScale FastAPI application and PostgreSQL database on Google Kubernetes Engine (GKE).

GreenScale is a Kubernetes-based batch-job scheduling prototype that uses Norwegian electricity prices and Gemini runtime predictions to select preferred execution times.

This document covers the infrastructure currently deployed on GKE. End-to-end integration of the automatic Kubernetes Job executor must be verified separately.

## 2. Prerequisites

The following are required:

- Access to the GreenScale Google Cloud project
- Appropriate Google Cloud IAM and Kubernetes permissions
- Google Cloud CLI (`gcloud`)
- Kubernetes CLI (`kubectl`)
- Access to the GreenScale GitHub repository
- Docker, when building and publishing a new container image

Google Cloud Shell provides `gcloud` and `kubectl`.

Do not store database passwords, access tokens, or service account keys in Git.

## 3. Google Cloud Configuration

| Setting | Value |
|---|---|
| GCP Project ID | `greenscale-group24-512813` |
| GKE Cluster | `greenscale-cluster1` |
| Region | `europe-north1` |
| Zone | `europe-north1-a` |
| Namespace | `greenscale` |
| API Image | `europe-north1-docker.pkg.dev/greenscale-group24-512813/greenscale/greenscale-api:llm-fix-v1` |
| API Service | `greenscale-api-service` |
| PostgreSQL Service | `greenscale-postgres` |

## 3.1. Fresh Deployment Prerequisites

The instructions in this runbook currently target the existing GKE cluster `greenscale-cluster1`.

A fresh deployment requires the following resources and configuration.

### A. Google Cloud resources

- An active Google Cloud project with billing enabled
- Google Kubernetes Engine API enabled
- Vertex AI API enabled
- A running GKE cluster
- GKE Workload Identity Federation enabled
- Appropriate IAM permissions for deployment and Vertex AI access

The current deployment uses:

- Project: `greenscale-group24-512813`
- Cluster: `greenscale-cluster1`
- Zone: `europe-north1-a`

Cluster creation is not required when using the existing environment.

### B. Kubernetes resources

The following must exist before deploying the FastAPI application:

| Resource | Name | Purpose |
|---|---|---|
| Namespace | `greenscale` | Isolates GreenScale resources |
| ServiceAccount | `greenscale-api` | Provides the API's Kubernetes identity |
| Secret | `greenscale-postgres-secret` | PostgreSQL initialization credentials |
| Secret | `greenscale-db-secret` | FastAPI database connection string |
| PersistentVolumeClaim | `greenscale-postgres-pvc` | PostgreSQL persistent storage |
| PostgreSQL Service | `greenscale-postgres` | Internal database network address |

The namespace, ServiceAccount, and Secrets were originally created using Kubernetes commands. They are not all defined in the current version-controlled manifests.

### C. Database credentials

Database credentials must be created securely and must not be committed to GitHub.

The PostgreSQL Secret contains:

- `POSTGRES_USER`
- `POSTGRES_PASSWORD`
- `POSTGRES_DB`

The API database Secret contains:

- `database-url`

The `database-url` value uses the SQLAlchemy PostgreSQL connection format and references the Kubernetes PostgreSQL Service.

Credential creation and database deployment are covered in the following deployment procedure.

### D. Docker image

The current API image is:

`europe-north1-docker.pkg.dev/greenscale-group24-512813/greenscale/greenscale-api:llm-fix-v1`

Kubernetes pulls this image from Google Artifact Registry in europe-north1.

The image contains the FastAPI application and its Python dependencies. PostgreSQL is deployed separately.

### E. Deployment order

For a fresh installation, deploy resources in this logical order:

1. Namespace
2. Kubernetes ServiceAccount and required IAM bindings
3. Database Secrets
4. PostgreSQL PersistentVolumeClaim
5. PostgreSQL Deployment and Service
6. FastAPI Deployment and Service
7. Health checks and smoke tests

The Kubernetes Job executor and its RBAC configuration must be deployed and verified separately as part of the full batch-execution integration.

### F. Important safety precautions

- Do not recreate Secrets unnecessarily in the working cluster.
- Do not delete the PostgreSQL PVC during routine application updates.
- Do not store database passwords or API keys in Git.
- Use least-privilege IAM and Kubernetes RBAC.
- Be aware that running GKE nodes and persistent disks can generate charges.

## 3.2. Create Namespace and API ServiceAccount

**Important:** The commands in this section are intended for a fresh installation. On the existing GreenScale GKE cluster, these resources have already been created.

### A. Create the namespace

```bash
kubectl create namespace greenscale
```

This creates a dedicated Kubernetes namespace for GreenScale resources.

Verify:

```bash
kubectl get namespaces
```

Expected namespace: `greenscale`.

If the namespace already exists, do not recreate it.

### B. Create the FastAPI ServiceAccount

```bash
kubectl create serviceaccount greenscale-api -n greenscale
```

The ServiceAccount provides an identity to the FastAPI Pod.

Verify:

```bash
kubectl get serviceaccounts -n greenscale
```

Expected ServiceAccount: `greenscale-api`.

The FastAPI Deployment references this identity using:

```yaml
serviceAccountName: greenscale-api
```

### C. Configure Google Cloud Workload Identity Federation

The existing GKE cluster has Workload Identity Federation enabled.

Verify its configuration:

```bash
gcloud container clusters describe greenscale-cluster1 \
  --zone europe-north1-a \
  --format="value(workloadIdentityConfig.workloadPool)"
```

Expected:

```text
greenscale-group24-512813.svc.id.goog
```

The GreenScale API Kubernetes ServiceAccount is authorized to access Vertex AI using an IAM principal binding.

For the existing project, the IAM principal is:

```text
principal://iam.googleapis.com/projects/725624792033/locations/global/workloadIdentityPools/greenscale-group24-512813.svc.id.goog/subject/ns/greenscale/sa/greenscale-api
```

The principal has the Google Cloud IAM role `roles/aiplatform.user`.

In a fresh Google Cloud project, the project number, Workload Identity pool, and IAM permissions must be configured for that project. Do not reuse the existing project's identifiers in a different project.

### D. Verify the API identity

```bash
kubectl get serviceaccount greenscale-api -n greenscale
```

Inspect the Deployment identity after deployment:

```bash
kubectl get deployment greenscale-api -n greenscale \
  -o jsonpath='{.spec.template.spec.serviceAccountName}{"\n"}'
```

Expected:

```text
greenscale-api
```

This confirms that the Deployment is configured to use the expected Kubernetes ServiceAccount.

A successful Gemini request through Vertex AI provides additional verification that authentication and authorization work.

## 3.3. Create PostgreSQL Kubernetes Secrets

GreenScale uses Kubernetes Secrets to provide database credentials to PostgreSQL and FastAPI.

These instructions are intended for a fresh deployment. **Do not execute them against the existing GreenScale namespace**, because the database credentials and database contents must be preserved.

### A. Choose database settings

The current deployment uses:

- Username: `greenscale`
- Database name: `greenscale`
- PostgreSQL Service: `greenscale-postgres`
- Port: `5432`

Generate a strong password locally in Google Cloud Shell:

```bash
DB_PASSWORD="$(openssl rand -hex 24)"
```

This generates a random hexadecimal password and saves it temporarily in the current shell session.

Do not print, commit, or share the password.

### B. Create the PostgreSQL Secret

```bash
kubectl create secret generic greenscale-postgres-secret \
  --namespace greenscale \
  --from-literal=POSTGRES_USER=greenscale \
  --from-literal=POSTGRES_PASSWORD="$DB_PASSWORD" \
  --from-literal=POSTGRES_DB=greenscale
```

PostgreSQL reads these values through its Deployment's `envFrom` configuration.

### C. Create the FastAPI database Secret

```bash
DATABASE_URL="postgresql+psycopg://greenscale:${DB_PASSWORD}@greenscale-postgres:5432/greenscale"

kubectl create secret generic greenscale-db-secret \
  --namespace greenscale \
  --from-literal=database-url="$DATABASE_URL"
```

Both Secrets must contain the same database password.

The URL uses the `psycopg` PostgreSQL driver.

### D. Remove temporary shell variables

```bash
unset DB_PASSWORD DATABASE_URL
```

This removes the values from the current shell environment, although it does not erase all possible copies from shell history, Kubernetes state, or administrative audit logs.

### E. Verify that the Secrets exist

```bash
kubectl get secret greenscale-postgres-secret -n greenscale
kubectl get secret greenscale-db-secret -n greenscale
```

To inspect the available key names without displaying credential values:

```bash
kubectl describe secret greenscale-postgres-secret -n greenscale
kubectl describe secret greenscale-db-secret -n greenscale
```

Expected keys:

| Secret | Expected keys |
|---|---|
| `greenscale-postgres-secret` | `POSTGRES_USER`, `POSTGRES_PASSWORD`, `POSTGRES_DB` |
| `greenscale-db-secret` | `database-url` |

### F. Security precautions

- Never put real passwords in Kubernetes YAML files committed to Git.
- Never commit `.env` files containing credentials.
- Avoid displaying Secret values using `kubectl get secret -o yaml`.
- Do not recreate database Secrets when updating the FastAPI image.
- Restrict permission to read Kubernetes Secrets using RBAC.
- Kubernetes Secrets are not automatically equivalent to a dedicated encrypted secret-management system; review cluster encryption and access controls separately.

**Important:** A newly generated password is suitable for initializing a new PostgreSQL database. Replacing the Secret alone does not change the password inside an existing PostgreSQL database. Credential rotation requires a coordinated procedure.

## 3.4. Connect to GKE

Open Google Cloud Shell.

```bash
gcloud config set project greenscale-group24-512813

gcloud container clusters get-credentials greenscale-cluster1 \
  --zone europe-north1-a \
  --project greenscale-group24-512813
```

Verify connectivity:

```bash
kubectl config current-context
kubectl get nodes
kubectl get namespaces
```

These instructions connect to the existing cluster. They do not create a new one.


## 3.5. Deploy PostgreSQL and FastAPI to GKE

This procedure assumes that:

- The GKE cluster already exists and `kubectl` is connected to it.
- The `greenscale` namespace and `greenscale-api` ServiceAccount exist.
- The PostgreSQL and FastAPI database Secrets have been created.
- The API ServiceAccount has the required Vertex AI IAM permissions.
- The project repository is available locally or in Cloud Shell.

**Important:** These commands describe a fresh deployment. Do not recreate the existing PostgreSQL database or Secrets during routine application updates.

### A. Navigate to the GreenScale project

In Google Cloud Shell, from the existing repository clone:

```bash
cd ~/Group-24/7project/greenscale
```

If the repository has not yet been cloned, obtain it from the team's GitHub repository and navigate to the `7project/greenscale` directory.

### B. Deploy PostgreSQL persistent storage

```bash
kubectl apply -f k8s/postgres-pvc.yaml
```

Verify:

```bash
kubectl get pvc -n greenscale
```

Expected: `greenscale-postgres-pvc` has status `Bound`.

The PersistentVolumeClaim requests 10 GiB of storage using the `standard-rwo` StorageClass.

### C. Deploy PostgreSQL

```bash
kubectl apply -f k8s/postgres-service.yaml
kubectl apply -f k8s/postgres-deployment.yaml
```

Check:

```bash
kubectl rollout status deployment/greenscale-postgres \
  -n greenscale --timeout=180s

kubectl get pods -n greenscale
```

The PostgreSQL Pod should become `1/1 Running`.

The Service `greenscale-postgres` provides the internal hostname used by FastAPI for database connections.

### D. Deploy FastAPI

```bash
kubectl apply -f k8s/api-service.yaml
kubectl apply -f k8s/api-deployment.yaml
```

Check the rollout:

```bash
kubectl rollout status deployment/greenscale-api \
  -n greenscale --timeout=180s
```

Verify the resources:

```bash
kubectl get deployments,pods,services,pvc -n greenscale
```

Expected:

- `greenscale-api`: `1/1` available
- `greenscale-postgres`: `1/1` available
- Both application Pods: `Running`
- PostgreSQL PVC: `Bound`

### E. Verify FastAPI health

In the first Google Cloud Shell terminal, start port forwarding:

```bash
kubectl port-forward -n greenscale \
  service/greenscale-api-service 8000:8000
```

Leave this terminal open.

In a second Cloud Shell terminal, run:

```bash
curl --fail --show-error http://localhost:8000/health
```

Expected response:

```json
{"status":"ok"}
```

Stop port forwarding using `Ctrl+C` when finished.

### F. Verify the database connection

With port forwarding active, run:

```bash
curl --fail --show-error http://localhost:8000/jobs
```

A newly deployed database may return an empty list:

```json
[]
```

A populated database may return existing job records.

A successful response demonstrates that the API can serve the `/jobs` endpoint. If it returns a database-related error, inspect the API logs and database configuration.

### G. Verify the deployed image

```bash
kubectl get deployment greenscale-api -n greenscale \
  -o jsonpath='{.spec.template.spec.containers[0].image}{"\n"}'
```

Expected image for the current deployment:

```text
europe-north1-docker.pkg.dev/greenscale-group24-512813/greenscale/greenscale-api:llm-fix-v1
```

### H. Important deployment notes

- The API and PostgreSQL Services use `ClusterIP` and are not directly exposed to the public internet.
- The API uses Kubernetes readiness and liveness probes.
- Vertex AI authentication uses Workload Identity.
- PostgreSQL data is stored on a PersistentVolumeClaim.
- Do not delete or replace the PVC during a routine API update.
- The automatic Kubernetes Job executor requires separate integration verification.
- A new PostgreSQL installation must have its database schema initialized through the project's supported startup or migration mechanism before job endpoints can work reliably.

## 3.6. Database Table Initialization

GreenScale uses SQLAlchemy to initialize database tables when FastAPI starts.

The initialization code is located in `app/main.py`:

```python
async def lifespan(app: FastAPI):
    Base.metadata.create_all(bind=engine)
```

### How initialization works

1. The PostgreSQL Deployment starts and becomes available.
2. FastAPI starts and connects to PostgreSQL using `DATABASE_URL`.
3. SQLAlchemy checks the table definitions registered in `Base.metadata`.
4. Missing tables are created automatically.
5. Existing tables are preserved.

No separate manual SQL table-creation command is required for the current schema during a fresh deployment.

### Verify database initialization

After deploying FastAPI, check its logs:

```bash
kubectl logs deployment/greenscale-api -n greenscale --tail=50
```

Test the jobs endpoint through port forwarding:

```bash
curl --fail --show-error http://localhost:8000/jobs
```

An HTTP 200 response confirms that the endpoint successfully handled the request. The endpoint should be tested with PostgreSQL available.

### Important limitation

`Base.metadata.create_all()` creates missing tables but does not migrate existing database schemas.

If a future GreenScale version introduces new columns or modifies existing columns, a proper database migration must be performed.

Never delete the existing database or PersistentVolumeClaim simply to apply schema changes.


## 4. Kubernetes Infrastructure

GreenScale currently uses the following manifests:

| Manifest | Purpose |
|---|---|
| `k8s/api-deployment.yaml` | FastAPI Deployment, Vertex AI configuration, health probes |
| `k8s/api-service.yaml` | Internal API Service on port 8000 |
| `k8s/postgres-deployment.yaml` | PostgreSQL Deployment |
| `k8s/postgres-service.yaml` | Internal PostgreSQL Service on port 5432 |
| `k8s/postgres-pvc.yaml` | Persistent storage for PostgreSQL |
| `k8s/executor-rbac.yaml` | Kubernetes executor access-control resources |

The API uses the Kubernetes ServiceAccount `greenscale-api` for Google Cloud Workload Identity.

The existing GKE namespace contains two application Deployments: `greenscale-api` and `greenscale-postgres`.

## 5. Verify the Existing Deployment

```bash
kubectl get deployments,pods,services,pvc -n greenscale
```

Expected healthy conditions:

- Both Deployments show `1/1` ready.
- FastAPI and PostgreSQL Pods show `Running`.
- PostgreSQL PersistentVolumeClaim shows `Bound`.
- The API Service exposes port 8000 inside the cluster.
- The PostgreSQL Service exposes port 5432 inside the cluster.

Check the API rollout:

```bash
kubectl rollout status deployment/greenscale-api -n greenscale
```

Inspect the Deployment:

```bash
kubectl describe deployment greenscale-api -n greenscale
```

## 6. Health Monitoring

The FastAPI Deployment uses two Kubernetes health probes.

**Readiness probe**

- Endpoint: `/health`
- Port: `8000`
- Initial delay: 5 seconds
- Check interval: 10 seconds

**Liveness probe**

- Endpoint: `/health`
- Port: `8000`
- Initial delay: 15 seconds
- Check interval: 20 seconds

The health endpoint checks application responsiveness. It does not independently guarantee database availability.

## 7. Logs and Troubleshooting

View API logs:

```bash
kubectl logs deployment/greenscale-api -n greenscale --tail=50
```

View PostgreSQL logs:

```bash
kubectl logs deployment/greenscale-postgres -n greenscale --tail=50
```

Inspect recent cluster events:

```bash
kubectl get events -n greenscale \
  --sort-by=.metadata.creationTimestamp
```

Inspect Pods:

```bash
kubectl get pods -n greenscale -o wide
```

```bash
kubectl describe pods -n greenscale
```

Logs must be reviewed before sharing to ensure they do not contain credentials or sensitive information.

## 8. Vertex AI Integration

The FastAPI Deployment configures Vertex AI using:

- `USE_VERTEX_AI=true`
- `GOOGLE_CLOUD_PROJECT=greenscale-group24-512813`
- `GOOGLE_CLOUD_LOCATION=global`

The application uses Workload Identity rather than a hardcoded Gemini API key when Vertex AI mode is enabled.

A live Gemini request and GreenScale runtime prediction have both been verified from the running GKE container.

## 9. Scope and Limitations

The following have been verified:

- FastAPI runs on GKE.
- PostgreSQL runs with persistent storage.
- API-to-database communication works.
- Vertex AI provides runtime predictions.
- Kubernetes health probes are active.
- Kubernetes Services provide internal connectivity.
- A manually created Kubernetes batch Job can execute successfully.

The complete automatic scheduler-to-Kubernetes-Job execution workflow requires separate integration verification.

Deployment, rollback, cleanup, and security-review procedures will be expanded during the remaining Member C Phase 4 work.

## 10. Deployment Rollback and Recovery

Kubernetes maintains revision history for the GreenScale FastAPI Deployment. This allows the team to restore a previous Deployment configuration if an update fails.

### A. Check deployment history

```bash
kubectl rollout history deployment/greenscale-api -n greenscale
```

The deployment had three revisions during Phase 3 verification.

### B. Inspect the current deployment

```bash
kubectl get deployment greenscale-api -n greenscale

kubectl get pods -n greenscale
```

Check the currently configured Docker image:

```bash
kubectl get deployment greenscale-api -n greenscale \
  -o jsonpath='{.spec.template.spec.containers[0].image}{"\n"}'
```

### C. Roll back if necessary

If a new deployment fails, an authorized team member can run:

```bash
kubectl rollout undo deployment/greenscale-api -n greenscale
```

This restores the previous Deployment revision.

To restore a specific available revision:

```bash
kubectl rollout undo deployment/greenscale-api \
  -n greenscale --to-revision=2
```

**Warning:** These commands modify the running application. Execute them only when a rollback is necessary.

### D. Verify recovery

```bash
kubectl rollout status deployment/greenscale-api \
  -n greenscale --timeout=180s

kubectl get pods -n greenscale
```

Inspect logs if the application remains unhealthy:

```bash
kubectl logs deployment/greenscale-api -n greenscale --tail=50
```

### E. Important limitations

- Kubernetes rollback restores Deployment configuration, not database contents.
- It does not reverse PostgreSQL schema changes.
- A previous image must still be available in the container registry.
- The restored configuration must remain compatible with the current database and external services.
- Deployment revision history is limited by Kubernetes retention settings.
- After a rollback, update the repository to reflect the intended deployed configuration and document the reason.

The rollback commands have been documented but have not been executed against the working GreenScale deployment during this phase.

## 11. Kubernetes Cleanup

This section documents how to remove GreenScale resources after the project is finished.

**Warning:** Cleanup commands are destructive. Do not run them on the working GKE environment during normal development or demonstrations.

### A. Inspect resources before cleanup

```bash
kubectl config current-context
kubectl get deployments,pods,services,pvc -n greenscale
```

Confirm that the correct cluster and namespace are selected.

### B. Remove the FastAPI application

```bash
kubectl delete deployment greenscale-api -n greenscale
kubectl delete service greenscale-api-service -n greenscale
```

These commands remove the FastAPI Deployment and its Service.

They do not directly delete PostgreSQL or its persistent volume.

### C. Remove PostgreSQL compute resources

Only after confirming that the database is no longer required:

```bash
kubectl delete deployment greenscale-postgres -n greenscale
kubectl delete service greenscale-postgres -n greenscale
```

These commands stop PostgreSQL and remove its Service. The PersistentVolumeClaim remains unless deleted separately.

### D. Persistent storage cleanup

The PostgreSQL data is stored using:

`greenscale-postgres-pvc`

Deleting the PVC may also delete the underlying persistent disk, depending on the StorageClass reclaim policy.

**Do not delete the PVC unless data loss is acceptable and the team has confirmed that any required backup exists.**

For complete teardown only:

```bash
kubectl delete pvc greenscale-postgres-pvc -n greenscale
```

### E. Other resources

Review remaining Kubernetes resources:

```bash
kubectl get all -n greenscale
kubectl get pvc,secrets,serviceaccounts,roles,rolebindings -n greenscale
```

A complete teardown must also account for Kubernetes Jobs, executor RBAC, Secrets, ServiceAccounts, cluster-level resources where applicable, and Google Cloud IAM bindings created for GreenScale.

Do not remove shared cluster resources or IAM bindings without confirming ownership.

### F. Verify cleanup

```bash
kubectl get deployments,pods,services,pvc -n greenscale
```

Confirm that only the intended resources have been removed.

### G. Google Cloud cost considerations

Kubernetes resource cleanup and GKE cluster deletion are separate operations.

Deleting application Pods does not necessarily stop GKE cluster charges. Persistent disks, cluster infrastructure, networking resources, and other Google Cloud resources may continue generating costs.

After the project, review the GCP Billing console and remove resources that are no longer needed, following the team's agreed cleanup plan.

### H. Cleanup status

These commands are documented for reproducibility.

They have **not been executed against the working GreenScale environment**, because the project is still under development.

---

## Phase 3 — Automated Kubernetes Job Execution

### Deployment order

1. Configure executor permissions:
   `kubectl apply -f k8s/executor-rbac.yaml`

2. Deploy the planner:
   `kubectl apply -f k8s/planner-cronjob.yaml`

3. Deploy the dispatcher:
   `kubectl apply -f k8s/dispatcher-cronjob.yaml`

### Verify deployment

Run:
`kubectl get deployments,cronjobs,pvc -n greenscale`

The planner runs every 5 minutes.
The dispatcher runs every minute.

### Tested container images

- API: llm-fix-v1
- Planner: planner-fix-v1
- Dispatcher: dispatcher-v3
- Worker: dispatcher-v3

### End-to-end workflow

PENDING → WAITING → RUNNING → SUCCEEDED

The planner schedules jobs, the dispatcher submits them
to Kubernetes, and the dispatcher synchronizes completed
job statuses with PostgreSQL.

Do not recreate database Secrets or delete the PostgreSQL
PVC during routine deployments.