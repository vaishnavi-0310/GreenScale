# GreenScale observability and troubleshooting

Scope: health checks, logs, CI and Kubernetes troubleshooting for the GreenScale API.
Owner: Member A. Status of each section is marked **Verified locally** or **Reference (not yet verified on GKE)**.

## 1. Health and readiness endpoints (Verified locally)

| Endpoint | Question it answers | Touches the database? | Kubernetes use |
|---|---|---|---|
| `GET /health` | Is the process alive? | No | liveness probe |
| `GET /ready` | Can the app serve traffic? | Yes (`SELECT 1`) | readiness probe |

`/health` never touches the database on purpose. If it did, a database outage would make Kubernetes restart a healthy app.
`/ready` returns 503 when the database is unreachable and never includes error details in the response, because connection errors can contain hostnames or credentials.

Suggested probes for the deployment manifest:

```yaml
livenessProbe:
  httpGet: { path: /health, port: 8000 }
  initialDelaySeconds: 10
  periodSeconds: 10
readinessProbe:
  httpGet: { path: /ready, port: 8000 }
  initialDelaySeconds: 5
  periodSeconds: 5
```

### Evidence: readiness follows the database (28 Oct 2026)

Database running:

```
$ curl -i localhost:8000/ready
HTTP/1.1 200 OK
{"status":"ready","database":"up"}
```

After `docker compose stop db`:

```
$ curl -i localhost:8000/ready
HTTP/1.1 503 Service Unavailable
{"status":"unavailable","database":"down"}

$ curl -i localhost:8000/health
HTTP/1.1 200 OK
{"status":"ok"}
```

After `docker compose start db`:

```
$ curl -i localhost:8000/ready
HTTP/1.1 200 OK
{"status":"ready","database":"up"}
```

Result: readiness fails and recovers with the database, and liveness is unaffected.

## 2. Logs (Verified locally)

Every log line is one JSON object on stdout, written by `app/logging_config.py`.
Only an allowlist of fields is ever written, so secrets, API keys and user text cannot leak by accident.

Example:

```json
{"time": "2026-10-28T00:49:58+00:00", "level": "INFO", "logger": "greenscale", "message": "job created", "event": "job_created", "job_id": 5, "status": "pending"}
```

| Field | Meaning |
|---|---|
| `time`, `level`, `logger`, `message` | Always present |
| `event` | Machine-readable event name (table below) |
| `job_id`, `status` | Job lifecycle events |
| `method`, `path`, `status_code`, `duration_ms` | HTTP requests (query string and body are never logged) |
| `error` | Exception class name only, never the message |

| `event` | Level | Meaning |
|---|---|---|
| `http_request` | INFO | A request finished. `/health` and `/ready` are DEBUG to avoid probe noise |
| `http_error` | ERROR | A request raised an unhandled exception (HTTP 500) |
| `job_created` | INFO | A job was stored |
| `llm_unavailable` | WARNING | Gemini was busy or unreachable after retries (API returned 503) |
| `llm_rejected` | WARNING | Gemini answered but the output failed validation (API returned 502) |
| `readiness_failed` | ERROR | `/ready` could not reach the database |

Change the level with the `LOG_LEVEL` environment variable (`DEBUG`, `INFO`, `WARNING`).

### Reading logs

```bash
# Docker Compose
docker compose logs app --tail=100

# Kubernetes (Reference)
kubectl logs deployment/<deployment-name> -n greenscale --tail=100

# All lines for one job (works without jq)
docker compose logs app | grep '"job_id": 7'

# Only errors, if jq is installed
docker compose logs app --no-log-prefix | jq -c 'select(.level=="ERROR")'
```

## 3. HTTP status codes and what they mean

| Code | Where | Meaning | First thing to check |
|---|---|---|---|
| 201 | `POST /jobs`, `POST /jobs/from-text` | Job stored | - |
| 404 | `GET /jobs/{id}` | No such job | The id |
| 422 | `POST` endpoints | Input failed validation | Response body names the field |
| 502 | `POST /jobs/from-text` | LLM output unusable | `llm_rejected` in logs |
| 503 | `POST /jobs/from-text` | LLM busy or timed out | `llm_unavailable` in logs. Retry later |
| 503 | `GET /ready` | Database unreachable | Database container or pod |
| 500 | any | Unhandled bug | `http_error` in logs |

