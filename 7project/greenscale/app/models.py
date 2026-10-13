import enum
from datetime import datetime, timezone

from sqlalchemy import DateTime, Enum, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base


class Priority(str, enum.Enum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"


class JobStatus(str, enum.Enum):
    PENDING = "pending"        # stored, not yet decided
    WAITING = "waiting"        # decided: wait for a preferred window
    RUNNING = "running"        # Kubernetes Job created
    SUCCEEDED = "succeeded"
    FAILED = "failed"


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class Job(Base):
    __tablename__ = "jobs"

    id: Mapped[int] = mapped_column(primary_key=True)

    # What the user asked for
    name: Mapped[str] = mapped_column(String(200))
    description: Mapped[str | None] = mapped_column(Text, default=None)  # original text (used in Step 5)
    priority: Mapped[Priority] = mapped_column(
        Enum(Priority, native_enum=False, length=20), default=Priority.MEDIUM
    )
    deadline: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    estimated_minutes: Mapped[int] = mapped_column(Integer)

    # Scheduling + execution
    status: Mapped[JobStatus] = mapped_column(
        Enum(JobStatus, native_enum=False, length=20), default=JobStatus.PENDING
    )
    decision_reason: Mapped[str | None] = mapped_column(Text, default=None)
    scheduled_for: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=None)
    k8s_job_name: Mapped[str | None] = mapped_column(String(253), default=None)

    # Timing
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=None)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=None)