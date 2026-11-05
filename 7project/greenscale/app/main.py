import logging
import time
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from app import models  # noqa: F401
from app.config import settings
from app.db import Base, engine
from app.health import router as health_router
from app.logging_config import setup_logging
from app.routes import router as jobs_router
from app.reschedule import router as reschedule_router

setup_logging(settings.log_level)
logger = logging.getLogger("greenscale")


@asynccontextmanager
async def lifespan(app: FastAPI):
    Base.metadata.create_all(bind=engine)
    yield


app = FastAPI(
    title="GreenScale",
    description="Flexible Kubernetes Job Scheduler",
    version="0.1.0",
    lifespan=lifespan,
)

QUIET_PATHS = {"/health", "/ready"}  # probes fire every few seconds


@app.middleware("http")
async def log_requests(request: Request, call_next):
    start = time.perf_counter()
    try:
        response = await call_next(request)
    except Exception as exc:
        logger.error(
            "request failed",
            extra={
                "event": "http_error",
                "method": request.method,
                "path": request.url.path,
                "status_code": 500,
                "error": type(exc).__name__,
            },
        )
        raise
    level = logging.DEBUG if request.url.path in QUIET_PATHS else logging.INFO
    logger.log(
        level,
        "request",
        extra={
            "event": "http_request",
            "method": request.method,
            "path": request.url.path,
            "status_code": response.status_code,
            "duration_ms": round((time.perf_counter() - start) * 1000, 1),
        },
    )
    return response


app.include_router(jobs_router)
app.include_router(reschedule_router)
app.include_router(health_router)

STATIC_DIR = Path(__file__).resolve().parent.parent / "static"
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


@app.get("/", include_in_schema=False)
def ui():
    return FileResponse(STATIC_DIR / "index.html")
