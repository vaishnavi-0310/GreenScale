
from datetime import datetime, timezone
from math import ceil

from sqlalchemy.orm import Session
from sqlalchemy import select

from app.models import Job, JobStatus
from app.predictor import predict_runtime
from app.scheduler import choose_start

def get_pending_jobs(db: Session) -> list[Job]:
    """Retrieve all pending jobs from PostgreSQL."""

    statement = (
        select(Job)
        .where(Job.status == JobStatus.PENDING)
        .order_by(Job.id)
    )

    return list(db.scalars(statement).all())

def plan_pending_job(
    db: Session,
    job_id: int,
    prices: list,
    now: datetime | None = None,
    workload_type: str | None = None,
    input_size_mb: int | None = None,
) -> Job:

    # 1. Determine the current time
    if now is None:
        now = datetime.now(timezone.utc)

    # 2. Find the existing job
    job = db.get(Job, job_id)

    if job is None:
        raise ValueError("Job not found")

    # 3. Prevent scheduling the same job twice
    if job.status != JobStatus.PENDING:
        raise ValueError("Job is not pending")

    # 4. Read workload metadata from PostgreSQL
    # Explicit arguments, when supplied, take precedence.
    if workload_type is None:
        workload_type = job.workload_type

    if input_size_mb is None:
        input_size_mb = job.input_size_mb

    # 5. Choose how to estimate runtime
    if workload_type is None:
        # Legacy jobs may not have workload metadata.
        runtime_seconds = job.estimated_minutes * 60
        runtime_source = "stored_estimate"

    else:
        # Workloads without input data default to 0 MB.
        if input_size_mb is None:
            input_size_mb = 0

        if type(input_size_mb) is not int or input_size_mb < 0:
            raise ValueError(
                "A non-negative input_size_mb is required"
            )

        runtime_seconds, runtime_source = predict_runtime(
            workload_type=workload_type,
            input_size_mb=input_size_mb,
        )

    if type(runtime_seconds) is not int or runtime_seconds <= 0:
        raise ValueError(
            "Runtime prediction must be a positive integer"
        )

    # 5. Calculate the best starting time
    decision = choose_start(
        job_id=job.id,
        job_type="etl",
        priority=job.priority.value,
        deadline=job.deadline,
        predicted_seconds=runtime_seconds,
        prices=prices,
        now=now,
        runtime_source=runtime_source,
    )

    # 6. Update the job status and start time
    if decision.status == "waiting_prices":
        job.status = JobStatus.PENDING
        job.scheduled_for = None

    elif decision.status in {"scheduled", "at_risk"}:
        if decision.scheduled_start is None:
            raise ValueError("Scheduled start is missing")

        job.status = JobStatus.WAITING
        job.scheduled_for = decision.scheduled_start

    else:
        raise ValueError("Unexpected scheduling decision")

    # 7. Record why the decision was made
    job.decision_reason = (
        f"{decision.reason}; source={decision.runtime_source}"
    )

    # Store the updated runtime estimate in minutes
    if workload_type is not None:
        job.estimated_minutes = max(
            1, ceil(runtime_seconds / 60)
        )

    # 8. Save changes permanently
    try:
        db.commit()
        db.refresh(job)

    except Exception:
        db.rollback()
        raise

    return job


def main():
    """Discover and plan pending GreenScale jobs."""

    from datetime import timedelta
    from zoneinfo import ZoneInfo

    import httpx

    from app.db import SessionLocal
    from app.prices import fetch_day

    # 1. Get the current time
    now = datetime.now(timezone.utc)

    oslo_today = now.astimezone(
        ZoneInfo("Europe/Oslo")
    ).date()

    # 2. Open the database and discover pending jobs
    with SessionLocal() as db:
        pending_jobs = get_pending_jobs(db)

        if not pending_jobs:
            print("No pending jobs found.")
            return

        print(f"Found {len(pending_jobs)} pending job(s).")

        # 3. Fetch available electricity prices once
        prices = []

        for day in (
            oslo_today,
            oslo_today + timedelta(days=1),
        ):
            try:
                prices.extend(fetch_day(day, area="NO2"))
                print(f"Loaded electricity prices for {day}")

            except httpx.HTTPError as error:
                print(
                    f"Prices unavailable for {day}: "
                    f"{type(error).__name__}"
                )

        # 4. Plan each pending job
        planned_count = 0
        deferred_count = 0
        failed_count = 0

        # Keep IDs so we can load fresh ORM objects
        job_ids = [job.id for job in pending_jobs]

        for job_id in job_ids:
            try:
                job = plan_pending_job(
                    db=db,
                    job_id=job_id,
                    prices=prices,
                    now=now,
                )

                print(f"\nJob ID: {job.id}")
                print(f"Name: {job.name}")
                print(f"Status: {job.status.value}")
                print(f"Estimated minutes: {job.estimated_minutes}")
                print(f"Scheduled for: {job.scheduled_for}")
                print(f"Reason: {job.decision_reason}")

                if job.status == JobStatus.WAITING:
                    planned_count += 1
                else:
                    deferred_count += 1

            except Exception as error:
                db.rollback()
                failed_count += 1

                print(
                    f"\nFailed to plan job {job_id}: "
                    f"{type(error).__name__}: {error}"
                )

        # 5. Display a summary
        print("\n--- Planning Summary ---")
        print(f"Pending jobs found: {len(job_ids)}")
        print(f"Jobs scheduled: {planned_count}")
        print(f"Jobs still pending: {deferred_count}")
        print(f"Jobs failed: {failed_count}")


if __name__ == "__main__":
    main()