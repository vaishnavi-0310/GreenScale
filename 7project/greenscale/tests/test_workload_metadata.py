from datetime import datetime, timedelta, timezone

import pytest
from pydantic import ValidationError

from app.schemas import JobCreate


def future_deadline():
    """Return a deadline safely in the future."""
    return datetime.now(timezone.utc) + timedelta(days=2)


def test_valid_workload_metadata():
    job = JobCreate(
        name="CSV Processing",
        workload_type="csv_summary",
        input_size_mb=10,
        priority="medium",
        deadline=future_deadline(),
        estimated_minutes=5,
    )

    assert job.workload_type == "csv_summary"
    assert job.input_size_mb == 10


def test_invalid_workload_type():
    with pytest.raises(ValidationError):
        JobCreate(
            name="Invalid Workload",
            workload_type="video_editing",
            input_size_mb=10,
            deadline=future_deadline(),
            estimated_minutes=5,
        )


@pytest.mark.parametrize("size", [0, -1, -10])
def test_invalid_input_size(size):
    with pytest.raises(ValidationError):
        JobCreate(
            name="Invalid Input Size",
            workload_type="csv_summary",
            input_size_mb=size,
            deadline=future_deadline(),
            estimated_minutes=5,
        )


def test_old_job_request_still_works():
    job = JobCreate(
        name="Legacy Job",
        deadline=future_deadline(),
        estimated_minutes=5,
    )

    assert job.workload_type is None
    assert job.input_size_mb is None

from sqlalchemy import create_engine
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from app.db import Base
from app.models import Job, JobStatus
from app.plan_pending import get_pending_jobs


def test_get_pending_jobs():
    # Create a temporary in-memory SQLite database
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )

    Base.metadata.create_all(engine)

    with Session(engine) as db:
        deadline = future_deadline()

        # Create three jobs with different statuses
        db.add_all([
            Job(
                name="Pending CSV",
                workload_type="csv_summary",
                input_size_mb=10,
                priority="medium",
                deadline=deadline,
                estimated_minutes=5,
                status=JobStatus.PENDING,
            ),
            Job(
                name="Waiting Job",
                priority="medium",
                deadline=deadline,
                estimated_minutes=5,
                status=JobStatus.WAITING,
            ),
            Job(
                name="Pending Factorial",
                workload_type="factorial",
                input_size_mb=1,
                priority="high",
                deadline=deadline,
                estimated_minutes=5,
                status=JobStatus.PENDING,
            ),
        ])

        db.commit()

        # Retrieve pending jobs
        pending = get_pending_jobs(db)

        assert len(pending) == 2
        assert [job.name for job in pending] == [
            "Pending CSV",
            "Pending Factorial",
        ]

    engine.dispose()


def test_planner_uses_stored_metadata(monkeypatch):
    from datetime import datetime, timedelta, timezone
    from types import SimpleNamespace

    import app.plan_pending as planner
    from app.models import JobStatus
    from sqlalchemy import create_engine
    from sqlalchemy.orm import Session
    from sqlalchemy.pool import StaticPool

    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)

    now = datetime.now(timezone.utc)
    predictor_calls = []

    def fake_predict_runtime(workload_type, input_size_mb):
        predictor_calls.append((workload_type, input_size_mb))
        return 120, "test_predictor"

    def fake_choose_start(**kwargs):
        assert kwargs["predicted_seconds"] == 120
        assert kwargs["runtime_source"] == "test_predictor"

        return SimpleNamespace(
            status="scheduled",
            scheduled_start=now + timedelta(hours=1),
            reason="Test scheduling decision",
            runtime_source="test_predictor",
        )

    monkeypatch.setattr(
        planner, "predict_runtime", fake_predict_runtime
    )
    monkeypatch.setattr(
        planner, "choose_start", fake_choose_start
    )

    with Session(engine) as db:
        job = Job(
            name="Metadata Prediction Test",
            workload_type="csv_summary",
            input_size_mb=10,
            priority="medium",
            deadline=now + timedelta(days=2),
            estimated_minutes=5,
            status=JobStatus.PENDING,
        )
        db.add(job)
        db.commit()
        db.refresh(job)

        result = planner.plan_pending_job(
            db=db,
            job_id=job.id,
            prices=[],
            now=now,
        )

        assert predictor_calls == [("csv_summary", 10)]
        assert result.status == JobStatus.WAITING
        assert result.estimated_minutes == 2
        assert "source=test_predictor" in result.decision_reason

    engine.dispose()
