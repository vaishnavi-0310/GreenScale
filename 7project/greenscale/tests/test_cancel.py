import logging
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.auth import get_current_user
from app.db import Base, get_db
from app.main import app
from app.models import Job, JobStatus, User

REASON = "Lowest available price score; source=llm"
OWNER = object()  # marker: "the logged-in user owns this job"


def now():
    return datetime.now(timezone.utc)


@pytest.fixture()
def env():
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)

    def make_user(name):
        with Session() as db:
            user = User(
                name=name,
                email=f"{name.lower()}@example.com",
                password_hash="x",
                created_at=now(),
            )
            db.add(user)
            db.commit()
            db.refresh(user)
            return user

    owner, other = make_user("Owner"), make_user("Other")

    def override_get_db():
        db = Session()
        try:
            yield db
        finally:
            db.close()

    app.dependency_overrides[get_db] = override_get_db
    app.dependency_overrides[get_current_user] = lambda: owner
    yield SimpleNamespace(client=TestClient(app), Session=Session, owner=owner, other=other)
    app.dependency_overrides.clear()
    engine.dispose()


def add_job(env, *, status=JobStatus.WAITING, user_id=OWNER,
            scheduled_in=timedelta(hours=1), reason=REASON):
    with env.Session() as db:
        job = Job(
            name="demo",
            deadline=now() + timedelta(days=2),
            estimated_minutes=5,
            status=status,
            scheduled_for=None if scheduled_in is None else now() + scheduled_in,
            decision_reason=reason,
            user_id=env.owner.id if user_id is OWNER else user_id,
        )
        db.add(job)
        db.commit()
        db.refresh(job)
        return job.id


def stored(env, job_id):
    with env.Session() as db:
        return db.get(Job, job_id)


def cancel(env, job_id, **body):
    url = f"/jobs/{job_id}/cancel"
    return env.client.post(url, json=body) if body else env.client.post(url)


def test_cancels_a_waiting_job(env):
    job_id = add_job(env)

    r = cancel(env, job_id)

    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "cancelled"
    assert body["scheduled_for"] is None
    assert body["decision_reason"].startswith(REASON)
    assert "cancelled by user" in body["decision_reason"]
    assert "was planned for" in body["decision_reason"]
    job = stored(env, job_id)
    assert job.status == JobStatus.CANCELLED and job.scheduled_for is None


def test_cancels_a_pending_job_that_was_never_planned(env):
    job_id = add_job(env, status=JobStatus.PENDING, scheduled_in=None, reason=None)

    r = cancel(env, job_id)

    assert r.status_code == 200
    assert r.json()["status"] == "cancelled"
    assert r.json()["decision_reason"] == "Cancelled by user"


def test_reason_is_recorded(env):
    job_id = add_job(env)
    r = cancel(env, job_id, reason="no longer needed")
    assert "(no longer needed)" in r.json()["decision_reason"]


def test_an_empty_json_body_is_accepted(env):
    job_id = add_job(env)
    assert env.client.post(f"/jobs/{job_id}/cancel", json={}).status_code == 200


def test_reason_longer_than_200_characters_is_rejected(env):
    job_id = add_job(env)
    assert cancel(env, job_id, reason="x" * 201).status_code == 422
    assert stored(env, job_id).status == JobStatus.WAITING


@pytest.mark.parametrize(
    "status, phrase",
    [
        (JobStatus.RUNNING, "already started"),
        (JobStatus.SUCCEEDED, "already finished"),
        (JobStatus.FAILED, "already finished"),
        (JobStatus.CANCELLED, "already cancelled"),
    ],
)
def test_jobs_that_cannot_be_cancelled_return_409(env, status, phrase):
    job_id = add_job(env, status=status)
    before = stored(env, job_id).decision_reason

    r = cancel(env, job_id)

    assert r.status_code == 409
    assert phrase in r.json()["detail"]
    job = stored(env, job_id)
    assert job.status == status and job.decision_reason == before


def test_unknown_job_returns_404(env):
    assert cancel(env, 999).status_code == 404


def test_someone_elses_job_returns_404_and_is_untouched(env):
    job_id = add_job(env, user_id=env.other.id)
    assert cancel(env, job_id).status_code == 404
    assert stored(env, job_id).status == JobStatus.WAITING


def test_a_job_without_an_owner_returns_404(env):
    job_id = add_job(env, user_id=None)
    assert cancel(env, job_id).status_code == 404
    assert stored(env, job_id).status == JobStatus.WAITING


def test_login_is_required(env):
    job_id = add_job(env)
    app.dependency_overrides.pop(get_current_user)

    r = cancel(env, job_id)

    assert r.status_code == 401
    assert stored(env, job_id).status == JobStatus.WAITING


def test_other_jobs_are_untouched(env):
    target = add_job(env)
    bystander = add_job(env)
    before = stored(env, bystander).scheduled_for

    cancel(env, target)

    other = stored(env, bystander)
    assert other.status == JobStatus.WAITING and other.scheduled_for == before


class ListHandler(logging.Handler):
    def __init__(self):
        super().__init__()
        self.records = []

    def emit(self, record):
        self.records.append(record)


def test_logs_the_cancel_event(env):
    job_id = add_job(env)
    handler = ListHandler()
    logger = logging.getLogger("greenscale")
    logger.addHandler(handler)
    try:
        cancel(env, job_id)
    finally:
        logger.removeHandler(handler)

    events = [r for r in handler.records if getattr(r, "event", "") == "job_cancelled"]
    assert len(events) == 1 and events[0].job_id == job_id


def test_status_fits_the_database_column(env):
    job_id = add_job(env)
    cancel(env, job_id)
    with env.Session() as db:
        raw = db.execute(text("SELECT status FROM jobs WHERE id = :i"), {"i": job_id}).scalar_one()
    assert raw.upper() == "CANCELLED" and len(raw) <= 20


def test_dispatcher_ignores_a_cancelled_job_even_with_a_past_schedule(env):
    from execution.dispatcher import dispatch_due_jobs

    job_id = add_job(env, status=JobStatus.CANCELLED, scheduled_in=timedelta(minutes=-5))
    api = Mock()

    with env.Session() as db:
        assert dispatch_due_jobs(db, api) == []

    api.create_namespaced_job.assert_not_called()
    assert stored(env, job_id).status == JobStatus.CANCELLED


def test_planner_ignores_a_cancelled_job(env):
    from app.plan_pending import get_pending_jobs

    add_job(env, status=JobStatus.CANCELLED, scheduled_in=None, reason=None)
    pending = add_job(env, status=JobStatus.PENDING, scheduled_in=None, reason=None)

    with env.Session() as db:
        assert [j.id for j in get_pending_jobs(db)] == [pending]


def test_planning_a_cancelled_job_is_refused(env):
    from app.plan_pending import plan_pending_job

    job_id = add_job(env, status=JobStatus.CANCELLED, scheduled_in=None, reason=None)

    with env.Session() as db:
        with pytest.raises(ValueError):
            plan_pending_job(db=db, job_id=job_id, prices=[])
    assert stored(env, job_id).status == JobStatus.CANCELLED
