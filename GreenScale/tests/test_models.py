from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.db import Base
from app.models import Job, JobStatus, Priority


@pytest.fixture()
def session():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    with sessionmaker(bind=engine)() as s:
        yield s


def test_job_defaults(session):
    job = Job(
        name="nightly backup",
        deadline=datetime.now(timezone.utc) + timedelta(hours=8),
        estimated_minutes=30,
    )
    session.add(job)
    session.commit()

    assert job.id is not None
    assert job.status == JobStatus.PENDING
    assert job.priority == Priority.MEDIUM
    assert job.created_at is not None
    assert job.started_at is None