"""Read the start and finish times Kubernetes recorded for a Job."""

from __future__ import annotations

from datetime import datetime
from typing import Any


def _as_datetime(value: Any) -> datetime | None:
    return value if isinstance(value, datetime) else None


def get_job_times(
    batch_api: Any, name: str, namespace: str
) -> tuple[datetime | None, datetime | None]:
    """Return (started_at, finished_at) as recorded by Kubernetes for a Job.

    Uses read_namespaced_job, which the executor ServiceAccount may already call,
    so no extra RBAC permission is needed. Either value may be None, for example
    while the Job is still running.
    """
    job = batch_api.read_namespaced_job(name=name, namespace=namespace)
    status = getattr(job, "status", None)
    started = _as_datetime(getattr(status, "start_time", None))
    finished = _as_datetime(getattr(status, "completion_time", None))
    if finished is None:
        # A failed Job has no completionTime, so use when the Failed condition began.
        for condition in getattr(status, "conditions", None) or []:
            if condition.type == "Failed" and condition.status == "True":
                finished = _as_datetime(getattr(condition, "last_transition_time", None))
    return started, finished
