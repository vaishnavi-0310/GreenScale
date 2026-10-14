from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.db import Base, get_db
from app.llm import LLMError, LLMUnavailableError
from app.main import app
from app.models import Priority
from app.routes import get_extractor
from app.schemas import JobCreate


@pytest.fixture()
def client():
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    TestingSession = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)

    def override_get_db():
        db = TestingSession()
        try:
            yield db
        finally:
            db.close()

    app.dependency_overrides[get_db] = override_get_db
    yield TestClient(app)
    app.dependency_overrides.clear()


def use_extractor(fn):
    app.dependency_overrides[get_extractor] = lambda: fn


def fake_ok(text):
    return JobCreate(
        name="Compress logs",
        description=text,
        priority=Priority.LOW,
        deadline=datetime.now(timezone.utc) + timedelta(hours=10),
        estimated_minutes=20,
    )


def test_from_text_creates_job(client):
    use_extractor(fake_ok)
    r = client.post("/jobs/from-text", json={"text": "compress logs tonight"})
    assert r.status_code == 201
    body = r.json()
    assert body["name"] == "Compress logs"
    assert body["priority"] == "low"
    assert body["status"] == "pending"
    assert body["description"] == "compress logs tonight"

    # and it is really stored
    assert client.get(f"/jobs/{body['id']}").status_code == 200


def test_from_text_llm_unavailable_returns_503(client):
    def boom(text):
        raise LLMUnavailableError("busy")

    use_extractor(boom)
    r = client.post("/jobs/from-text", json={"text": "compress logs"})
    assert r.status_code == 503
    assert r.headers["retry-after"] == "30"
    assert client.get("/jobs").json() == []   # nothing saved


def test_from_text_bad_llm_output_returns_502(client):
    def bad(text):
        raise LLMError("invalid job data")

    use_extractor(bad)
    r = client.post("/jobs/from-text", json={"text": "compress logs"})
    assert r.status_code == 502
    assert client.get("/jobs").json() == []


@pytest.mark.parametrize("text", ["", "   ", "x" * 2001])
def test_from_text_rejects_bad_input(client, text):
    use_extractor(fake_ok)
    r = client.post("/jobs/from-text", json={"text": text})
    assert r.status_code == 422