# GreenScale — Reproducibility Guide

**DAT515 · Group 24**  
**Project:** Electricity-aware Kubernetes batch-job scheduler  
**Reference source revision:** `5797c61` (2 November 2026)  
**Status:** Team GKE execution demonstrated; independent clean-environment reproduction **not yet verified**.

> **Purpose:** Enable a new student, developer, or examiner to obtain GreenScale, set it up independently, and confirm its main features. A working link to the team's deployment demonstrates **access**, not reproducibility.

## Contents

1. [What can be reproduced?](#1-what-can-be-reproduced)
2. [Architecture](#2-architecture)
3. [Prerequisites](#3-prerequisites)
4. [Option A — Local setup](#4-option-a--local-setup)
5. [Option B — Full deployment on GKE](#5-option-b--full-deployment-on-gke)
6. [Acceptance tests](#6-acceptance-tests)
7. [Evidence and results](#7-evidence-and-results)
8. [Known limitations](#8-known-limitations)
9. [Troubleshooting and cleanup](#9-troubleshooting-and-cleanup)
10. [Reproducibility statement](#10-reproducibility-statement)

---

## 1. What can be reproduced?

| Environment | Functionality | Validation status |
|---|---|---|
| Local Docker Compose | UI, FastAPI, PostgreSQL, Swagger, job submission, basic tests | Configuration exists; independent fresh-clone test pending |
| Independent GKE cluster | Planner, dispatcher, Kubernetes workers and job status updates | Team demonstrated GKE execution; independent installation pending |
| Current team's GKE cluster | Completed `sleep` Jobs `gs-10` and `gs-11` | Demonstrated, but not a substitute for independent reproduction |

**Important:** Local Docker Compose alone does **not** reproduce Kubernetes job execution. For that, follow Option B.

## 2. Architecture

```text
User (Browser / Swagger)
          |
          v
     FastAPI + UI
          |
          v
      PostgreSQL
          |
          v
 Runtime prediction + NO2 electricity prices
          |
          v
   Planner CronJob (every 5 minutes in uploaded YAML)
          |
          v
 Dispatcher CronJob (every minute)
          |
          v
 Kubernetes Job --> Worker Pod --> Job result/status
          |                         |
          +------ PostgreSQL <------+
                    |
                    v
                GreenScale UI
```

The dispatcher runs **predefined, approved worker commands** such as `sleep`, `factorial`, and `csv_summary`; a free-text description by itself is not arbitrary executable code.

**Repository:** https://github.com/dat515-2026/Group-24  
**Project directory:** `7project/greenscale/`

Main files:

```text
greenscale/
├── Dockerfile
├── docker-compose.yml
├── requirements.txt
├── .env.example
├── app/                      # API, prediction, scheduler
├── execution/                # dispatcher, Kubernetes executor, worker
├── k8s/                      # GKE resources and CronJobs
├── tests/                    # automated tests
└── docs/deployment-runbook.md
```

## 3. Prerequisites

| Local setup | Full GKE setup (additional) |
|---|---|
| Git | GCP project with billing enabled |
| Docker and Docker Compose | `gcloud` and `kubectl` |
| Terminal / PowerShell | Permission to create GKE, IAM and registry resources |
| Python 3.12 for running tests | Cloud Build and Artifact Registry access |
| Ports `8000`, `5432` (optional `8080`) | Budget and cost controls |

**Cloud deployment can incur charges.** Use an isolated test project, not the team's active cluster, for independent reproduction.

## 4. Option A — Local setup

### Step A1 — Clone a fixed source revision

```bash
git clone https://github.com/dat515-2026/Group-24.git
cd Group-24/7project/greenscale
git checkout 5797c61
```

Using a fixed commit is more reproducible than deploying whatever happens to be on `dev` later.

### Step A2 — Configure environment

```bash
cp .env.example .env
```

On Windows PowerShell: `Copy-Item .env.example .env`.

Review `.env` before starting. For Vertex AI, your account must have suitable Google Cloud credentials and permissions. For Gemini API access, configure the relevant key. Never commit `.env`, credentials, or secret values.

**Known local setup caveat:** The uploaded `docker-compose.yml` bind-mounts `${HOME}/.config/gcloud/application_default_credentials.json` into the API container. On a fresh machine, this file may not exist and the container may fail to start. Either provide valid Application Default Credentials or, for a fallback-only local demo, remove/disable that credentials volume mount in a *local* Compose override and set `USE_VERTEX_AI=false`. Record the override as part of the reproduction steps.

### Step A3 — Start and check services

```bash
docker compose config
docker compose up -d --build
docker compose ps
curl -f http://localhost:8000/health
```

Open:

- **UI:** http://localhost:8000/
- **Swagger API:** http://localhost:8000/docs
- **Adminer (local only):** http://localhost:8080/

Expected: API healthy, PostgreSQL healthy, UI reachable. Create a job through UI/Swagger and confirm it can be retrieved from the API/database.

### Step A4 — Run automated tests

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
pytest -q
```

On Windows PowerShell, activate with `.\.venv\Scripts\Activate.ps1`. Record the **actual** test results; do not assume old test counts remain unchanged.

### Step A5 — Stop local setup

```bash
docker compose down
```

This preserves the named PostgreSQL volume (do **not** add `-v` unless deliberately discarding test data).

---

## 5. Option B — Full deployment on GKE

> This is a **guided deployment checklist**, not yet a verified one-command installer. The uploaded Kubernetes YAML contains team-specific project IDs, image tags, and GCP identity settings. Adapt these for an independent project before applying anything.

### Step B1 — Configure an independent GCP project

```bash
export PROJECT_ID="YOUR_OWN_GCP_PROJECT_ID"
export REGION="europe-north1"
export CLUSTER="greenscale-repro"

gcloud config set project "$PROJECT_ID"
gcloud services enable \
  container.googleapis.com \
  artifactregistry.googleapis.com \
  cloudbuild.googleapis.com
```

Create a compatible GKE cluster using your institution's allowed setup and budget. Then authenticate `kubectl` to **your** cluster and create the namespace:

```bash
# After creating the cluster, use --region for a regional cluster
# or --zone for a zonal cluster.
gcloud container clusters get-credentials "$CLUSTER" \
  --region "$REGION" --project "$PROJECT_ID"

kubectl create namespace greenscale
kubectl config current-context
```

If the cluster is zonal, replace `--region "$REGION"` with `--zone YOUR_ZONE`.

### Step B2 — Build an independent Docker image

From `7project/greenscale`:

```bash
gcloud artifacts repositories create greenscale \
  --repository-format=docker \
  --location="$REGION" \
  --description="GreenScale images"

export IMAGE="$REGION-docker.pkg.dev/$PROJECT_ID/greenscale/greenscale-api:repro-5797c61"
gcloud builds submit --tag "$IMAGE" .
```

If the repository already exists in your independent project, skip its creation.

**Before applying YAML:** Replace team-specific registry paths, GCP project IDs, and image tags in **all** relevant deployment/CronJob manifests. Set `GREEN_SCALE_WORKER_IMAGE` to the image actually containing `execution.worker`. Ideally record an immutable image digest after building.

### Step B3 — Configure PostgreSQL credentials

```bash
DB_PASSWORD="$(openssl rand -hex 24)"

kubectl create secret generic greenscale-postgres-secret \
  -n greenscale \
  --from-literal=POSTGRES_USER=greenscale \
  --from-literal=POSTGRES_PASSWORD="$DB_PASSWORD" \
  --from-literal=POSTGRES_DB=greenscale

kubectl create secret generic greenscale-db-secret \
  -n greenscale \
  --from-literal=database-url="postgresql+psycopg://greenscale:${DB_PASSWORD}@greenscale-postgres:5432/greenscale"
```

Do not print or commit database credentials. Check `k8s/postgres-service.yaml` for the actual service name before relying on the sample URL above.

### Step B4 — Apply resources in dependency order

```bash
kubectl apply -f k8s/postgres-pvc.yaml
kubectl apply -f k8s/postgres-service.yaml
kubectl apply -f k8s/postgres-deployment.yaml
kubectl rollout status deployment/greenscale-postgres -n greenscale --timeout=240s

kubectl apply -f k8s/executor-rbac.yaml
kubectl apply -f k8s/api-service.yaml
kubectl apply -f k8s/api-deployment.yaml
kubectl apply -f k8s/planner-cronjob.yaml
kubectl apply -f k8s/dispatcher-cronjob.yaml
```

**Before the last five commands:** Verify the `greenscale-api` ServiceAccount exists and its intended Google Cloud identity has required Vertex AI access, and confirm the chosen manifests reference the independent project and image. Configure any additional GCS permissions needed for CSV jobs. Check database table initialization/migrations in the selected code revision. See `docs/deployment-runbook.md` for project-specific configuration details.

### Step B5 — Verify all components

```bash
kubectl get deployments,pods,services,pvc,cronjobs -n greenscale
kubectl rollout status deployment/greenscale-api -n greenscale --timeout=240s
kubectl auth can-i create jobs \
  --as=system:serviceaccount:greenscale:greenscale-executor \
  -n greenscale
```

Expected permission result: `yes`.

### Step B6 — Access UI securely

```bash
kubectl port-forward -n greenscale \
  service/greenscale-api-service 8000:8000
```

From the machine running the port-forward, open http://localhost:8000/ and http://localhost:8000/docs. Do not publicly expose PostgreSQL or Adminer. Public application access requires additional HTTPS and authentication configuration.

---

## 6. Acceptance tests

Run the checks below on a **fresh environment** and write down PASS/FAIL.

| ID | Test | Expected result |
|---|---|---|
| T1 | Open `GET /health` | HTTP 200 |
| T2 | Create and retrieve a job | Same job ID and metadata |
| T3 | Submit high-priority `sleep` job | Valid `scheduled_for` recorded |
| T4 | Check dispatcher and Kubernetes | `gs-<id>` Job created |
| T5 | Inspect worker logs | Requested sleep seconds observed |
| T6 | Compare stored and GKE timestamps | Status `SUCCEEDED`; duration close to Kubernetes Job duration |
| T7 | Check prediction and price integration | Source/fallback and scheduling reason documented |
| T8 | Run `pytest -q` | Actual passing/failing counts recorded |

**Example:** Submit `Run sleep job for 75 seconds. File size 2 MB.` with priority `high` and a future deadline. Replace `11` below with the new job's numeric ID:

```bash
kubectl get job gs-11 -n greenscale
kubectl logs job/gs-11 -n greenscale --timestamps
kubectl get job gs-11 -n greenscale \
  -o jsonpath='{.status.startTime}{"\n"}{.status.completionTime}{"\n"}'
```

Use an actual newly created ID: `gs-11` is an **example**, not a claim that this ID will exist in an independent cluster.

For the `jobs` table, compare `started_at`, `finished_at`, and `scheduled_for` against Kubernetes timestamps. CronJob polling, Pod creation, and image pulling can delay the actual start. A 75-second worker sleep can correspond to a slightly longer Kubernetes Job duration.

## 7. Evidence and results

Record the results in your report or a `docs/reproduction-results.md` file.

| Evidence | What to record |
|---|---|
| Source revision | `git rev-parse HEAD` |
| Environment | OS, Docker version, Kubernetes version, cluster type, region |
| Docker image | Full image path, tag, preferably digest |
| Local services | `docker compose ps` + health response |
| Test suite | `pytest -q` output |
| GKE | `kubectl get deployments,pods,cronjobs -n greenscale` |
| Workload | Submitted request, Job ID, worker logs, `SUCCEEDED` status |
| Timing | Scheduled, Kubernetes started/completed, database started/finished |
| Any deviations | Workarounds, missing permissions, fallback mode, errors |

### Sign-off checklist

- [ ] Fresh clone at pinned commit
- [ ] Local Docker setup passes
- [ ] API and database tests pass
- [ ] Independent GKE project deployed
- [ ] Job submission and completion demonstrated
- [ ] GKE and database times compared
- [ ] Evidence saved without secrets
- [ ] Deployment steps updated after the test

## 8. Known limitations

- The uploaded manifests include **hard-coded team project IDs and older image tags**. For example, the uploaded dispatcher YAML says `sleep-fix-v1`, while the team deployed `dispatcher-timestamp-v2` to its running CronJob. Check in the exact manifest used for the final reproducibility test.
- A clean installation on a new GKE cluster has **not yet been demonstrated**.
- Local Compose relies on a Google ADC file mount that may be absent on another computer.
- The uploaded planner YAML runs every **5 minutes** and the dispatcher every **1 minute**. Scheduling a start does not guarantee immediate Pod startup.
- LLM runtime estimates may differ from explicitly requested sleep times, depending on prediction inputs and rounding.
- The dispatcher timestamp fix should be verified against a **new** completed job; old database records will not automatically change.
- GKE costs, Artifact Registry access, IAM, and storage class availability vary by project and location.

## 9. Troubleshooting and cleanup

| Problem | Check |
|---|---|
| No Kubernetes Job | CronJob health, dispatcher logs, RBAC, `scheduled_for` time |
| Pod is `ImagePullBackOff` | Image reference and registry permissions |
| Database connection fails | PostgreSQL Pod, Secret, Service and PVC |
| Job starts late | CronJob polling, image pull, Pod scheduling |
| UI duration is high | Compare DB `finished_at` and GKE `completionTime` |
| Predicted minutes look incorrect | Predictor input fields, source and rounding |

Useful diagnostic commands:

```bash
kubectl get events -n greenscale --sort-by=.lastTimestamp
kubectl get cronjobs,jobs,pods -n greenscale
kubectl describe cronjob greenscale-dispatcher -n greenscale
kubectl get pvc -n greenscale
```

Clean up unused **test** GKE resources to avoid continued billing. **Never delete the team's active database volume or cluster** during troubleshooting.

## 10. Reproducibility statement

> GreenScale provides source code, dependency specifications, Docker Compose configuration, Kubernetes manifests, automated tests, and deployment instructions. The team has demonstrated batch-job execution on its GKE cluster. Full independent reproducibility will be claimed after a fresh-clone local run and an independent GKE deployment pass the documented acceptance tests, with versions and test evidence recorded.

---

**Maintainers:** DAT515 Group 24  
**Recommended repository path:** `7project/greenscale/docs/REPRODUCIBILITY.md`  
**Last prepared:** 2 November 2026
