from contextlib import asynccontextmanager

from fastapi import FastAPI

from app import models  # noqa: F401
from app.db import Base, engine
from app.routes import router as jobs_router          # NEW

from app.health import router as health_router

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

app.include_router(jobs_router)                        # NEW
app.include_router(health_router)

