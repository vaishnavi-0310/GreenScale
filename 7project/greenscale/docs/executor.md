# Kubernetes Job executor

The executor is a cluster-independent adapter. It accepts a scheduled-job dictionary, validates it, builds a Kubernetes `batch/v1` Job, and calls an injected `BatchV1Api`. It does not read database rows or load cluster credentials; a future dispatcher owns those boundaries.

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

The dispatcher should convert one database record into the request dictionary, parse `scheduled_start_utc` as an aware UTC datetime, and wait until `UTC now >= scheduled_start_utc` before submission. It should atomically claim a due database job before calling the executor; deterministic Job names make Kubernetes creation retries idempotent, but do not replace an atomic claim when multiple dispatcher replicas are running. The executor itself does not sleep, claim records, or query PostgreSQL.

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

Inject a configured `kubernetes.client.BatchV1Api` in the runtime integration. In a Pod, the integration should load in-cluster configuration; local administrative testing can load kubeconfig. Unit tests and the demonstration use mocks and never contact a cluster.

## RBAC

`k8s/executor-rbac.yaml` creates the `greenscale-executor` ServiceAccount and a namespaced Role/RoleBinding. It grants only `create`, `get`, `list`, and `watch` on `batch` Jobs. It grants no cluster-wide permissions and cannot read Secrets. Apply the manifest only after confirming the target namespace and cluster context.

## Local verification

From the `greenscale` project directory:

```bash
python -m pytest -q tests/test_job_executor.py
python -m execution.demo_executor
```

The demo prints a deterministic mocked lifecycle, for example:

```text
Submission: submitted
Status: running
Status: succeeded
```

No live Kubernetes cluster is required.
