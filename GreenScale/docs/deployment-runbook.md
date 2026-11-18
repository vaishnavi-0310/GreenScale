# GreenScale — GKE Deployment Runbook

This guide explains how to connect to the team's existing Google Kubernetes Engine (GKE) cluster, deploy GreenScale components, and check that they work. **Run commands in Google Cloud Shell from `~/Group-24/7project/greenscale` unless stated otherwise.**

> **Important:** The project already has a working GKE environment. Do not recreate its database Secrets, delete its PersistentVolumeClaim (PVC), or apply fresh-install steps to the running cluster unnecessarily.

## 1. What GreenScale runs on GKE

| Component | Purpose | Repository file |
|---|---|---|
| FastAPI and web UI | Accepts jobs and displays their status | `k8s/api-deployment.yaml`, `k8s/api-service.yaml` |
| PostgreSQL | Stores jobs, users, and scheduling results | `k8s/postgres-deployment.yaml`, `k8s/postgres-service.yaml`, `k8s/postgres-pvc.yaml` |
| Planner CronJob | Plans pending jobs every **5 minutes** using runtime estimates and NO2 prices | `k8s/planner-cronjob.yaml` |
| Dispatcher CronJob | Checks ready jobs every **minute**, starts Kubernetes Jobs, and updates status | `k8s/dispatcher-cronjob.yaml` |
| Executor permissions | Grants required Kubernetes permissions | `k8s/executor-rbac.yaml` |
| Worker | Runs supported `sleep`, `factorial`, and `csv_summary` workloads | `execution/worker.py` |

Typical workflow: **submit job → store in PostgreSQL → estimate runtime and choose a start time → dispatch Kubernetes Job → run worker → update status**.

The team has demonstrated automatic Kubernetes Job execution on GKE. This is **not** the same as verifying a fresh installation in a new GCP project.

## 2. Existing cloud environment

| Setting | Value |
|---|---|
| Google Cloud project | `greenscale-group24-512813` |
| GKE cluster | `greenscale-cluster1` |
| Zone | `europe-north1-a` |
| Region | `europe-north1` |
| Namespace | `greenscale` |
| API Service | `greenscale-api-service` (port 8000, `ClusterIP`) |
| Database Service | `greenscale-postgres` (port 5432) |
| PostgreSQL PVC | `greenscale-postgres-pvc` (10 GiB, `standard-rwo`) |

**Prerequisites:** Access to the GCP project and cluster; `gcloud` and `kubectl` (both available in Cloud Shell); the GreenScale repository; appropriate IAM/Kubernetes permissions. GKE and persistent disks may incur costs.

## 3. Connect to the existing cluster

```bash
gcloud config set project greenscale-group24-512813
gcloud container clusters get-credentials greenscale-cluster1 \
  --zone europe-north1-a --project greenscale-group24-512813
cd ~/Group-24/7project/greenscale
kubectl config current-context
kubectl get nodes
kubectl get deployments,pods,services,pvc,cronjobs -n greenscale
```

If the repository is not yet in Cloud Shell, clone the team's repository before the `cd` command. Do not run the deployment commands from a different directory.

## 4. Fresh-install prerequisites (skip if already configured)

These steps are **only for a new installation**, not routine updates to the team's working cluster.

A new GCP environment needs a running GKE cluster, the required Google Cloud APIs, billing, a container image in Artifact Registry, and appropriate permissions. The team's manifests contain project-specific values and must be adjusted for another project.

### 4.1 Namespace, ServiceAccount, and cloud identity

```bash
kubectl create namespace greenscale
kubectl create serviceaccount greenscale-api -n greenscale
```

The API Deployment uses the Kubernetes ServiceAccount `greenscale-api`. The existing project uses GKE Workload Identity Federation to grant it Vertex AI access (`roles/aiplatform.user`). Verify the existing cluster's pool:

```bash
gcloud container clusters describe greenscale-cluster1 \
  --zone europe-north1-a \
  --format='value(workloadIdentityConfig.workloadPool)'
```

Expected in the team's project: `greenscale-group24-512813.svc.id.goog`. **A new project needs its own identity and IAM setup; copying the team's identifier will not configure access.**

### 4.2 Database Secrets

Create these **only when initializing a new database**. Never commit passwords or Secret values.

```bash
DB_PASSWORD="$(openssl rand -hex 24)"
kubectl create secret generic greenscale-postgres-secret -n greenscale \
  --from-literal=POSTGRES_USER=greenscale \
  --from-literal=POSTGRES_PASSWORD="$DB_PASSWORD" \
  --from-literal=POSTGRES_DB=greenscale
DATABASE_URL="postgresql+psycopg://greenscale:${DB_PASSWORD}@greenscale-postgres:5432/greenscale"
kubectl create secret generic greenscale-db-secret -n greenscale \
  --from-literal=database-url="$DATABASE_URL"
unset DB_PASSWORD DATABASE_URL
kubectl get secrets -n greenscale
```

Replacing a Secret **does not** change an existing PostgreSQL account's password. Do not regenerate credentials for an active database.

## 5. Deploy or update components

First check that the namespace, ServiceAccount, database Secrets, image references, and access permissions are correct. Review the image tags in the manifests before applying them: they have changed during development and **must not be assumed to match an older runbook**.

For a fresh installation, apply PostgreSQL before the API:

