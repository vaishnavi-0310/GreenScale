"""Build and submit GreenScale batch Jobs through an injected Kubernetes API."""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from typing import Any

_JOB_ID_RE = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$")
_IMAGE_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/:@-]*$")


def _validate_request(request: Mapping[str, Any]) -> None:
    """Reject malformed or unsafe values before building a Kubernetes object."""
    required = {"job_id", "image", "command", "namespace"}
    missing = required - request.keys()
    if missing:
        raise ValueError(f"Missing required job fields: {', '.join(sorted(missing))}")

    job_id = request["job_id"]
    if (not isinstance(job_id, str) or len(job_id) > 60
            or not _JOB_ID_RE.fullmatch(job_id)):
        # The gs- prefix must still fit Kubernetes' 63-character Job name limit.
        raise ValueError("job_id must be a lowercase DNS-compatible name (max 60 chars)")

    namespace = request["namespace"]
    if not isinstance(namespace, str) or not _JOB_ID_RE.fullmatch(namespace):
        raise ValueError("namespace must be a lowercase DNS-compatible name")

    image = request["image"]
    if not isinstance(image, str) or not _IMAGE_RE.fullmatch(image):
        raise ValueError("image must be a valid image reference without whitespace")

    command = request["command"]
    if (not isinstance(command, Sequence) or isinstance(command, (str, bytes))
            or not command or any(not isinstance(part, str) or not part for part in command)):
        raise ValueError("command must be a non-empty list of non-empty strings")


def build_job_spec(request: Mapping[str, Any]) -> dict[str, Any]:
    """Return a Kubernetes batch/v1 Job manifest without contacting a cluster."""
    _validate_request(request)
    job_id = request["job_id"]
    return {
        "apiVersion": "batch/v1",
        "kind": "Job",
        "metadata": {
            "name": f"gs-{job_id}",
            "labels": {"app": "greenscale", "job-id": job_id},
        },
        "spec": {
            "backoffLimit": 1,
            "ttlSecondsAfterFinished": 3600,
            "template": {
                "metadata": {"labels": {"app": "greenscale", "job-id": job_id}},
                "spec": {
                    "restartPolicy": "Never",
                    "containers": [{
                        "name": "worker",
                        "image": request["image"],
                        "command": list(request["command"]),
                    }],
                },
            },
        },
    }


def submit_job(batch_api: Any, request: Mapping[str, Any]) -> Any:
    """Create the deterministic Kubernetes Job using an injected BatchV1Api."""
    manifest = build_job_spec(request)
    return batch_api.create_namespaced_job(
        namespace=request["namespace"], body=manifest
    )


def get_job_status(batch_api: Any, name: str, namespace: str) -> str:
    """Map a Kubernetes Job status to GreenScale's executor state vocabulary."""
    job = batch_api.read_namespaced_job_status(name=name, namespace=namespace)
    status = job.status
    conditions = getattr(status, "conditions", None) or []
    if any(condition.type == "Complete" and condition.status == "True"
           for condition in conditions):
        return "succeeded"
    if any(condition.type == "Failed" and condition.status == "True"
           for condition in conditions):
        return "failed"
    if (getattr(status, "succeeded", 0) or 0) > 0:
        return "succeeded"
    if (getattr(status, "active", 0) or 0) > 0:
        return "running"
    # A failed Pod counter alone can be transient while Job retries remain.
    return "pending"
