import requests
import pytest
from google.genai import errors

from app import llm
from app.predictor import (
    FALLBACK,
    MAX_SECONDS,
    SOURCE_LLM,
    SOURCE_LLM_ERROR,
    SOURCE_NO_KEY,
    predict_runtime,
)

FAKE_KEY = "test-key-should-never-reach-the-model"


class FakeResponse:
    def __init__(self, text):
        self.text = text


class FakeModels:
    def __init__(self, outcomes):
        self.outcomes = list(outcomes)
        self.calls = 0
        self.requests: list[dict] = []

    def generate_content(self, **kwargs):
        self.calls += 1
        self.requests.append(kwargs)
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return FakeResponse(outcome)


class FakeClient:
    def __init__(self, outcomes):
        self.models = FakeModels(outcomes)


def busy_error():
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


@pytest.fixture()
def with_key(monkeypatch):
    monkeypatch.setattr(llm.settings, "gemini_api_key", FAKE_KEY)


@pytest.fixture()
def without_key(monkeypatch):
    monkeypatch.setattr(llm.settings, "gemini_api_key", "")


# --- Fallback when no key is configured -------------------------------------


@pytest.mark.parametrize("workload_type", sorted(FALLBACK))
def test_no_key_returns_fallback_for_each_type(without_key, workload_type):
    assert predict_runtime(workload_type, 10) == (FALLBACK[workload_type], SOURCE_NO_KEY)


def test_no_key_sleep_matches_guide_expectation(without_key):
    assert predict_runtime("sleep", 10) == (60, SOURCE_NO_KEY)


def test_fallback_table_values_are_within_safe_bounds():
    assert all(1 <= seconds <= MAX_SECONDS for seconds in FALLBACK.values())


# --- Programming errors are raised, not swallowed ----------------------------


def test_unknown_workload_type_raises_before_any_call(with_key):
    client = FakeClient(['{"estimated_seconds": 5}'])
    with pytest.raises(ValueError):
        predict_runtime("bitcoin_mining", 10, client=client)
    assert client.models.calls == 0


# --- Successful LLM path -------------------------------------------------------


def test_valid_llm_response_is_used(with_key):
    client = FakeClient(['{"estimated_seconds": 75}'])
    assert predict_runtime("sleep", 10, client=client) == (75, SOURCE_LLM)
    assert client.models.calls == 1


def test_injected_client_is_used_even_without_key(without_key):
    # Dependency injection must bypass the settings check so tests stay offline.
    client = FakeClient(['{"estimated_seconds": 42}'])
    assert predict_runtime("factorial", 10, client=client) == (42, SOURCE_LLM)


def test_prompt_contains_only_metadata_and_never_the_key(with_key):
    client = FakeClient(['{"estimated_seconds": 30}'])
    predict_runtime("csv_summary", 250, client=client)

    sent = client.models.requests[0]
    contents = sent["contents"]
    assert "csv_summary" in contents
    assert "250" in contents
    assert FAKE_KEY not in contents
    assert FAKE_KEY not in str(sent["config"].system_instruction)


def test_requests_structured_json_output(with_key):
    client = FakeClient(['{"estimated_seconds": 30}'])
    predict_runtime("sleep", 1, client=client)

    config = client.models.requests[0]["config"]
    assert config.response_mime_type == "application/json"
    assert config.response_schema is not None
    assert config.temperature == 0


# --- Degraded LLM path falls back ---------------------------------------------


@pytest.mark.parametrize("seconds", [0, -5, MAX_SECONDS + 1, 99_999])
def test_out_of_range_estimate_falls_back(with_key, seconds):
    client = FakeClient([f'{{"estimated_seconds": {seconds}}}'])
    assert predict_runtime("sleep", 10, client=client) == (60, SOURCE_LLM_ERROR)


def test_boundary_values_are_accepted(with_key):
    assert predict_runtime("sleep", 10, client=FakeClient(['{"estimated_seconds": 1}'])) == (
        1,
        SOURCE_LLM,
    )
    assert predict_runtime(
        "sleep", 10, client=FakeClient([f'{{"estimated_seconds": {MAX_SECONDS}}}'])
    ) == (MAX_SECONDS, SOURCE_LLM)


@pytest.mark.parametrize(
    "raw",
    [
        "not json at all",
        '{"estimated_seconds": "ninety"}',
        '{"something_else": 90}',
        "",
    ],
    ids=["garbage", "non-integer", "wrong-key", "empty"],
)
def test_invalid_llm_payload_falls_back(with_key, raw):
    client = FakeClient([raw])
    assert predict_runtime("factorial", 10, client=client) == (90, SOURCE_LLM_ERROR)


def test_server_busy_three_times_falls_back_after_retries(with_key):
    client = FakeClient([busy_error(), busy_error(), busy_error()])
    assert predict_runtime("csv_summary", 10, client=client) == (120, SOURCE_LLM_ERROR)
    assert client.models.calls == llm.MAX_ATTEMPTS


def test_server_busy_then_success_uses_llm_value(with_key):
    client = FakeClient([busy_error(), '{"estimated_seconds": 200}'])
    assert predict_runtime("csv_summary", 10, client=client) == (200, SOURCE_LLM)
    assert client.models.calls == 2


def test_timeout_three_times_falls_back(with_key):
    timeout = requests.exceptions.ReadTimeout("read timed out")
    client = FakeClient([timeout, timeout, timeout])
    assert predict_runtime("sleep", 10, client=client) == (60, SOURCE_LLM_ERROR)
    assert client.models.calls == llm.MAX_ATTEMPTS


def test_unexpected_exception_falls_back(with_key):
    client = FakeClient([RuntimeError("sdk exploded")])
    assert predict_runtime("sleep", 10, client=client) == (60, SOURCE_LLM_ERROR)


def test_fallback_logs_only_exception_type(with_key, caplog):
    client = FakeClient(["not json"])
    with caplog.at_level("WARNING", logger="app.predictor"):
        predict_runtime("sleep", 10, client=client)
    assert any("fell back" in record.getMessage() for record in caplog.records)
    assert FAKE_KEY not in caplog.text
    assert "not json" not in caplog.text
