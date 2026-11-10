# GreenScale — Reproducibility Guide

**DAT515 · Group 24**  
**Repository:** https://github.com/dat515-2026/Group-24  
**Project directory:** `7project/greenscale`

This guide explains how another student can run GreenScale locally, test it, and understand what is required to reproduce the Kubernetes execution workflow on GKE.

**Status:** The team has demonstrated GreenScale job execution on its GKE cluster. A complete setup from scratch in an independent cloud project has **not** been verified. Do not describe the latter as completed.

## 1. What to expect

| Setup | What it includes |
|---|---|
| Local Docker Compose | FastAPI, UI, PostgreSQL, Adminer, job submission, and API checks |
| GKE | Planner CronJob, dispatcher CronJob, Kubernetes Jobs/workers, status updates |

Local Docker Compose alone does not run the Kubernetes dispatcher and worker pipeline.

The job flow is: **submit → save in PostgreSQL → estimate runtime and select an electricity-aware time → dispatch → execute a Kubernetes Job → update status**. Workers support `sleep`, `factorial`, and `csv_summary` workloads; free-text input is not arbitrary executable Python code.

## 2. Prerequisites

**Local:** Git, Docker with Compose, a terminal, and Python 3.12 for tests. Ports 8000 and 8080 should be available; the database also publishes port 5432.

**GKE (additional):** A Google Cloud project with billing, GKE, `gcloud`, `kubectl`, Artifact Registry/Cloud Build access, IAM permissions, and a suitable budget. Cloud infrastructure can incur charges.

## 3. Run GreenScale locally

### Step 1 — Get the code

```bash
git clone https://github.com/dat515-2026/Group-24.git
cd Group-24/7project/greenscale
# Record the exact revision used:
git rev-parse HEAD
```

For a repeatable comparison, all testers should use the **same agreed commit SHA**. Do not assume that an older commit includes newer CSV or authentication features.

### Step 2 — Configure environment

Copy the template:

```bash
cp .env.example .env
```

On Windows PowerShell, use `Copy-Item .env.example .env`. Review the variables for your chosen run mode. **Never commit `.env`, credentials, or passwords.**

**Important Compose caveat:** `docker-compose.yml` mounts a local Google Application Default Credentials file from `${HOME}/.config/gcloud/application_default_credentials.json`. It may not exist on a new computer. For a local run without Vertex AI, set `USE_VERTEX_AI=false` and remove that mount using a **local-only Compose override** if necessary. The runtime predictor may then use its fallback mode.

### Step 3 — Start services

```bash
docker compose config
docker compose up -d --build
docker compose ps
```

Check:

- App: http://localhost:8000/
- Login: http://localhost:8000/login
- Swagger: http://localhost:8000/docs
- Adminer: http://localhost:8080/
- Health: `curl http://localhost:8000/health` (expected `{"status":"ok"}`)

Create an account if required by the selected version, sign in, then create and retrieve a job. Local Compose database credentials in the repository are development defaults, **not production credentials**.

### Step 4 — Run tests

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
pytest -q
```

On Windows PowerShell, activate with `.\.venv\Scripts\Activate.ps1`. Record the actual test output; don't quote a test count from an older revision.

### Step 5 — Stop services

```bash
docker compose down
```

This preserves the database volume. Do not use `down -v` unless you intend to delete local data.

## 4. Reproduce Kubernetes execution on GKE

The team's detailed commands are in **`docs/deployment-runbook.md`**. Its project IDs and image references relate to the team's environment; they must be changed before using another GCP project.

For an independent installation:

1. Create or select a GCP project with billing and the required APIs, then create a compatible GKE cluster with Workload Identity Federation.
2. Clone the same source revision used for testing. Build the application image and publish it to an Artifact Registry repository you control.
3. Update image paths, cloud project settings, and identities in the API, planner, and dispatcher manifests. Make sure the worker image includes `execution.worker`.
4. Create the `greenscale` namespace, required ServiceAccounts, database Secrets, and appropriate IAM permissions. CSV workloads additionally need suitable Cloud Storage permissions.
5. Deploy PostgreSQL PVC, Service, and Deployment. Deploy executor RBAC, FastAPI Service/Deployment, then planner and dispatcher CronJobs. Apply any required database migrations.
6. Verify that everything is running and submit a real `sleep` job to confirm scheduling, execution, and status updates.

The full command sequence for the team's existing cluster, database Secret creation, schema migrations, and rollback precautions is in the runbook. This is a **guided procedure**, not a tested one-command installation for an independent project.

## 5. Verify the workflow

After connecting to the intended cluster:

```bash
kubectl get deployments,pods,services,pvc,cronjobs -n greenscale
kubectl get jobs -n greenscale
kubectl auth can-i create jobs \
  --as=system:serviceaccount:greenscale:greenscale-executor -n greenscale
```

The permission check should return `yes`. Forward the API:

```bash
kubectl port-forward -n greenscale service/greenscale-api-service 8000:8000
```

Open the web UI or Swagger, sign in if required, and submit a supported job. For its actual database ID, inspect its Kubernetes Job:

```bash
JOB_ID=123  # Replace 123 with the actual GreenScale job ID
kubectl get job "gs-$JOB_ID" -n greenscale
kubectl logs "job/gs-$JOB_ID" -n greenscale
```

Verify the database records `SUCCEEDED` for a successful run. Check the scheduling reason and recorded start/finish times. The chosen start time can differ from actual Pod startup due to CronJob polling and Kubernetes startup delays.

### Basic test checklist

- [ ] Local API and UI load
- [ ] PostgreSQL connection works and jobs can be created/retrieved
- [ ] Automated tests run; actual results recorded
- [ ] GKE API, database, planner, and dispatcher resources are healthy
- [ ] A submitted supported job creates a Kubernetes Job and finishes
- [ ] Final GreenScale job status agrees with Kubernetes
- [ ] Image versions and any problems are recorded

## 6. Evidence and known limitations

Keep only evidence that actually exists: source commit SHA, `docker compose ps`, `pytest -q` output, `kubectl` resource status, relevant Job logs, and job/status screenshots **if saved**. Do not include secrets.

Limitations to state clearly:

- GKE manifests contain team-specific settings and image tags. Check and update all affected manifests before a new deployment.
- Cloud identity, database Secrets, container registry access, and CSV storage permissions require setup.
- The Docker Compose credentials mount can prevent a first-time local run without Google credentials.
- Scheduling time is not guaranteed to equal actual Pod start time.
- A clean installation in a new project and tested teardown/rollback are **not** claimed as complete.

For problems, start with API/dispatcher logs, Kubernetes Events, Job logs, image paths, RBAC permissions, and PostgreSQL Pod/Service/PVC. Use the troubleshooting section in `docs/deployment-runbook.md`.

**Reproducibility statement:** GreenScale provides source code, dependency files, Docker Compose configuration, Kubernetes manifests, tests, and setup instructions. Team GKE execution has been demonstrated. Independent full reproducibility should be claimed only after another environment successfully follows the procedure and records its results.
