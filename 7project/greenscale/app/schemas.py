from datetime import datetime, timedelta, timezone

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from app.models import JobStatus, Priority


class JobCreate(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    description: str | None = None
    priority: Priority = Priority.MEDIUM
    deadline: datetime
    estimated_minutes: int = Field(gt=0, le=1440)  # 1 minute to 24 hours

    @field_validator("deadline")
    @classmethod
    def make_timezone_aware(cls, v: datetime) -> datetime:
        # Treat naive datetimes as UTC so comparisons never crash
        if v.tzinfo is None:
            return v.replace(tzinfo=timezone.utc)
        return v

    @model_validator(mode="after")
    def deadline_must_fit_runtime(self):
        earliest_finish = datetime.now(timezone.utc) + timedelta(
            minutes=self.estimated_minutes
        )
        if self.deadline < earliest_finish:
            raise ValueError(
                "deadline is too soon: the job could not finish "
                "even if it started right now"
            )
        return self


class JobRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    name: str
    description: str | None
    priority: Priority
    deadline: datetime
    estimated_minutes: int
    status: JobStatus
    decision_reason: str | None
    scheduled_for: datetime | None
    k8s_job_name: str | None
    created_at: datetime
    started_at: datetime | None
    finished_at: datetime | None


class TextJobRequest(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)

    text: str = Field(min_length=1, max_length=2000)