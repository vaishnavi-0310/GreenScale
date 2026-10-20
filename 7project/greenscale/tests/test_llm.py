from datetime import datetime, timedelta, timezone
import requests

import pytest
from google.genai import errors

from app import llm
from app.llm import LLMError, LLMUnavailableError, extract_job
from app.models import Priority

NOW = datetime.now(timezone.utc)

GOOD = (
    '{"name": "Compress logs", "priority": "low", '
    f'"estimated_minutes": 20, "deadline": "{(NOW + timedelta(days=1)).isoformat()}"}}'
)


class FakeResponse:
    def __init__(self, text):
        self.text = text


class FakeModels:
    def __init__(self, outcomes):
        self.outcomes = list(outcomes)
        self.calls = 0

    def generate_content(self, **kwargs):
        self.calls += 1
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return FakeResponse(outcome)


class FakeClient:
    def __init__(self, outcomes):
        self.models = FakeModels(outcomes)


def busy_error():
    # Build a 503 APIError without depending on the SDK's constructor,
    # which changes between google-genai versions.
    err = errors.ServerError.__new__(errors.ServerError)
    Exception.__init__(err, "503 UNAVAILABLE: busy")
    err.code = 503
    err.status = "UNAVAILABLE"
    err.message = "busy"
    err.details = {}
    err.response = None
    return err


@pytest.fixture(autouse=True)
def no_sleep(monkeypatch):
    monkeypatch.setattr(llm.time, "sleep", lambda s: None)


def test_extracts_valid_job():
    job = extract_job("compress logs tonight, no rush", now=NOW, client=FakeClient([GOOD]))
    assert job.name == "Compress logs"
    assert job.priority == Priority.LOW
    assert job.estimated_minutes == 20
    assert job.description == "compress logs tonight, no rush"
    assert job.deadline.tzinfo is not None


def test_retries_then_succeeds():
    client = FakeClient([busy_error(), GOOD])
    job = extract_job("compress logs", now=NOW, client=client)
    assert job.name == "Compress logs"
    assert client.models.calls == 2


def test_gives_up_after_max_attempts():
    client = FakeClient([busy_error(), busy_error(), busy_error()])
    with pytest.raises(LLMUnavailableError):
        extract_job("compress logs", now=NOW, client=client)
    assert client.models.calls == 3


def test_invalid_json_raises():
    with pytest.raises(LLMError):
        extract_job("x", now=NOW, client=FakeClient(["not json at all"]))


def test_invalid_priority_raises():
    bad = GOOD.replace('"low"', '"urgent"')
    with pytest.raises(LLMError):
        extract_job("x", now=NOW, client=FakeClient([bad]))


def test_deadline_too_soon_raises():
    soon = (
        '{"name": "t", "priority": "low", "estimated_minutes": 600, '
        '"deadline": "2000-01-01T00:00:00Z"}'
    )
    with pytest.raises(LLMError):
        extract_job("x", now=NOW, client=FakeClient([soon]))


def test_empty_description_raises():
    with pytest.raises(LLMError):
        extract_job("   ", now=NOW, client=FakeClient([GOOD]))


def test_empty_response_raises():
    with pytest.raises(LLMError):
        extract_job("x", now=NOW, client=FakeClient([""]))


def test_missing_api_key_raises(monkeypatch):
    monkeypatch.setattr(llm.settings, "gemini_api_key", "")
    with pytest.raises(LLMError):
        extract_job("compress logs", now=NOW)


def test_timeout_is_retried_then_reported_unavailable():
    timeout = requests.exceptions.ReadTimeout("read timed out")
    client = FakeClient([timeout, timeout, timeout])
    with pytest.raises(LLMUnavailableError):
        extract_job("compress logs", now=NOW, client=client)
    assert client.models.calls == 3