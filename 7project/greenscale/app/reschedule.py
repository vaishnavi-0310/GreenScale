"""Reschedule a planned (waiting) job: PATCH /jobs/{id}/schedule."""

import logging
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, ConfigDict, Field, field_validator
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.auth import get_current_user
from app.db import get_db
from app.models import Job, JobStatus, User
from app.schemas import JobRead

logger = logging.getLogger("greenscale")
router = APIRouter(prefix="/jobs", tags=["jobs"])

PAST_GRACE = timedelta(seconds=60)  # tolerate clock skew and "run now"
MIN_BUFFER = timedelta(minutes=5)  # same safety margin as app/scheduler.py
MARK = "; rescheduled by user"


class RescheduleRequest(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)

    scheduled_for: datetime
    note: str | None = Field(default=None, max_length=200)

    @field_validator("scheduled_for")
    @classmethod
    def must_include_timezone(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError(
                "scheduled_for must include a timezone offset, "
                "for example 2026-11-06T09:00:00+01:00 or ending in Z"
            )
        return value


def _utc(value: datetime) -> datetime:
    """Treat naive values (SQLite) as UTC and normalise everything to UTC."""
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _text(value: datetime | None) -> str:
    return "unset" if value is None else _utc(value).strftime("%Y-%m-%d %H:%M UTC")


@router.patch("/{job_id}/schedule", response_model=JobRead)
def reschedule_job(
    job_id: int,
    payload: RescheduleRequest,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    # Lock the row so the dispatcher cannot claim the job while we change it.
    job = db.execute(
        select(Job).where(Job.id == job_id, Job.user_id == user.id).with_for_update()
    ).scalar_one_or_none()
    if job is None:
        raise HTTPException(status_code=404, detail="Job not found")
    if job.status != JobStatus.WAITING:
        raise HTTPException(
            status_code=409,
            detail=f"Only waiting jobs can be rescheduled; this job is {job.status.value}.",
        )

    new_start = _utc(payload.scheduled_for)
    now = datetime.now(timezone.utc)
    if new_start < now - PAST_GRACE:
        raise HTTPException(status_code=422, detail="scheduled_for must not be in the past.")

    runtime = timedelta(minutes=job.estimated_minutes)
    latest_start = _utc(job.deadline) - runtime - max(MIN_BUFFER, runtime * 0.2)
    if new_start > latest_start:
        raise HTTPException(
            status_code=422,
            detail=(
                "Too late to finish before the deadline. "
                f"The latest possible start is {_text(latest_start)}."
            ),
        )

    previous = job.scheduled_for
    base = (job.decision_reason or "Scheduled").split(MARK)[0]
    note = f" ({payload.note})" if payload.note else ""
    job.scheduled_for = new_start
    job.decision_reason = f"{base}{MARK} from {_text(previous)} to {_text(new_start)}{note}"
    db.commit()
    db.refresh(job)

    logger.info(
        "job rescheduled",
        extra={"event": "job_rescheduled", "job_id": job.id, "status": job.status.value},
    )
    return job
