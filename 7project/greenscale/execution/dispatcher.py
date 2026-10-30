"""One-shot dispatcher that claims due GreenScale jobs and submits Kubernetes Jobs."""

from __future__ import annotations

import os
import argparse
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import Job, JobStatus
from execution.job_executor import get_job_status, submit_once

DEFAULT_NAMESPACE = "greenscale"
DEFAULT_IMAGE = "busybox:1.36"


@dataclass(frozen=True)
class DispatchResult:
    job_id: int
    outcome: str
    error_type: str | None = None


def job_to_executor_request(
    job: Job,
    *,
    image: str = DEFAULT_IMAGE,
    namespace: str = DEFAULT_NAMESPACE,
) -> dict[str, Any]:
    """Adapt a due database job to the executor's independent request contract.

    This integration stage submits a harmless echo workload as an execution
    probe. Replace its image/command mapping when a real workload runner exists.
    """
    if job.scheduled_for is None:
        raise ValueError(f"Job {job.id} has no scheduled_for timestamp")
    scheduled = job.scheduled_for
    if scheduled.tzinfo is None:
        scheduled = scheduled.replace(tzinfo=timezone.utc)
    scheduled = scheduled.astimezone(timezone.utc)
    return {
        "job_id": str(job.id),
        "image": image,
        "command": ["echo", f"GreenScale job {job.id}: {job.name}"],
        "scheduled_start_utc": scheduled.isoformat().replace("+00:00", "Z"),
        "namespace": namespace,
    }


def dispatch_due_jobs(
    db: Session,
    batch_api: Any,
    *,
    now: datetime | None = None,
    namespace: str = DEFAULT_NAMESPACE,
    image: str = DEFAULT_IMAGE,
    limit: int = 50,
    job_id: int | None = None,
) -> list[DispatchResult]:
    """Atomically claim due waiting jobs, submit each once, and persist status.

    `FOR UPDATE SKIP LOCKED` prevents competing dispatcher processes from
    claiming the same rows. Deterministic Job names make retry after uncertain
    API errors idempotent (the retry receives an HTTP 409 and checks that Job).
    """
    if limit < 1:
        raise ValueError("limit must be positive")
    now = now or datetime.now(timezone.utc)
    if now.tzinfo is None:
        raise ValueError("now must be timezone-aware")
    now = now.astimezone(timezone.utc)

    conditions = [
        Job.status == JobStatus.WAITING,
        Job.scheduled_for.is_not(None),
        Job.scheduled_for <= now,
    ]
    if job_id is not None:
        conditions.append(Job.id == job_id)

    statement = (
        select(Job)
        .where(*conditions)
        .order_by(Job.scheduled_for, Job.id)
        .limit(limit)
        .with_for_update(skip_locked=True)
    )
    claimed = list(db.scalars(statement).all())
    requests: list[tuple[int, str, dict[str, Any]]] = []
    for job in claimed:
        job_name = f"gs-{job.id}"
        request = job_to_executor_request(job, image=image, namespace=namespace)
        # Claim before the external API call so concurrent dispatchers skip it.
        job.status = JobStatus.RUNNING
        job.k8s_job_name = job_name
        requests.append((job.id, job_name, request))
    db.commit()

    results: list[DispatchResult] = []
    for job_id, job_name, request in requests:
        try:
            outcome = submit_once(batch_api, request)
            executor_status = get_job_status(batch_api, job_name, namespace)
            job = db.get(Job, job_id)
            if job is None:
                results.append(DispatchResult(job_id, "submitted_job_record_missing"))
                continue
            if executor_status == "succeeded":
                job.status = JobStatus.SUCCEEDED
                job.finished_at = now
            elif executor_status == "failed":
                job.status = JobStatus.FAILED
                job.finished_at = now
            else:
                # pending means the Job exists but has not started a Pod yet.
                job.status = JobStatus.RUNNING
                if executor_status == "running" and job.started_at is None:
                    job.started_at = now
            db.commit()
            results.append(DispatchResult(job_id, outcome))
        except Exception as exc:
            # Keep the deterministic name for safe retry after an uncertain
            # create response; the next HTTP 409 will be treated idempotently.
            db.rollback()
            job = db.get(Job, job_id)
            if job is not None and job.status == JobStatus.RUNNING:
                job.status = JobStatus.WAITING
                db.commit()
            results.append(DispatchResult(job_id, "error", type(exc).__name__))

    return results


def create_batch_api(*, in_cluster: bool | None = None) -> Any:
    """Load Kubernetes credentials and return a BatchV1Api client.

    By default, in-cluster configuration is selected inside Kubernetes and
    local kubeconfig is used outside it. Tests inject a mock API directly.
    """
    from kubernetes import client, config

    if in_cluster is None:
        in_cluster = bool(os.environ.get("KUBERNETES_SERVICE_HOST"))
    if in_cluster:
        config.load_incluster_config()
    else:
        config.load_kube_config()
    return client.BatchV1Api()


def main() -> None:
    """Run one dispatch pass, optionally targeting a single job."""
    import argparse

    from app.db import SessionLocal

    parser = argparse.ArgumentParser(description="Dispatch GreenScale jobs.")
    parser.add_argument(
        "--job-id",
        type=int,
        help="Dispatch only the specified database job ID.",
    )
    args = parser.parse_args()

    batch_api = create_batch_api()
    with SessionLocal() as db:
        results = dispatch_due_jobs(db, batch_api, job_id=args.job_id)

    if not results:
        print("No due jobs found.")
        return

    for result in results:
        suffix = f" ({result.error_type})" if result.error_type else ""
        print(f"Job {result.job_id}: {result.outcome}{suffix}")

if __name__ == "__main__":
    main()
