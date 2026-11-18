# Kubernetes Job executor

The executor accepts a scheduled-job dictionary, validates it, builds a Kubernetes `batch/v1` Job, and calls an injected `BatchV1Api`. `execution/dispatcher.py` is the thin integration layer: it selects due GreenScale database jobs, atomically claims them, adapts each row to the executor contract, submits once, and persists the Kubernetes job name and status.

## Request contract

Required keys:

- `job_id`: lowercase DNS-compatible identifier, 1–52 characters. The `gs-` prefix keeps the generated Job name within Kubernetes' name limit.
- `image`: currently allowlisted to `busybox:1.36` for the isolated prototype.
- `command`: a list with `echo` as its first item and at least one argument. It accepts at most 16 arguments, 1,024 characters per argument, and 4,096 characters in total. Shell commands and other executables are rejected.
- `namespace`: lowercase DNS-compatible Kubernetes namespace, 1–63 characters.
- `scheduled_start_utc`: optional ISO 8601 string with an explicit UTC timezone, such as `2026-10-24T12:00:00Z` or `2026-10-24T12:00:00+00:00`.

Example:

```python
request = {
    "job_id": "demo-001",
    "image": "busybox:1.36",
    "command": ["echo", "GreenScale"],
    "scheduled_start_utc": "2026-10-24T12:00:00Z",
    "namespace": "greenscale",
}
```

The current image and command allowlists intentionally keep the prototype narrow. Extend them only after agreeing on permitted workload images and command policies.

The deterministic Kubernetes Job name is `gs-<job_id>`. The manifest uses one retry, `restartPolicy: Never`, and deletes completed Jobs after one hour. The `job-id` label supports correlation with the originating scheduled job.

## Dispatcher boundary and timing

`dispatch_due_jobs(db, batch_api, now=...)` selects rows with database status `waiting` and `scheduled_for <= now` in UTC. It locks rows with `FOR UPDATE SKIP LOCKED`, marks them `running`, and stores the deterministic Kubernetes Job name before making the API call. This prevents concurrent dispatcher instances from claiming the same row. If submission errors, the row returns to `waiting`; a retry uses the same Job name, so an uncertain create response resolves as `already_exists` if Kubernetes accepted the first request. The executor itself does not sleep. Run `python -m execution.dispatcher` as a one-shot command on a schedule (for example, from an approved CronJob) to check for newly due jobs.

The database adapter currently submits an allowlisted `busybox:1.36` echo probe using the database job ID and name. It proves the scheduling-to-Kubernetes lifecycle but does not execute the user's workload. Replace that mapping with an approved worker image and command when a workload runner is available.

## Client methods, outcomes, and errors

`submit_job(batch_api, request)` calls `create_namespaced_job(namespace=..., body=...)` and returns the Kubernetes API response. `submit_once(batch_api, request)` calls the same API but returns `submitted` on success and `already_exists` when Kubernetes returns HTTP 409 for the deterministic Job name. Other API errors, including authorization and connection errors, are re-raised for the caller's retry/error policy. Request validation errors raise `ValueError` before any Kubernetes API call.

`get_job_status(batch_api, name, namespace)` calls `read_namespaced_job_status(...)` and maps states as follows:

| Kubernetes state | Executor state | GreenScale database status |
| --- | --- | --- |
| Job created, not yet active | `pending` | `running` |
| Active Pod | `running` | `running` |
| Complete condition or succeeded count | `succeeded` | `succeeded` |
| Failed terminal condition | `failed` | `failed` |

A failed Pod counter alone does not mean the Job is terminally failed: Kubernetes may retry it. `is_terminal_failed(job)` checks the Job's `Failed=True` condition. The caller should persist the deterministic Kubernetes Job name. GreenScale's `waiting` status remains the dispatcher's pre-submission state while a job waits for its scheduled window.

`create_batch_api()` loads in-cluster configuration when `KUBERNETES_SERVICE_HOST` is present and local kubeconfig otherwise. It returns a `kubernetes.client.BatchV1Api`; functions still accept an injected API so tests use mocks and never contact a cluster. The Dockerfile includes the `execution` package so the deployment image can run the one-shot dispatcher.

## RBAC

`k8s/executor-rbac.yaml` creates the `greenscale-executor` ServiceAccount and a namespaced Role/RoleBinding. The binding grants the Job-only Role to that executor account and the existing `greenscale-api` account used by Member C's Deployment. This lets the API create and inspect Jobs while preserving the API account's Workload Identity configuration. Permissions are limited to `create`, `get`, `list`, and `watch` on `batch` Jobs; there is no cluster-wide access or Secret read permission. Apply only after confirming the target namespace and cluster context.

## Local verification

From the `greenscale` project directory:

```bash
python -m pytest -q tests/test_job_executor.py
python -m execution.demo_executor
python -m pytest -q tests/test_dispatcher.py
```

The demo prints a deterministic mocked lifecycle, for example:

```text
Submission: submitted
Status: running
Status: succeeded
```

No live Kubernetes cluster is required.