Note that 503 means two different things depending on the endpoint.

## 4. CI (Verified on GitHub)

Workflow: `GreenScale CI` (`.github/workflows/greenscale-ci.yml`), triggered on every push and pull request.

| Job | What it does |
|---|---|
| `tests` | Installs dependencies and runs `python -m pytest -q` |
| `docker-build` | Builds the image. Runs only if `tests` passed |

CI is fully offline: it needs no Gemini key, PostgreSQL, electricity API or cluster, because those are mocked or replaced by in-memory SQLite.

Reading a failed run: open the run in the Actions tab, click the failed job, expand the red step and look for lines starting with `FAILED` or `ERROR`.

| Symptom in the log | Likely cause | Fix |
|---|---|---|
| `ModuleNotFoundError: app` | `pytest.ini` missing or wrong working directory | Check `working-directory` and that `pytest.ini` is committed |
| `ModuleNotFoundError: <package>` | Dependency not in `requirements.txt` | Add it, pinned |
| A test that passed yesterday fails today | Hardcoded date in a test | Use dates relative to `now` |
| `docker-build` skipped | `tests` failed | Fix tests first |

Evidence: `<paste screenshot link or run URL of a green run>`

## 5. Known failure behaviour

**App exits at startup if the database is down.** `create_all` runs in the startup handler, so an unreachable database stops the process with `OperationalError ... Connection refused`.
On Kubernetes this appears as `CrashLoopBackOff` until the database is reachable.
Mitigations: start the database first, or add a startup retry.
Observed locally on 28 Oct 2026 when the app was started with the database stopped.

## 6. Kubernetes troubleshooting (Reference, not yet verified on GKE)

```bash
kubectl get pods -n greenscale
kubectl describe pod <pod-name> -n greenscale     # read the Events section at the bottom
kubectl logs <pod-name> -n greenscale
kubectl logs <pod-name> -n greenscale --previous  # logs of the crashed container
kubectl get events -n greenscale --sort-by=.lastTimestamp
```

| Pod state | Meaning | Common causes |
|---|---|---|
| `Pending` | Not scheduled yet | Not enough CPU or memory, unbound PersistentVolumeClaim |
| `ContainerCreating` | Starting up | Image still pulling, missing Secret or ConfigMap |
| `ImagePullBackOff` / `ErrImagePull` | Cannot download the image | Wrong image name or tag, private registry without an `imagePullSecret`, registry rate limit |
| `CrashLoopBackOff` | Container keeps exiting and restarting | App error at startup (for example database unreachable), missing environment variable, wrong command, out of memory (exit code 137), failing liveness probe |
| `Running` but `0/1` Ready | Readiness probe failing | `/ready` returns 503, so check the database |
| `Completed` | A Kubernetes Job pod finished successfully | - |
| `Error` | A Job pod exited with a non-zero code | `kubectl logs <pod>` |

How to tell these apart: `describe pod` shows pull errors under Events (`ImagePullBackOff`), while `logs --previous` shows the actual exception for `CrashLoopBackOff`.

### Kubernetes Jobs

```bash
kubectl get jobs -n greenscale
kubectl describe job <job-name> -n greenscale
kubectl logs job/<job-name> -n greenscale
```

| Job state | Meaning |
|---|---|
| `active` > 0 | A pod is running |
| `succeeded` = 1 | Finished successfully |
| `failed` > 0 | A pod failed. The Job retries until `backoffLimit` |

`Forbidden` when creating a Job means missing RBAC permissions:

```bash
kubectl auth can-i create jobs -n greenscale --as=system:serviceaccount:greenscale:<service-account>
```

### GreenScale database status versus Kubernetes state

| Database `status` | Meaning |
|---|---|
| `pending` | Stored, not planned yet (or waiting for prices) |
| `waiting` | Planned, has `scheduled_for` and `decision_reason` |
| `running` | A Kubernetes Job was created |
| `succeeded` / `failed` | Finished |

Kubernetes is the source of live execution status, and PostgreSQL stores the history.