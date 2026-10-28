from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db import get_db
from app.llm import LLMError, LLMUnavailableError, extract_job
from app.models import Job, JobStatus
from app.schemas import JobCreate, JobRead, TextJobRequest

import logging

logger = logging.getLogger("greenscale")

router = APIRouter(prefix="/jobs", tags=["jobs"])


@router.post("", response_model=JobRead, status_code=status.HTTP_201_CREATED)
def create_job(payload: JobCreate, db: Session = Depends(get_db)):
    job = Job(**payload.model_dump())
    db.add(job)
    db.commit()
    db.refresh(job)
    logger.info(
        "job created",
        extra={"event": "job_created", "job_id": job.id, "status": job.status.value},
    )
    return job

def get_extractor():
    """Dependency so tests can swap in a fake LLM."""
    return extract_job


@router.post("/from-text", response_model=JobRead, status_code=status.HTTP_201_CREATED)
def create_job_from_text(
    payload: TextJobRequest,
    db: Session = Depends(get_db),
    extractor=Depends(get_extractor),
):
    try:
        job_data = extractor(payload.text)
    except LLMUnavailableError:
        logger.warning("llm unavailable", extra={"event": "llm_unavailable"})   # in the 503 block
        logger.warning("llm output rejected", extra={"event": "llm_rejected"})  # in the 502 block
        raise HTTPException(
            status_code=503,
            detail="The LLM service is temporarily unavailable. Please retry shortly.",
            headers={"Retry-After": "30"},
        )
    except LLMError as e:
        logger.warning("llm unavailable", extra={"event": "llm_unavailable"})   # in the 503 block
        logger.warning("llm output rejected", extra={"event": "llm_rejected"})  # in the 502 block
        raise HTTPException(
            status_code=502,
            detail=f"Could not turn the description into a valid job: {e}",
        )

    job = Job(**job_data.model_dump())
    db.add(job)
    db.commit()
    db.refresh(job)
    logger.info(
        "job created",
        extra={"event": "job_created", "job_id": job.id, "status": job.status.value},
    )
    return job

@router.get("", response_model=list[JobRead])
def list_jobs(
    status_filter: JobStatus | None = Query(default=None, alias="status"),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    db: Session = Depends(get_db),
):
    stmt = select(Job).order_by(Job.created_at.desc(), Job.id.desc())
    if status_filter is not None:
        stmt = stmt.where(Job.status == status_filter)
    return db.scalars(stmt.limit(limit).offset(offset)).all()


@router.get("/{job_id}", response_model=JobRead)
def get_job(job_id: int, db: Session = Depends(get_db)):
    job = db.get(Job, job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="Job not found")
    return job