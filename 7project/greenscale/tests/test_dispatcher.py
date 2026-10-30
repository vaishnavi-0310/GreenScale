from datetime import datetime, timedelta, timezone
import sys
from types import ModuleType, SimpleNamespace
from unittest.mock import Mock

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.db import Base
from app.models import Job, JobStatus
from execution.dispatcher import (
    create_batch_api,
    dispatch_due_jobs,
    job_to_executor_request,
)


@pytest.fixture
def db_session():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    with factory() as session:
        yield session
    engine.dispose()


def make_job(db, *, status=JobStatus.WAITING, scheduled_for=None, name="demo"):
    job = Job(
        name=name,
        deadline=datetime(2026, 11, 1, tzinfo=timezone.utc),
        estimated_minutes=1,
        status=status,
        scheduled_for=scheduled_for,
    )
    db.add(job)
    db.commit()
    db.refresh(job)
    return job


def test_request_adapter_produces_executor_contract(db_session):
    job = make_job(
        db_session,
        scheduled_for=datetime(2026, 10, 28, 12, tzinfo=timezone.utc),
    )
    request = job_to_executor_request(job)
    assert request == {
        "job_id": str(job.id),
        "image": "busybox:1.36",
        "command": ["echo", f"GreenScale legacy job {job.id}: demo"],
        "scheduled_start_utc": "2026-10-28T12:00:00Z",
        "namespace": "greenscale",
    }


def test_dispatch_claims_due_job_and_persists_terminal_status(db_session):
    now = datetime(2026, 10, 28, 13, tzinfo=timezone.utc)
    job = make_job(
        db_session,
        scheduled_for=now - timedelta(minutes=1),
    )
    api = Mock()
    api.read_namespaced_job.return_value = SimpleNamespace(
        status=SimpleNamespace(succeeded=1, failed=0, active=0, conditions=[])
    )

    results = dispatch_due_jobs(db_session, api, now=now)

    db_session.refresh(job)
    assert results[0].outcome == "submitted"
    assert job.status == JobStatus.SUCCEEDED
    assert job.k8s_job_name == f"gs-{job.id}"
    assert job.finished_at.replace(tzinfo=timezone.utc) == now
    api.create_namespaced_job.assert_called_once()


def test_dispatch_skips_not_yet_due_jobs(db_session):
    now = datetime(2026, 10, 28, 13, tzinfo=timezone.utc)
    make_job(db_session, scheduled_for=now + timedelta(minutes=1))
    api = Mock()

    assert dispatch_due_jobs(db_session, api, now=now) == []
    api.create_namespaced_job.assert_not_called()


def test_dispatch_is_retry_safe_when_kubernetes_reports_conflict(db_session):
    now = datetime(2026, 10, 28, 13, tzinfo=timezone.utc)
    job = make_job(db_session, scheduled_for=now - timedelta(minutes=1))
    api = Mock()
    # A 409 must be represented by an exception, like Kubernetes ApiException.
    class Conflict(Exception):
        status = 409
    api.create_namespaced_job.side_effect = Conflict()
    api.read_namespaced_job.return_value = SimpleNamespace(
        status=SimpleNamespace(succeeded=0, failed=0, active=0, conditions=[])
    )

    results = dispatch_due_jobs(db_session, api, now=now)

    db_session.refresh(job)
    assert results[0].outcome == "already_exists"
    assert job.status == JobStatus.RUNNING
    assert job.k8s_job_name == f"gs-{job.id}"


def test_dispatch_releases_claim_after_api_error_for_retry(db_session):
    now = datetime(2026, 10, 28, 13, tzinfo=timezone.utc)
    job = make_job(db_session, scheduled_for=now - timedelta(minutes=1))
    api = Mock()
    api.create_namespaced_job.side_effect = RuntimeError("temporary API error")

    results = dispatch_due_jobs(db_session, api, now=now)

    db_session.refresh(job)
    assert results[0].outcome == "error"
    assert results[0].error_type == "RuntimeError"
    assert job.status == JobStatus.WAITING
    api.read_namespaced_job.assert_not_called()


def test_dispatch_requires_timezone_aware_now(db_session):
    api = Mock()
    with pytest.raises(ValueError, match="timezone-aware"):
        dispatch_due_jobs(db_session, api, now=datetime(2026, 10, 28, 13))


def test_job_without_schedule_cannot_be_adapted(db_session):
    job = make_job(db_session, scheduled_for=None)
    with pytest.raises(ValueError, match="scheduled_for"):
        job_to_executor_request(job)


@pytest.mark.parametrize("in_cluster, loader", [
    (True, "load_incluster_config"),
    (False, "load_kube_config"),
])
def test_create_batch_api_loads_selected_credentials(monkeypatch, in_cluster, loader):
    config = SimpleNamespace(
        load_incluster_config=Mock(),
        load_kube_config=Mock(),
    )
    expected_api = object()
    client = SimpleNamespace(BatchV1Api=Mock(return_value=expected_api))
    fake_kubernetes = ModuleType("kubernetes")
    fake_kubernetes.client = client
    fake_kubernetes.config = config
    monkeypatch.setitem(sys.modules, "kubernetes", fake_kubernetes)

    assert create_batch_api(in_cluster=in_cluster) is expected_api
    getattr(config, loader).assert_called_once_with()
    other_loader = "load_kube_config" if in_cluster else "load_incluster_config"
    getattr(config, other_loader).assert_not_called()
    client.BatchV1Api.assert_called_once_with()


