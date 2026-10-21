
from datetime import datetime, timezone
from math import ceil

from sqlalchemy.orm import Session

from app.models import Job, JobStatus
from app.predictor import predict_runtime
from app.scheduler import choose_start


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

    # 4. Get runtime estimate
    if workload_type is None:
        runtime_seconds = job.estimated_minutes * 60
        runtime_source = "stored_estimate"

    else:
        if (
            type(input_size_mb) is not int
            or input_size_mb < 0
        ):
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
    import argparse
    from datetime import timedelta
    from zoneinfo import ZoneInfo

    import httpx

    from app.db import SessionLocal
    from app.prices import fetch_day

    # 1. Read inputs from the terminal
    parser = argparse.ArgumentParser(
        description="Plan a pending GreenScale job"
    )

    parser.add_argument("job_id", type=int)

    parser.add_argument(
        "--workload-type",
        required=True,
        choices=["sleep", "factorial", "csv_summary"],
    )

    parser.add_argument(
        "--input-size-mb",
        required=True,
        type=int,
    )

    args = parser.parse_args()

    if args.input_size_mb < 1:
        parser.error("--input-size-mb must be positive")

    # 2. Find today's date in Norway
    now = datetime.now(timezone.utc)
    oslo_today = now.astimezone(
        ZoneInfo("Europe/Oslo")
    ).date()

    # 3. Download today's and tomorrow's prices
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

    # 4. Open PostgreSQL and plan the requested job
    with SessionLocal() as db:
        job = plan_pending_job(
            db=db,
            job_id=args.job_id,
            prices=prices,
            now=now,
            workload_type=args.workload_type,
            input_size_mb=args.input_size_mb,
        )

        # 5. Display the saved result
        print("Job ID:", job.id)
        print("Status:", job.status.value)
        print("Estimated minutes:", job.estimated_minutes)
        print("Scheduled for:", job.scheduled_for)
        print("Reason:", job.decision_reason)


if __name__ == "__main__":
    main()
