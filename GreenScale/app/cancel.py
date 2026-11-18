"""Cancel a job that has not started yet: POST /jobs/{id}/cancel."""

import logging
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.auth import get_current_user
from app.db import get_db
from app.models import Job, JobStatus, User
from app.schemas import JobRead

logger = logging.getLogger("greenscale")
router = APIRouter(prefix="/jobs", tags=["jobs"])

CANCELLABLE = {JobStatus.PENDING, JobStatus.WAITING}
MARK = "cancelled by user"


class CancelRequest(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)

    reason: str | None = Field(default=None, max_length=200)


def _utc_text(value: datetime | None) -> str:
    if value is None:
        return ""
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")


def _why_not(job: Job) -> str:
    if job.status == JobStatus.CANCELLED:
        return "This job is already cancelled."
    if job.status == JobStatus.RUNNING:
        return "This job has already started and cannot be stopped from GreenScale."
    return f"This job has already finished ({job.status.value}) and cannot be cancelled."


@router.post("/{job_id}/cancel", response_model=JobRead)
def cancel_job(
    job_id: int,
    payload: CancelRequest | None = None,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    # Same ownership rule as rescheduling: someone else's job is a 404.
    # The row is locked so the dispatcher cannot claim it while we change it.
    job = db.execute(
        select(Job).where(Job.id == job_id, Job.user_id == user.id).with_for_update()
    ).scalar_one_or_none()
    if job is None:
        raise HTTPException(status_code=404, detail="Job not found")
    if job.status not in CANCELLABLE:
        raise HTTPException(status_code=409, detail=_why_not(job))

    planned = _utc_text(job.scheduled_for)
    was = f" (was planned for {planned})" if planned else ""
    reason = f" ({payload.reason})" if payload and payload.reason else ""
    if job.decision_reason:
        job.decision_reason = f"{job.decision_reason}; {MARK}{was}{reason}"
    else:
        job.decision_reason = f"{MARK.capitalize()}{was}{reason}"
    job.status = JobStatus.CANCELLED
    job.scheduled_for = None  # a cancelled job must never look like an upcoming one
    db.commit()
    db.refresh(job)

    logger.info(
        "job cancelled",
        extra={"event": "job_cancelled", "job_id": job.id, "status": job.status.value},
    )
    return job