def test_dispatch_can_target_one_job_only(db_session):
    now = datetime(2026, 10, 28, 13, tzinfo=timezone.utc)

    target = make_job(
        db_session,
        scheduled_for=now - timedelta(minutes=1),
        name="isolated test job",
    )
    other = make_job(
        db_session,
        scheduled_for=now - timedelta(minutes=1),
        name="must remain waiting",
    )

    api = Mock()
    api.read_namespaced_job.return_value = SimpleNamespace(
        status=SimpleNamespace(succeeded=1, failed=0, active=0, conditions=[])
    )

    results = dispatch_due_jobs(
        db_session,
        api,
        now=now,
        job_id=target.id,
    )

    db_session.refresh(target)
    db_session.refresh(other)

    assert len(results) == 1
    assert results[0].job_id == target.id
    assert target.status == JobStatus.SUCCEEDED
    assert other.status == JobStatus.WAITING
    api.create_namespaced_job.assert_called_once()

def test_dispatch_stores_kubernetes_start_and_finish_times(db_session):
    now = datetime(2026, 10, 28, 13, tzinfo=timezone.utc)
    k8s_start = datetime(2026, 10, 28, 12, 59, 50, tzinfo=timezone.utc)
    k8s_end = datetime(2026, 10, 28, 12, 59, 54, tzinfo=timezone.utc)
    job = make_job(db_session, scheduled_for=now - timedelta(minutes=1))
    api = Mock()
    api.read_namespaced_job.return_value = SimpleNamespace(
        status=SimpleNamespace(
            succeeded=1,
            failed=0,
            active=0,
            conditions=[],
            start_time=k8s_start,
            completion_time=k8s_end,
        )
    )

    dispatch_due_jobs(db_session, api, now=now)

    db_session.refresh(job)
    assert job.status == JobStatus.SUCCEEDED
    assert job.started_at.replace(tzinfo=timezone.utc) == k8s_start
    assert job.finished_at.replace(tzinfo=timezone.utc) == k8s_end


def test_failure_reading_times_does_not_undo_the_status_update(db_session):
    now = datetime(2026, 10, 28, 13, tzinfo=timezone.utc)
    job = make_job(db_session, scheduled_for=now - timedelta(minutes=1))
    finished = SimpleNamespace(
        status=SimpleNamespace(succeeded=1, failed=0, active=0, conditions=[])
    )
    api = Mock()
    # First read gives the status, the second read (for the times) fails.
    api.read_namespaced_job.side_effect = [finished, RuntimeError("boom")]

    results = dispatch_due_jobs(db_session, api, now=now)

    db_session.refresh(job)
    assert results[0].outcome == "submitted"
    assert job.status == JobStatus.SUCCEEDED
    assert job.finished_at.replace(tzinfo=timezone.utc) == now


def status_sequence(*items):
    """Return each item in turn, then keep returning the last one."""
    remaining = iter(items)

    def _next(**kwargs):
        return next(remaining, items[-1])

    return _next


PENDING = SimpleNamespace(
    status=SimpleNamespace(succeeded=0, failed=0, active=0, conditions=[])
)
DONE = SimpleNamespace(
    status=SimpleNamespace(succeeded=1, failed=0, active=0, conditions=[])
)


def test_dispatch_polls_until_the_job_finishes(db_session):
    now = datetime(2026, 10, 28, 13, tzinfo=timezone.utc)
    job = make_job(db_session, scheduled_for=now - timedelta(minutes=1))
    api = Mock()
    api.read_namespaced_job.side_effect = status_sequence(PENDING, PENDING, DONE)
    sleep = Mock()

    dispatch_due_jobs(db_session, api, now=now, wait_seconds=10, sleep=sleep)

    db_session.refresh(job)
    assert job.status == JobStatus.SUCCEEDED
    assert sleep.call_count == 2


def test_dispatch_stops_waiting_after_the_limit(db_session):
    now = datetime(2026, 10, 28, 13, tzinfo=timezone.utc)
    job = make_job(db_session, scheduled_for=now - timedelta(minutes=1))
    api = Mock()
    api.read_namespaced_job.side_effect = status_sequence(PENDING)
    sleep = Mock()

    dispatch_due_jobs(db_session, api, now=now, wait_seconds=3, sleep=sleep)

    db_session.refresh(job)
    assert job.status == JobStatus.RUNNING
    assert sleep.call_count == 3


def test_dispatch_does_not_wait_by_default(db_session):
    now = datetime(2026, 10, 28, 13, tzinfo=timezone.utc)
    job = make_job(db_session, scheduled_for=now - timedelta(minutes=1))
    api = Mock()
    api.read_namespaced_job.side_effect = status_sequence(PENDING)
    sleep = Mock()

    dispatch_due_jobs(db_session, api, now=now, sleep=sleep)

    db_session.refresh(job)
    assert job.status == JobStatus.RUNNING
    sleep.assert_not_called()


def test_api_error_detail_shows_http_status_and_message(db_session):
    now = datetime(2026, 10, 28, 13, tzinfo=timezone.utc)
    job = make_job(db_session, scheduled_for=now - timedelta(minutes=1))

    class Forbidden(Exception):
        status = 403
        reason = "Forbidden"
        body = "jobs.batch gs-1 is forbidden"

    api = Mock()
    api.create_namespaced_job.side_effect = Forbidden()

    results = dispatch_due_jobs(db_session, api, now=now)

    db_session.refresh(job)
    assert results[0].outcome == "error"
    assert "403" in results[0].error_detail
    assert "forbidden" in results[0].error_detail.lower()
    assert job.status == JobStatus.WAITING


def test_non_api_errors_have_no_detail(db_session):
    now = datetime(2026, 10, 28, 13, tzinfo=timezone.utc)
    make_job(db_session, scheduled_for=now - timedelta(minutes=1))
    api = Mock()
    api.create_namespaced_job.side_effect = RuntimeError("temporary API error")

    results = dispatch_due_jobs(db_session, api, now=now)

    assert results[0].error_type == "RuntimeError"
    assert results[0].error_detail is None