```bash
kubectl apply -f k8s/postgres-pvc.yaml
kubectl apply -f k8s/postgres-service.yaml
kubectl apply -f k8s/postgres-deployment.yaml
kubectl rollout status deployment/greenscale-postgres -n greenscale --timeout=180s
```

Deploy the API and automatic execution components:

```bash
kubectl apply -f k8s/executor-rbac.yaml
kubectl apply -f k8s/api-service.yaml
kubectl apply -f k8s/api-deployment.yaml
kubectl apply -f k8s/planner-cronjob.yaml
kubectl apply -f k8s/dispatcher-cronjob.yaml
kubectl rollout status deployment/greenscale-api -n greenscale --timeout=180s
```

**Existing cluster:** Apply only resources you intend to update. The PostgreSQL Deployment has one replica with `Recreate` strategy and a persistent volume; this is **not** a multi-replica PostgreSQL high-availability setup.

### Database migrations

The application can create missing tables on startup but does not automatically add columns to existing tables. When updating an existing database to the account and stored-LLM-response features, the repository provides these migrations:

```bash
kubectl exec -i -n greenscale deployment/greenscale-postgres -- \
  psql -U greenscale -d greenscale < migrations/002_add_user_auth.sql
kubectl exec -i -n greenscale deployment/greenscale-postgres -- \
  psql -U greenscale -d greenscale < migrations/003_add_llm_response.sql
```

**Run migrations only when required**, after checking which schema changes have already been applied. Preserve database data and the PVC; do not delete either to update the schema.

## 6. Verify deployment and job execution

### 6.1 Check the main resources

```bash
kubectl get deployments,pods,services,pvc,cronjobs -n greenscale
kubectl get jobs -n greenscale
kubectl get deployment greenscale-api -n greenscale \
  -o jsonpath='{.spec.template.spec.containers[0].image}{"\n"}'
```

Check that the API and PostgreSQL Pods are running, the PostgreSQL PVC is `Bound`, and both CronJobs exist. The API Deployment includes readiness and liveness probes on `/health` and CPU/memory requests and limits.

### 6.2 Access the application

```bash
kubectl port-forward -n greenscale service/greenscale-api-service 8000:8000
```

Leave that terminal open. In another terminal, test `curl http://localhost:8000/health`. Open `http://localhost:8000/login` to sign in, or `http://localhost:8000/docs` to inspect the API. The Service is `ClusterIP`, so it is not a public LoadBalancer.

### 6.3 Check an actual submitted job

Submit a supported workload through GreenScale, then replace `<ID>` below with its database job ID:

```bash
kubectl get jobs -n greenscale
JOB_ID=123  # Replace 123 with the actual GreenScale job ID
kubectl get job "gs-$JOB_ID" -n greenscale
kubectl logs "job/gs-$JOB_ID" -n greenscale
```

Expected successful path: `PENDING → WAITING → RUNNING → SUCCEEDED`. The planner chooses a time; the dispatcher creates and monitors the Kubernetes Job. Its actual Pod start may be later than `scheduled_for` due to polling and Kubernetes startup. For CSV jobs, required Google Cloud Storage access must also be configured.

## 7. Troubleshooting

```bash
kubectl logs deployment/greenscale-api -n greenscale --tail=50
kubectl logs deployment/greenscale-postgres -n greenscale --tail=50
kubectl get cronjobs,jobs,pods -n greenscale
kubectl get events -n greenscale --sort-by=.metadata.creationTimestamp
kubectl describe deployment greenscale-api -n greenscale
```

| Symptom | Check first |
|---|---|
| API not ready | API logs, `/health`, database connection and Secrets |
| Database unavailable | PostgreSQL Pod, Service, Secret, PVC |
| Job never starts | Planner/dispatcher CronJobs, scheduled time, executor RBAC |
| `ImagePullBackOff` / missing worker module | Registry access and image referenced by API, planner, dispatcher, and worker |
| Job fails | `kubectl logs job/gs-<ID>` and Job/Pod details |
| Vertex AI request fails | API ServiceAccount, Workload Identity, IAM access |

Do not share logs without checking for sensitive information. A passing `/health` means the API responds; it does not guarantee the database or the full scheduler workflow is healthy.

## 8. Recovery and cleanup

To inspect Deployment history or revert a failed API update:

```bash
kubectl rollout history deployment/greenscale-api -n greenscale
# Only if a rollback is needed:
kubectl rollout undo deployment/greenscale-api -n greenscale
kubectl rollout status deployment/greenscale-api -n greenscale
```

Rollback restores a previous **Deployment configuration**, not a database backup or schema. The rollback procedure is documented; it should not be described as tested unless actually performed.

After the project finishes, first inspect `kubectl get all,pvc -n greenscale`. Deleting the PostgreSQL PVC can permanently remove the disk and its data; **never delete it during routine troubleshooting or deployment**. Removing workloads does not necessarily stop all GCP charges. Cleanup of the working cluster must be agreed with the team.

## 9. Verified scope and limitations

- The team's GKE environment has demonstrated API, PostgreSQL persistent storage, Vertex AI integration, and automatic Kubernetes batch-job execution.
- The repository includes planner/dispatcher CronJobs, worker code, authentication, CSV APIs, tests, and deployment instructions.
- New-project setup still requires manual GCP identity, permissions, image, and Secret configuration.
- An **independent fresh deployment** and **rollback/cleanup against the working cluster** are not claimed as completed tests.
- For fresh-clone instructions and tests, see `docs/Reproducubility.md`.
