
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.db import Base
from app.models import Job, JobStatus
import app.plan_pending as planner


def test_main_automatically_plans_pending_jobs(monkeypatch, capsys):
    # 1. Create an isolated, in-memory database.
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)

    TestingSession = sessionmaker(
        bind=engine,
        expire_on_commit=False,
    )

    deadline = datetime.now(timezone.utc) + timedelta(days=2)

    # 2. Insert three test jobs.
    with TestingSession() as db:
        for name, status in [
            ("CSV Job", JobStatus.PENDING),
            ("Factorial Job", JobStatus.PENDING),
            ("Already Scheduled", JobStatus.WAITING),
        ]:
            db.add(
                Job(
                    name=name,
                    priority="medium",
                    deadline=deadline,
                    estimated_minutes=5,
                    status=status,
                )
            )
        db.commit()

    # 3. Replace the real PostgreSQL session.
    import app.db
    import app.prices

    monkeypatch.setattr(
        app.db,
        "SessionLocal",
        TestingSession,
    )

    # 4. Avoid external electricity-price API calls.
    monkeypatch.setattr(
        app.prices,
        "fetch_day",
        lambda day, area: [],
    )

    # 5. Replace the scheduler with a predictable fake.
    processed_ids = []

    def fake_plan_pending_job(db, job_id, prices, now):
        job = db.get(Job, job_id)
        processed_ids.append(job_id)

        job.status = JobStatus.WAITING
        job.scheduled_for = now + timedelta(hours=1)
        job.decision_reason = "test scheduled"

        db.commit()
        db.refresh(job)
        return job

    monkeypatch.setattr(
        planner,
        "plan_pending_job",
        fake_plan_pending_job,
    )

    # 6. Execute the real batch main() function.
    planner.main()

    # 7. Check the result.
    output = capsys.readouterr().out

    assert processed_ids == [1, 2]
    assert "Found 2 pending job(s)." in output
    assert "Jobs scheduled: 2" in output
    assert "Jobs failed: 0" in output

    # 8. Verify that database changes were saved.
    with TestingSession() as db:
        assert db.get(Job, 1).status == JobStatus.WAITING
        assert db.get(Job, 2).status == JobStatus.WAITING
        assert db.get(Job, 3).status == JobStatus.WAITING

    engine.dispose()


def test_main_continues_after_one_job_fails(monkeypatch, capsys):
    import app.db
    import app.prices

    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)

    TestingSession = sessionmaker(
        bind=engine,
        expire_on_commit=False,
    )

    deadline = datetime.now(timezone.utc) + timedelta(days=2)

    # Create three pending jobs
    with TestingSession() as db:
        for name in ["Job 1", "Job 2", "Job 3"]:
            db.add(
                Job(
                    name=name,
                    priority="medium",
                    deadline=deadline,
                    estimated_minutes=5,
                    status=JobStatus.PENDING,
                )
            )
        db.commit()

    monkeypatch.setattr(app.db, "SessionLocal", TestingSession)
    monkeypatch.setattr(
        app.prices,
        "fetch_day",
        lambda day, area: [],
    )

    processed_ids = []

    def fake_plan_pending_job(db, job_id, prices, now):
        processed_ids.append(job_id)

        if job_id == 2:
            raise ValueError("Simulated prediction failure")

        job = db.get(Job, job_id)
        job.status = JobStatus.WAITING
        job.scheduled_for = now + timedelta(hours=1)
        db.commit()
        db.refresh(job)
        return job

    monkeypatch.setattr(
        planner,
        "plan_pending_job",
        fake_plan_pending_job,
    )

    planner.main()

    output = capsys.readouterr().out

    # Every job was attempted, even after Job 2 failed
    assert processed_ids == [1, 2, 3]

    # Two jobs succeeded, one failed
    assert "Jobs scheduled: 2" in output
    assert "Jobs failed: 1" in output

    # Confirm the database statuses
    with TestingSession() as db:
        assert db.get(Job, 1).status == JobStatus.WAITING
        assert db.get(Job, 2).status == JobStatus.PENDING
        assert db.get(Job, 3).status == JobStatus.WAITING

    engine.dispose()

