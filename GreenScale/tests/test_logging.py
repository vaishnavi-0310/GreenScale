import json
import logging

import pytest
from fastapi.testclient import TestClient

from app.logging_config import JsonFormatter
from app.main import app


def make_record(**extra):
    record = logging.LogRecord("greenscale", logging.INFO, __file__, 1, "job created", None, None)
    for key, value in extra.items():
        setattr(record, key, value)
    return record


def test_output_is_valid_json_with_job_id():
    out = json.loads(JsonFormatter().format(make_record(job_id=7, event="job_created")))
    assert out["job_id"] == 7
    assert out["message"] == "job created"
    assert out["level"] == "INFO"


def test_unlisted_fields_are_never_logged():
    line = JsonFormatter().format(make_record(api_key="SECRET-KEY", text="private user text"))
    assert "SECRET-KEY" not in line
    assert "private user text" not in line


class ListHandler(logging.Handler):
    def __init__(self):
        super().__init__()
        self.records = []

    def emit(self, record):
        self.records.append(record)


@pytest.fixture()
def captured():
    handler = ListHandler()
    logger = logging.getLogger("greenscale")
    logger.addHandler(handler)
    yield handler.records
    logger.removeHandler(handler)


def test_request_is_logged_without_query_string(captured):
    TestClient(app).get("/nope?token=abc123")
    rec = next(r for r in captured if getattr(r, "event", "") == "http_request")
    assert rec.path == "/nope"
    assert rec.status_code == 404
    assert "abc123" not in JsonFormatter().format(rec)