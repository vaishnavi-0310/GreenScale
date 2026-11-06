"""Tests for authenticated CSV uploads and Kubernetes result retrieval."""

import json
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, update
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.db import Base, get_db
from app.main import app
from app.models import Job, JobStatus


@pytest.fixture
def setup_api():
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine, expire_on_commit=False)

    def override_db():
        with Session() as db:
            yield db

    app.dependency_overrides[get_db] = override_db

    client = TestClient(app)
    try:
        yield client, Session
    finally:
        client.close()
        app.dependency_overrides.clear()
        engine.dispose()


def login(client, email="csv@example.com"):
    assert client.post(
        "/auth/register",
        json={
            "name": "CSV User",
            "email": email,
            "password": "test-password-123",
        },
    ).status_code == 201

    assert client.post(
        "/auth/login",
        json={"email": email, "password": "test-password-123"},
    ).status_code == 200


def test_csv_upload_success(setup_api, monkeypatch):
    client, _ = setup_api
    login(client)

    blob = MagicMock()
    storage_client = MagicMock()
    storage_client.bucket.return_value.blob.return_value = blob

    monkeypatch.setattr(
        "google.cloud.storage.Client",
        lambda: storage_client,
    )

    response = client.post(
        "/jobs/upload-csv",
        files={"file": ("sales.csv", b"name,amount\nA,10\n", "text/csv")},
    )

    assert response.status_code == 200
    data = response.json()

    assert data["filename"] == "sales.csv"
    assert data["size_mb"] == 1
    assert data["gcs_uri"].startswith(
        "gs://greenscale-demo-data-group24-512813/uploads/"
    )

    blob.upload_from_string.assert_called_once_with(
        b"name,amount\nA,10\n",
        content_type="text/csv",
        if_generation_match=0,
    )


@pytest.mark.parametrize(
    "filename,content,expected",
    [
        ("notes.txt", b"hello", 400),
        ("empty.csv", b"", 400),
        ("large.csv", b"x" * (20 * 1024 * 1024 + 1), 413),
    ],
)
def test_csv_upload_validation(setup_api, filename, content, expected):
    client, _ = setup_api
    login(client)

    response = client.post(
        "/jobs/upload-csv",
        files={"file": (filename, content, "text/csv")},
    )
    assert response.status_code == expected


def test_csv_upload_requires_login(setup_api):
    client, _ = setup_api

    response = client.post(
        "/jobs/upload-csv",
        files={"file": ("sales.csv", b"a,b\n1,2", "text/csv")},
    )
    assert response.status_code in (401, 403)


def create_completed_csv_job(client, Session):
    response = client.post(
        "/jobs",
        json={
            "name": "CSV result test",
            "description": "Summarize gs://test-bucket/sales.csv",
            "workload_type": "csv_summary",
            "input_size_mb": 1,
            "priority": "high",
            "deadline": (
                datetime.now(timezone.utc) + timedelta(hours=8)
            ).isoformat(),
            "estimated_minutes": 1,
        },
    )
    assert response.status_code == 201
    job_id = response.json()["id"]

    with Session() as db:
        db.execute(
            update(Job)
            .where(Job.id == job_id)
            .values(
                status=JobStatus.SUCCEEDED,
                k8s_job_name=f"gs-{job_id}",
            )
        )
        db.commit()

    return job_id


def test_csv_result_success(setup_api, monkeypatch):
    client, Session = setup_api
    login(client)
    job_id = create_completed_csv_job(client, Session)

    pod = SimpleNamespace(
        metadata=SimpleNamespace(name=f"gs-{job_id}-abc"),
        status=SimpleNamespace(phase="Succeeded"),
    )

    k8s_api = MagicMock()
    k8s_api.list_namespaced_pod.return_value.items = [pod]

    expected = {
        "workload": "csv_summary",
        "rows": 2,
        "columns": 2,
        "status": "SUCCESS",
    }
    k8s_api.read_namespaced_pod_log.return_value = json.dumps(expected)

    monkeypatch.setattr(
        "kubernetes.config.load_kube_config",
        lambda: None,
    )
    monkeypatch.delenv("KUBERNETES_SERVICE_HOST", raising=False)
    monkeypatch.setattr(
        "kubernetes.client.CoreV1Api",
        lambda: k8s_api,
    )

    response = client.get(f"/jobs/{job_id}/result")

    assert response.status_code == 200
    assert response.json() == expected
    k8s_api.read_namespaced_pod_log.assert_called_once()


def test_csv_result_missing_pod(setup_api, monkeypatch):
    client, Session = setup_api
    login(client)
    job_id = create_completed_csv_job(client, Session)

    k8s_api = MagicMock()
    k8s_api.list_namespaced_pod.return_value.items = []

    monkeypatch.setattr(
        "kubernetes.config.load_kube_config",
        lambda: None,
    )
    monkeypatch.delenv("KUBERNETES_SERVICE_HOST", raising=False)
    monkeypatch.setattr(
        "kubernetes.client.CoreV1Api",
        lambda: k8s_api,
    )

    response = client.get(f"/jobs/{job_id}/result")

    assert response.status_code == 404
    assert "Pod not found" in response.json()["detail"]


def test_csv_result_requires_job_owner(setup_api):
    client, Session = setup_api

    login(client, "owner@example.com")
    job_id = create_completed_csv_job(client, Session)

    client.post("/auth/logout")
    login(client, "other@example.com")

    response = client.get(f"/jobs/{job_id}/result")

    assert response.status_code == 404
