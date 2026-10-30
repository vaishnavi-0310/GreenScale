"""Build and submit validated GreenScale Kubernetes Jobs."""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from datetime import datetime, timedelta
from typing import Any

# This prototype intentionally permits only the harmless image/command used by
# the offline demo. Extend these allowlists after agreeing on workload policy.
ALLOWED_IMAGES = frozenset({"busybox:1.36"})
_JOB_ID_RE = re.compile(r"^[a-z0-9](?:[-a-z0-9]{0,50}[a-z0-9])?$")
_NAMESPACE_RE = re.compile(r"^[a-z0-9](?:[-a-z0-9]{0,61}[a-z0-9])?$")
_MAX_COMMAND_ARGS = 16
_MAX_COMMAND_ARG_LENGTH = 1024
_MAX_COMMAND_LENGTH = 4096


def _validate_request(request: Mapping[str, Any]) -> None:
    """Validate the executor contract before producing a Kubernetes object."""
    if not isinstance(request, Mapping):
        raise ValueError("request must be a mapping")
    required = {"job_id", "image", "command", "namespace"}
    missing = required - request.keys()
    if missing:
        raise ValueError(f"Missing required job fields: {', '.join(sorted(missing))}")

    job_id = request["job_id"]
    if not isinstance(job_id, str) or not _JOB_ID_RE.fullmatch(job_id):
        raise ValueError("job_id must be a lowercase DNS-compatible name (max 52 chars)")

    namespace = request["namespace"]
    if not isinstance(namespace, str) or not _NAMESPACE_RE.fullmatch(namespace):
        raise ValueError("namespace must be a lowercase DNS-compatible name (max 63 chars)")

    image = request["image"]
    if not isinstance(image, str) or image not in ALLOWED_IMAGES:
        raise ValueError(f"image is not allowed; permitted images: {', '.join(sorted(ALLOWED_IMAGES))}")

    command = request["command"]
    if (not isinstance(command, Sequence) or isinstance(command, (str, bytes))
            or not command or any(not isinstance(part, str) or not part for part in command)):
        raise ValueError("command must be a non-empty list of non-empty strings")
    if len(command) > _MAX_COMMAND_ARGS:
        raise ValueError(f"command may contain at most {_MAX_COMMAND_ARGS} arguments")
    if any(len(part) > _MAX_COMMAND_ARG_LENGTH or "\x00" in part for part in command):
        raise ValueError("command arguments exceed the allowed size or contain a null byte")
    if sum(map(len, command)) > _MAX_COMMAND_LENGTH:
        raise ValueError("command exceeds the maximum total length")
    if command[0] != "echo" or len(command) < 2:
        raise ValueError("only the allowlisted echo command with at least one argument is permitted")

    scheduled_start = request.get("scheduled_start_utc")
    if scheduled_start is not None:
        if not isinstance(scheduled_start, str):
            raise ValueError("scheduled_start_utc must be an ISO 8601 UTC string")
        try:
            parsed = datetime.fromisoformat(scheduled_start.replace("Z", "+00:00"))
        except ValueError as exc:
            raise ValueError("scheduled_start_utc must be an ISO 8601 UTC string") from exc
        if parsed.tzinfo is None or parsed.utcoffset() != timedelta(0):
            raise ValueError("scheduled_start_utc must include a UTC timezone")


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


def submit_once(batch_api: Any, request: Mapping[str, Any]) -> str:
    """Submit once; report deterministic-name conflicts as idempotent retries."""
    try:
        submit_job(batch_api, request)
    except Exception as exc:
        if getattr(exc, "status", None) == 409:
            return "already_exists"
        raise
    return "submitted"


def is_terminal_failed(job: Any) -> bool:
    """Return whether Kubernetes marked the Job itself as terminally failed."""
    status = getattr(job, "status", None)
    conditions = getattr(status, "conditions", None) or []
    return any(condition.type == "Failed" and condition.status == "True"
               for condition in conditions)


def get_job_status(batch_api: Any, name: str, namespace: str) -> str:
    """Map Kubernetes Job conditions/counters to executor lifecycle states."""
    job = batch_api.read_namespaced_job(name=name, namespace=namespace)
    status = job.status
    conditions = getattr(status, "conditions", None) or []
    if any(condition.type == "Complete" and condition.status == "True"
           for condition in conditions):
        return "succeeded"
    if is_terminal_failed(job):
        return "failed"
    if (getattr(status, "succeeded", 0) or 0) > 0:
        return "succeeded"
    if (getattr(status, "active", 0) or 0) > 0:
        return "running"
    # Failed Pods may be retried; only a terminal Job condition means failure.
    return "pending"
