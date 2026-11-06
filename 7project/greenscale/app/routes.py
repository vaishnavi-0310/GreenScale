import os
import uuid
from pathlib import Path

from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import httpx
from fastapi import APIRouter, Depends, File, HTTPException, Query, Response, UploadFile, status
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.auth import get_current_user
from app.db import get_db
from app.llm import LLMError, LLMUnavailableError, extract_job_with_response
from app.models import Job, JobStatus, User
from app.plan_pending import plan_pending_job
from app.prices import fetch_day
from app.schemas import JobCreate, JobRead, TextJobRequest

import logging

logger = logging.getLogger("greenscale")

router = APIRouter(prefix="/jobs", tags=["jobs"])


@router.post("", response_model=JobRead, status_code=status.HTTP_201_CREATED)
def create_job(
    payload: JobCreate,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    job = Job(**payload.model_dump(), user_id=user.id)
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
    return extract_job_with_response


@router.post("/from-text", response_model=JobRead, status_code=status.HTTP_201_CREATED)
def create_job_from_text(
    payload: TextJobRequest,
    db: Session = Depends(get_db),
    extractor=Depends(get_extractor),
    user: User = Depends(get_current_user),
):
    try:
        extraction = extractor(payload.text)
        if isinstance(extraction, tuple):
            job_data, llm_response = extraction
        else:
            # Keep injected/custom extractors that return only JobCreate compatible.
            job_data, llm_response = extraction, None
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

    job = Job(
        **job_data.model_dump(),
        user_id=user.id,
        llm_response=llm_response,
    )
    db.add(job)
    db.commit()
    db.refresh(job)
    logger.info(
        "job created",
        extra={"event": "job_created", "job_id": job.id, "status": job.status.value},
    )
    return job


@router.post("/{job_id}/plan", response_model=JobRead)
def plan_job(
    job_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """Estimate runtime and choose a feasible slot using current NO2 prices."""
    job = db.scalar(select(Job).where(Job.id == job_id, Job.user_id == user.id))
    if job is None:
        raise HTTPException(status_code=404, detail="Job not found")
    if job.status != JobStatus.PENDING:
        raise HTTPException(status_code=409, detail="Only pending jobs can be planned")

    now = datetime.now(timezone.utc)
    today = now.astimezone(ZoneInfo("Europe/Oslo")).date()
    prices = []
    unavailable_days = []
    for day in (today, today + timedelta(days=1)):
        try:
            prices.extend(fetch_day(day, area="NO2"))
        except httpx.HTTPError:
            # Tomorrow's prices may not be published yet; missing data is not fabricated.
            unavailable_days.append(day.isoformat())

    try:
        job = plan_pending_job(db=db, job_id=job_id, prices=prices, now=now)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    if unavailable_days and not prices:
        job.decision_reason = (
            f"{job.decision_reason}; NO2 prices unavailable for "
            f"{', '.join(unavailable_days)}"
        )
        db.commit()
        db.refresh(job)
    return job

@router.get("", response_model=list[JobRead])
def list_jobs(
    response: Response,
    status_filter: JobStatus | None = Query(default=None, alias="status"),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    filters = [Job.user_id == user.id]
    if status_filter is not None:
        filters.append(Job.status == status_filter)

    total = db.scalar(select(func.count()).select_from(Job).where(*filters)) or 0
    response.headers["X-Total-Count"] = str(total)
    stmt = select(Job).where(*filters).order_by(Job.created_at.desc(), Job.id.desc())
    return db.scalars(stmt.limit(limit).offset(offset)).all()


@router.get("/{job_id}", response_model=JobRead)
def get_job(
    job_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    job = db.scalar(select(Job).where(Job.id == job_id, Job.user_id == user.id))
    if job is None:
        raise HTTPException(status_code=404, detail="Job not found")
    return job


@router.post("/upload-csv")
def upload_csv(
    file: UploadFile = File(...),
    user: User = Depends(get_current_user),
):
    """Upload a user's CSV file to GCS for the existing CSV worker."""
    from google.cloud import storage

    bucket_name = os.getenv(
        "GREEN_SCALE_CSV_BUCKET",
        "greenscale-demo-data-group24-512813",
    )
    max_bytes = 20 * 1024 * 1024

    filename = Path(file.filename or "").name

    if not filename.lower().endswith(".csv"):
        raise HTTPException(
            status_code=400,
            detail="Only CSV files are supported.",
        )

    content = file.file.read(max_bytes + 1)

    if not content:
        raise HTTPException(status_code=400, detail="CSV file is empty.")

    if len(content) > max_bytes:
        raise HTTPException(
            status_code=413,
            detail="CSV file exceeds the 20 MB upload limit.",
        )

    object_name = f"uploads/{user.id}/{uuid.uuid4().hex}.csv"

    try:
        blob = storage.Client().bucket(bucket_name).blob(object_name)
        blob.upload_from_string(
            content,
            content_type="text/csv",
            if_generation_match=0,
        )
    except Exception:
        logger.exception("CSV upload failed")
        raise HTTPException(
            status_code=503,
            detail="Could not upload CSV to cloud storage.",
        )

    return {
        "filename": filename,
        "gcs_uri": f"gs://{bucket_name}/{object_name}",
        "size_mb": max(1, (len(content) + 1024 * 1024 - 1) // (1024 * 1024)),
    }


@router.get("/{job_id}/result")
def get_job_result(
    job_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """Read CSV summary JSON from the completed Kubernetes worker Pod."""
    import json

    from kubernetes import client, config
    from kubernetes.client.exceptions import ApiException

    job = db.scalar(
        select(Job).where(
            Job.id == job_id,
            Job.user_id == user.id,
        )
    )

    if job is None:
        raise HTTPException(status_code=404, detail="Job not found")

    if job.workload_type != "csv_summary":
        raise HTTPException(
            status_code=400,
            detail="Results are available for CSV summary jobs only.",
        )

    if job.status != JobStatus.SUCCEEDED:
        raise HTTPException(
            status_code=409,
            detail="CSV job has not completed successfully.",
        )

    namespace = os.getenv("GREEN_SCALE_NAMESPACE", "greenscale")
    job_name = job.k8s_job_name or f"gs-{job.id}"

    if job_name != f"gs-{job.id}":
        raise HTTPException(
            status_code=409,
            detail="Unexpected Kubernetes Job name.",
        )

    try:
        if os.getenv("KUBERNETES_SERVICE_HOST"):
            config.load_incluster_config()
        else:
            config.load_kube_config()

        api = client.CoreV1Api()

        pods = api.list_namespaced_pod(
            namespace=namespace,
            label_selector=f"batch.kubernetes.io/job-name={job_name}",
        )

        if not pods.items:
            pods = api.list_namespaced_pod(
                namespace=namespace,
                label_selector=f"job-name={job_name}",
            )

        completed = [
            pod for pod in pods.items
            if pod.status.phase == "Succeeded"
        ]

        if not completed:
            raise HTTPException(
                status_code=404,
                detail="Completed worker Pod not found. Its logs may have expired.",
            )

        pod_name = completed[0].metadata.name

        logs = api.read_namespaced_pod_log(
            name=pod_name,
            namespace=namespace,
            container="worker",
            tail_lines=200,
        )

        result = json.loads(logs)

        if not isinstance(result, dict) or result.get("workload") != "csv_summary":
            raise ValueError("Unexpected worker result format")

        if result.get("status") != "SUCCESS":
            raise ValueError("Worker did not report success")

        return result

    except HTTPException:
        raise
    except ApiException as exc:
        logger.warning("Kubernetes result lookup failed: %s", exc.status)
        raise HTTPException(
            status_code=502,
            detail="Could not retrieve worker logs from Kubernetes.",
        )
    except (ValueError, json.JSONDecodeError):
        raise HTTPException(
            status_code=502,
            detail="Worker logs do not contain a valid CSV summary.",
        )
    except Exception:
        logger.exception("CSV result retrieval failed")
        raise HTTPException(
            status_code=503,
            detail="Kubernetes result service is unavailable.",
        )
