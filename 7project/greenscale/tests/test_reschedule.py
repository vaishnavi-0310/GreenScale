import logging
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.db import Base, get_db
from app.main import app
from app.models import Job, JobStatus

REASON = "Lowest available price score; source=llm"


def now():
    return datetime.now(timezone.utc)


def parse(value):
    dt = datetime.fromisoformat(value)
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def close(a, b):
    return abs(a - b) < timedelta(seconds=1)


@pytest.fixture()
def env():
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)

    def override_get_db():
        db = Session()
        try:
            yield db
        finally:
            db.close()

    app.dependency_overrides[get_db] = override_get_db
    client = TestClient(app)
    registration = client.post(
        "/auth/register",
        json={"name": "CI User", "email": "ci@example.com", "password": "test-password-123"},
    )
    assert registration.status_code == 201
    login = client.post(
        "/auth/login",
        json={"email": "ci@example.com", "password": "test-password-123"},
    )
    assert login.status_code == 200
    yield client, Session
    app.dependency_overrides.clear()
    engine.dispose()


def add_job(Session, *, status=JobStatus.WAITING, minutes=10,
            deadline_in=timedelta(hours=10), scheduled_in=timedelta(hours=1)):
    with Session() as db:
        job = Job(
            user_id=1,
            name="demo",
            deadline=now() + deadline_in,
            estimated_minutes=minutes,
            status=status,
            scheduled_for=now() + scheduled_in,
            decision_reason=REASON,
        )
        db.add(job)
        db.commit()
        db.refresh(job)
        return job.id


def reschedule(client, job_id, when, **extra):
    value = when.isoformat() if isinstance(when, datetime) else when
    return client.patch(f"/jobs/{job_id}/schedule", json={"scheduled_for": value, **extra})


def stored(client, job_id):
    return client.get(f"/jobs/{job_id}").json()


def test_moves_a_waiting_job(env):
    client, Session = env
    job_id = add_job(Session)
    target = now() + timedelta(hours=3)

    r = reschedule(client, job_id, target)

    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "waiting"
    assert close(parse(body["scheduled_for"]), target)
    assert body["decision_reason"].startswith(REASON)
    assert "rescheduled by user" in body["decision_reason"]
    assert close(parse(stored(client, job_id)["scheduled_for"]), target)


def test_accepts_other_utc_offsets(env):
    client, Session = env
    job_id = add_job(Session)
    target = now() + timedelta(hours=3)
    local = target.astimezone(timezone(timedelta(hours=1)))

    r = reschedule(client, job_id, local)

    assert r.status_code == 200
    assert close(parse(r.json()["scheduled_for"]), target)


def test_run_now_is_allowed(env):
    client, Session = env
    job_id = add_job(Session)
    assert reschedule(client, job_id, now()).status_code == 200


def test_rejects_a_time_without_timezone(env):
    client, Session = env
    job_id = add_job(Session)
    assert reschedule(client, job_id, "2030-01-01T09:00:00").status_code == 422


def test_rejects_a_time_in_the_past_and_keeps_the_old_schedule(env):
    client, Session = env
    job_id = add_job(Session)
    before = stored(client, job_id)["scheduled_for"]

    r = reschedule(client, job_id, now() - timedelta(hours=2))

    assert r.status_code == 422
    assert "past" in r.json()["detail"]
    assert stored(client, job_id)["scheduled_for"] == before


def test_rejects_a_start_that_would_miss_the_deadline(env):
    client, Session = env
    # 60 min job, deadline in 2 h: latest start is about 48 min from now.
    job_id = add_job(Session, minutes=60, deadline_in=timedelta(hours=2))

    too_late = reschedule(client, job_id, now() + timedelta(minutes=90))
    assert too_late.status_code == 422
    assert "latest possible start" in too_late.json()["detail"]

    in_time = reschedule(client, job_id, now() + timedelta(minutes=30))
    assert in_time.status_code == 200


@pytest.mark.parametrize(
    "status",
    [JobStatus.PENDING, JobStatus.RUNNING, JobStatus.SUCCEEDED, JobStatus.FAILED],
)
def test_only_waiting_jobs_can_be_rescheduled(env, status):
    client, Session = env
    job_id = add_job(Session, status=status)
    before = stored(client, job_id)["scheduled_for"]

    r = reschedule(client, job_id, now() + timedelta(hours=3))

    assert r.status_code == 409
    assert status.value in r.json()["detail"]
    assert stored(client, job_id)["scheduled_for"] == before


def test_unknown_job_returns_404(env):
    client, _ = env
    assert reschedule(client, 999, now() + timedelta(hours=1)).status_code == 404


@pytest.mark.parametrize(
    "body",
    [
        {},
        {"scheduled_for": "not-a-date"},
        {"scheduled_for": (now() + timedelta(hours=1)).isoformat(), "note": "x" * 201},
    ],
)
def test_bad_request_bodies_return_422(env, body):
    client, Session = env
    job_id = add_job(Session)
    assert client.patch(f"/jobs/{job_id}/schedule", json=body).status_code == 422


def test_repeated_reschedules_do_not_stack_the_reason(env):
    client, Session = env
    job_id = add_job(Session)
    first = now() + timedelta(hours=2)
    second = now() + timedelta(hours=4)

    reschedule(client, job_id, first)
    r = reschedule(client, job_id, second)

    reason = r.json()["decision_reason"]
    assert reason.count("rescheduled by user") == 1
    assert reason.startswith(REASON)
    assert close(parse(r.json()["scheduled_for"]), second)


def test_note_is_recorded(env):
    client, Session = env
    job_id = add_job(Session)
    r = reschedule(client, job_id, now() + timedelta(hours=2), note="moved for demo")
    assert "(moved for demo)" in r.json()["decision_reason"]


def test_other_jobs_are_untouched(env):
    client, Session = env
    target_id = add_job(Session)
    other_id = add_job(Session)
    before = stored(client, other_id)

    reschedule(client, target_id, now() + timedelta(hours=3))

    assert stored(client, other_id) == before


class ListHandler(logging.Handler):
    def __init__(self):
        super().__init__()
        self.records = []

    def emit(self, record):
        self.records.append(record)


def test_logs_the_reschedule_event(env):
    client, Session = env
    job_id = add_job(Session)
    handler = ListHandler()
    logger = logging.getLogger("greenscale")
    logger.addHandler(handler)
    try:
        reschedule(client, job_id, now() + timedelta(hours=2))
    finally:
        logger.removeHandler(handler)

    events = [r for r in handler.records if getattr(r, "event", "") == "job_rescheduled"]
    assert len(events) == 1
    assert events[0].job_id == job_id


def test_dispatcher_picks_up_a_job_moved_to_now(env):
    from execution.dispatcher import dispatch_due_jobs

    client, Session = env
    job_id = add_job(Session)  # planned for one hour from now, so not due yet
    api = Mock()
    api.read_namespaced_job.return_value = SimpleNamespace(
        status=SimpleNamespace(succeeded=1, failed=0, active=0, conditions=[])
    )
    with Session() as db:
        assert dispatch_due_jobs(db, api) == []
    api.create_namespaced_job.assert_not_called()

    assert reschedule(client, job_id, now()).status_code == 200

    with Session() as db:
        results = dispatch_due_jobs(db, api, now=now() + timedelta(seconds=5))
    assert results[0].outcome == "submitted"
    assert stored(client, job_id)["status"] == "succeeded"
