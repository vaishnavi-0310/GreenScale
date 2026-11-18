from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.db import Base, get_db
from app.main import app


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
    yield client
    app.dependency_overrides.clear()


def make_payload(**overrides):
    payload = {
        "name": "nightly backup",
        "priority": "low",
        "deadline": (datetime.now(timezone.utc) + timedelta(hours=8)).isoformat(),
        "estimated_minutes": 30,
    }
    payload.update(overrides)
    return payload


def test_create_job(client):
    r = client.post("/jobs", json=make_payload())
    assert r.status_code == 201
    body = r.json()
    assert body["id"] == 1
    assert body["status"] == "pending"
    assert body["priority"] == "low"


def test_get_job_by_id(client):
    created = client.post("/jobs", json=make_payload()).json()
    r = client.get(f"/jobs/{created['id']}")
    assert r.status_code == 200
    assert r.json()["name"] == "nightly backup"


def test_get_missing_job_returns_404(client):
    assert client.get("/jobs/999").status_code == 404


def test_list_jobs_and_status_filter(client):
    client.post("/jobs", json=make_payload(name="a"))
    client.post("/jobs", json=make_payload(name="b"))

    assert len(client.get("/jobs").json()) == 2
    assert len(client.get("/jobs?status=pending").json()) == 2
    assert client.get("/jobs?status=running").json() == []


def test_rejects_deadline_in_the_past(client):
    past = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
    r = client.post("/jobs", json=make_payload(deadline=past))
    assert r.status_code == 422


def test_rejects_deadline_shorter_than_runtime(client):
    soon = (datetime.now(timezone.utc) + timedelta(minutes=5)).isoformat()
    r = client.post("/jobs", json=make_payload(deadline=soon, estimated_minutes=60))
    assert r.status_code == 422


@pytest.mark.parametrize("minutes", [0, -5, 5000])
def test_rejects_bad_duration(client, minutes):
    r = client.post("/jobs", json=make_payload(estimated_minutes=minutes))
    assert r.status_code == 422


def test_rejects_invalid_priority(client):
    r = client.post("/jobs", json=make_payload(priority="urgent"))
    assert r.status_code == 422

def test_create_and_retrieve_workload_metadata(client):
    # Step 1: Prepare a job with workload metadata
    payload = make_payload(
        name="CSV Processing Test",
        workload_type="csv_summary",
        input_size_mb=10,
    )

    # Step 2: Create the job through FastAPI
    response = client.post("/jobs", json=payload)

    assert response.status_code == 201

    created_job = response.json()

    # Step 3: Verify the API creation response
    assert created_job["workload_type"] == "csv_summary"
    assert created_job["input_size_mb"] == 10

    # Step 4: Retrieve the job using its ID
    job_id = created_job["id"]

    get_response = client.get(f"/jobs/{job_id}")

    assert get_response.status_code == 200

    stored_job = get_response.json()

    # Step 5: Verify the saved metadata
    assert stored_job["workload_type"] == "csv_summary"
    assert stored_job["input_size_mb"] == 10

# invalid metadata API test
def test_api_rejects_invalid_workload_metadata(client):
    payload = make_payload(
        workload_type="video_editing",
        input_size_mb=10,
    )

    response = client.post("/jobs", json=payload)

    assert response.status_code == 422