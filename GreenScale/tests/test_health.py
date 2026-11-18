import pytest
from fastapi.testclient import TestClient
from sqlalchemy.exc import OperationalError

from app.db import get_db
from app.main import app


class OkSession:
    def execute(self, *args, **kwargs):
        return None

    def close(self):
        pass


class BrokenSession:
    def execute(self, *args, **kwargs):
        raise OperationalError("SELECT 1", {}, Exception("password=hunter2 host=10.0.0.5"))

    def close(self):
        pass


@pytest.fixture()
def client():
    yield TestClient(app)
    app.dependency_overrides.clear()


def use_session(session):
    def override():
        yield session

    app.dependency_overrides[get_db] = override


def test_health_does_not_need_database(client):
    use_session(BrokenSession())
    r = client.get("/health")
    assert r.status_code == 200
    assert r.json() == {"status": "ok"}


def test_ready_ok_when_database_up(client):
    use_session(OkSession())
    r = client.get("/ready")
    assert r.status_code == 200
    assert r.json()["database"] == "up"


def test_ready_503_when_database_down(client):
    use_session(BrokenSession())
    r = client.get("/ready")
    assert r.status_code == 503
    assert r.json()["database"] == "down"


def test_ready_does_not_leak_error_details(client):
    use_session(BrokenSession())
    r = client.get("/ready")
    assert "hunter2" not in r.text
    assert "10.0.0.5" not in r.text