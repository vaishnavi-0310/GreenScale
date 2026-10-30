"""Dispatch due GreenScale jobs and reconcile Kubernetes execution status."""

from __future__ import annotations

import os
import argparse
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any
from urllib.parse import urlsplit

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import Job, JobStatus
from execution.job_executor import get_job_status, submit_once
from execution.job_times import get_job_times

DEFAULT_NAMESPACE = "greenscale"
DEFAULT_IMAGE = WORKER_IMAGE
_GCS_URI_RE = re.compile(r"gs://[^\s\"'<>]+")


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


def _gcs_uri_from_description(description: str | None) -> str | None:
    if not description:
        return None
    match = _GCS_URI_RE.search(description)
    if not match:
        return None
    return match.group(0).rstrip(".,;:!?)]}")


def _worker_command(job: Job) -> list[str]:
    if job.workload_type == "sleep":
        seconds = min(max(int(job.estimated_minutes or 1), 1), 1440) * 60
        return ["python", "-m", "execution.worker", "sleep", "--seconds", str(seconds)]
    if job.workload_type == "factorial":
        input_size_mb = min(max(int(job.input_size_mb or 1), 1), 500)
        return ["python", "-m", "execution.worker", "factorial", "--input-size-mb", str(input_size_mb)]
    if job.workload_type == "csv_summary":
        uri = _gcs_uri_from_description(job.description)
        if not uri:
            raise ValueError("csv_summary requires a gs://bucket/object URI in the job description")
        parsed = urlsplit(uri)
        if parsed.scheme != "gs" or not parsed.netloc or not parsed.path.lstrip("/"):
            raise ValueError("csv_summary input must be a gs://bucket/object URI")
        max_size_mb = min(max(int(job.input_size_mb or 500), 1), 500)
        return [
            "python", "-m", "execution.worker", "csv-summary",
            "--gcs-uri", uri, "--max-size-mb", str(max_size_mb),
        ]
    raise ValueError(f"Unsupported workload_type: {job.workload_type!r}")


def job_to_executor_request(
    job: Job,
    *,
    image: str | None = None,
    namespace: str = DEFAULT_NAMESPACE,
) -> dict[str, Any]:
    """Translate stored workload metadata into an allowlisted worker command."""
    if job.scheduled_for is None:
        raise ValueError(f"Job {job.id} has no scheduled_for timestamp")
    scheduled = job.scheduled_for
    if scheduled.tzinfo is None:
        scheduled = scheduled.replace(tzinfo=timezone.utc)
    scheduled = scheduled.astimezone(timezone.utc)

    if job.workload_type is None:
        if job.description:
            raise ValueError("job description did not resolve to a supported workload type")
        # Preserve old probe behavior only for legacy records without descriptions.
        selected_image = image or LEGACY_IMAGE
        command = ["echo", f"GreenScale legacy job {job.id}: {job.name}"]
    else:
        selected_image = image or DEFAULT_IMAGE
        command = _worker_command(job)
    return {
        "job_id": str(job.id),
        "image": selected_image,
        "command": command,
        "scheduled_start_utc": scheduled.isoformat().replace("+00:00", "Z"),
        "namespace": namespace,
    }


def _reconcile_running_jobs(
    db: Session, batch_api: Any, *, now: datetime, namespace: str, limit: int
) -> list[DispatchResult]:
    statement = (
        select(Job)
        .where(Job.status == JobStatus.RUNNING, Job.k8s_job_name.is_not(None))
        .order_by(Job.id)
        .limit(limit)
        .with_for_update(skip_locked=True)
    )
    jobs = list(db.scalars(statement).all())
    references = [(job.id, job.k8s_job_name) for job in jobs]
    db.commit()

    results: list[DispatchResult] = []
    for job_id, job_name in references:
        try:
            executor_status = get_job_status(batch_api, job_name, namespace)
        except Exception as exc:
            db.rollback()
            results.append(DispatchResult(job_id, "status_check_error", type(exc).__name__))
            continue

        job = db.get(Job, job_id)
        if job is None:
            results.append(DispatchResult(job_id, "record_missing"))
            continue
        if executor_status == "succeeded":
            job.status = JobStatus.SUCCEEDED
            job.finished_at = job.finished_at or now
        elif executor_status == "failed":
            job.status = JobStatus.FAILED
            job.finished_at = job.finished_at or now
        elif executor_status == "running" and job.started_at is None:
            job.started_at = now
        db.commit()
        results.append(DispatchResult(job_id, f"status_{executor_status}"))
    return results


def dispatch_due_jobs(
    db: Session,
    batch_api: Any,
    *,
    now: datetime | None = None,
    namespace: str = DEFAULT_NAMESPACE,
    image: str | None = None,
    limit: int = 50,
    job_id: int | None = None,
    wait_seconds: float = 0,
    poll_interval: float = 1.0,
    sleep: Any = time.sleep,
) -> list[DispatchResult]:
    """Reconcile running jobs and submit each due waiting job once."""
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
        try:
            request = job_to_executor_request(job, image=image, namespace=namespace)
        except ValueError as exc:
            job.status = JobStatus.FAILED
            job.finished_at = now
            job.decision_reason = (
                f"{job.decision_reason or 'Scheduled'}; execution configuration failed: {exc}"
            )
            results.append(DispatchResult(job.id, "configuration_error", "ValueError"))
            continue
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
                job.status = JobStatus.RUNNING
                if executor_status == "running" and job.started_at is None:
                    job.started_at = now
            db.commit()
            results.append(DispatchResult(claimed_id, outcome))
        except Exception as exc:
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
    """Load Kubernetes credentials and return a BatchV1Api client."""
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
