import logging

from fastapi import APIRouter, Depends, Response
from sqlalchemy import text
from sqlalchemy.orm import Session

from app.db import get_db

router = APIRouter(tags=["health"])
logger = logging.getLogger("greenscale")


@router.get("/health")
def health() -> dict:
    """Liveness: the process is up. Never touches the database."""
    return {"status": "ok"}


@router.get("/ready")
def ready(response: Response, db: Session = Depends(get_db)) -> dict:
    """Readiness: dependencies are reachable. Never leaks error details."""
    try:
        db.execute(text("SELECT 1"))
    except Exception as exc:
        # Only the exception type is logged and none of it is returned,
        # because connection errors can contain hostnames and credentials.
        logger.error(
            "readiness check failed",
            extra={"event": "readiness_failed", "error": type(exc).__name__},
        )
        response.status_code = 503
        return {"status": "unavailable", "database": "down"}
    return {"status": "ready", "database": "up"}