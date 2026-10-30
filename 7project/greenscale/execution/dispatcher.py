"""One-shot dispatcher that claims due GreenScale jobs and submits Kubernetes Jobs."""

from __future__ import annotations

import os
import argparse
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import Job, JobStatus
from execution.job_executor import get_job_status, submit_once
from execution.job_times import get_job_times

DEFAULT_NAMESPACE = "greenscale"
DEFAULT_IMAGE = "busybox:1.36"


@dataclass(frozen=True)
class DispatchResult:
    job_id: int
    outcome: str
    error_type: str | None = None
    error_detail: str | None = None


def describe_error(exc: Exception) -> str | None:
    """Short description of a Kubernetes API error: HTTP status, reason and message."""
    status = getattr(exc, "status", None)
    if status is None:
        return None
    detail = f"HTTP {status} {getattr(exc, 'reason', '') or ''}".strip()
    body = getattr(exc, "body", None)
    if isinstance(body, str) and body:
        detail += f": {body[:400]}"
    return detail


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
    wait_seconds: float = 0,
    poll_interval: float = 1.0,
    sleep: Any = time.sleep,
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
    max_polls = (
        int(wait_seconds // poll_interval)
        if wait_seconds > 0 and poll_interval > 0
        else 0
    )
    for claimed_id, job_name, request in requests:
        try:
            outcome = submit_once(batch_api, request)
            executor_status = get_job_status(batch_api, job_name, namespace)
            polls = 0
            while executor_status not in {"succeeded", "failed"} and polls < max_polls:
                sleep(poll_interval)
                executor_status = get_job_status(batch_api, job_name, namespace)
                polls += 1
            job = db.get(Job, claimed_id)
            if job is None:
                results.append(DispatchResult(claimed_id, "submitted_job_record_missing"))
                continue
            # Prefer the times Kubernetes recorded. Reading them is best effort:
            # a failure here must never undo the status update.
            k8s_started = k8s_finished = None
            if executor_status in {"succeeded", "failed", "running"}:
                try:
                    k8s_started, k8s_finished = get_job_times(
                        batch_api, job_name, namespace
                    )
                except Exception:
                    pass
            if k8s_started is not None and job.started_at is None:
                job.started_at = k8s_started
            if executor_status == "succeeded":
                job.status = JobStatus.SUCCEEDED
                job.finished_at = k8s_finished or now
            elif executor_status == "failed":
                job.status = JobStatus.FAILED
                job.finished_at = k8s_finished or now
            else:
                # pending means the Job exists but has not started a Pod yet.
                job.status = JobStatus.RUNNING
                if executor_status == "running" and job.started_at is None:
                    job.started_at = now
            db.commit()
            results.append(DispatchResult(claimed_id, outcome))
        except Exception as exc:
            # Keep the deterministic name for safe retry after an uncertain
            # create response; the next HTTP 409 will be treated idempotently.
            db.rollback()
            job = db.get(Job, claimed_id)
            if job is not None and job.status == JobStatus.RUNNING:
                job.status = JobStatus.WAITING
                db.commit()
            results.append(
                DispatchResult(claimed_id, "error", type(exc).__name__, describe_error(exc))
            )

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
    from app.db import SessionLocal

    parser = argparse.ArgumentParser(description="Dispatch GreenScale jobs.")
    parser.add_argument(
        "--job-id",
        type=int,
        help="Dispatch only the specified database job ID.",
    )
    parser.add_argument(
        "--wait-seconds",
        type=float,
        default=30.0,
        help="Seconds to wait for a submitted Job to finish (0 = do not wait).",
    )
    args = parser.parse_args()

    batch_api = create_batch_api()
    with SessionLocal() as db:
        results = dispatch_due_jobs(
            db, batch_api, job_id=args.job_id, wait_seconds=args.wait_seconds
        )

    if not results:
        print("No due jobs found.")
        return

    for result in results:
        suffix = f" ({result.error_type})" if result.error_type else ""
        print(f"Job {result.job_id}: {result.outcome}{suffix}")
        if result.error_detail:
            print(f"  detail: {result.error_detail}")

if __name__ == "__main__":
    main()
