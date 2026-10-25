# Kubernetes Job executor

The executor is a cluster-independent adapter. It accepts a scheduled-job dictionary, builds a Kubernetes `batch/v1` Job, and calls an injected `BatchV1Api`. It does not read database rows or load cluster credentials; a future dispatcher owns those boundaries.

## Request contract

Required keys:

- `job_id`: lowercase DNS-compatible identifier (1–63 characters).
- `image`: container image reference.
- `command`: non-empty list of non-empty strings, passed as the container entrypoint command.
- `namespace`: lowercase DNS-compatible Kubernetes namespace.
- `scheduled_start_utc`: optional ISO 8601 UTC string (for example, `2026-10-24T12:00:00Z`). The executor records no timing behavior; a dispatcher must wait until this time before submitting.

Example:

```python
request = {
    "job_id": "demo-001",
    "image": "busybox:1.36",
    "command": ["sh", "-c", "echo GreenScale; sleep 2"],
    "scheduled_start_utc": "2026-10-24T12:00:00Z",
    "namespace": "greenscale",
}
```

The deterministic Kubernetes Job name is `gs-<job_id>`. The manifest uses one retry, `restartPolicy: Never`, and deletes completed Jobs after one hour. The `job-id` label supports correlation with the originating scheduled job.

## Client boundary

`submit_job(batch_api, request)` calls `create_namespaced_job(namespace=..., body=...)`. `get_job_status(batch_api, name, namespace)` calls `read_namespaced_job_status(...)` and maps states as follows:

| Kubernetes state | Executor state |
| --- | --- |
| Complete condition or succeeded count | `succeeded` |
| Failed terminal condition | `failed` |
| Active count | `running` |
| No active or terminal state | `pending` |

A failed Pod counter alone remains `pending` because Kubernetes may retry the Job. The caller should map these values to the database's existing status vocabulary and persist the Kubernetes Job name; the executor intentionally does not couple to the database schema.

Inject a configured `kubernetes.client.BatchV1Api` in the runtime integration. In a Pod, the integration should load in-cluster configuration; local administrative testing can load kubeconfig. Unit tests use mocks and never contact a cluster.

## RBAC

`k8s/executor-rbac.yaml` creates the `greenscale-executor` ServiceAccount and a namespaced Role/RoleBinding. It grants only `create`, `get`, `list`, and `watch` on `batch` Jobs. It grants no cluster-wide permissions and cannot read Secrets. Apply the manifest only after confirming the target namespace and cluster context.

## Local verification

Install the Kubernetes Python client into the project environment:

```bash
python -m pip install kubernetes
python -m pytest -q tests/test_job_executor.py
```

No live Kubernetes cluster is required. An optional manual probe can use `busybox:1.36` after RBAC is applied in a suitable test namespace.
