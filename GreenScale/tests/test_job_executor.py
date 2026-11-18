from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from execution.job_executor import (
    build_job_spec,
    get_job_status,
    is_terminal_failed,
    submit_job,
    submit_once,
)

REQ = {
    "job_id": "demo-001",
    "image": "busybox:1.36",
    "command": ["echo", "GreenScale"],
    "namespace": "greenscale",
    "scheduled_start_utc": "2026-10-24T12:00:00Z",
}


def test_builds_batch_v1_job_with_deterministic_name_and_expiry():
    spec = build_job_spec(REQ)
    assert spec["apiVersion"] == "batch/v1"
    assert spec["kind"] == "Job"
    assert spec["metadata"]["name"] == "gs-demo-001"
    assert spec["metadata"]["labels"]["job-id"] == "demo-001"
    assert spec["spec"]["backoffLimit"] == 1
    assert spec["spec"]["ttlSecondsAfterFinished"] == 3600
    pod = spec["spec"]["template"]["spec"]
    assert pod["restartPolicy"] == "Never"
    assert pod["containers"] == [{"name": "worker", "image": "busybox:1.36", "command": REQ["command"]}]


def test_manifest_builder_does_not_mutate_request():
    req = {**REQ, "command": list(REQ["command"])}
    before = {**req, "command": list(req["command"])}
    build_job_spec(req)
    assert req == before


def test_submit_calls_batch_api_with_expected_namespace_and_manifest():
    api = Mock()
    result = submit_job(api, REQ)
    api.create_namespaced_job.assert_called_once_with(
        namespace="greenscale", body=build_job_spec(REQ)
    )
    assert result is api.create_namespaced_job.return_value


def test_submit_once_returns_submitted_on_success():
    api = Mock()
    assert submit_once(api, REQ) == "submitted"
    api.create_namespaced_job.assert_called_once()


class ApiError(Exception):
    def __init__(self, status):
        self.status = status


def test_submit_once_returns_already_exists_for_http_409():
    api = Mock()
    api.create_namespaced_job.side_effect = ApiError(409)
    assert submit_once(api, REQ) == "already_exists"
    api.create_namespaced_job.assert_called_once_with(
        namespace="greenscale", body=build_job_spec(REQ)
    )


def test_submit_once_reraises_non_conflict_api_errors():
    api = Mock()
    api.create_namespaced_job.side_effect = ApiError(403)
    with pytest.raises(ApiError) as raised:
        submit_once(api, REQ)
    assert raised.value.status == 403


@pytest.mark.parametrize("status, expected", [
    (SimpleNamespace(succeeded=1, failed=0, active=0, conditions=[]), "succeeded"),
    (SimpleNamespace(succeeded=0, failed=0, active=1, conditions=[]), "running"),
    (SimpleNamespace(succeeded=0, failed=0, active=0, conditions=[]), "pending"),
])
def test_maps_job_counters_to_states(status, expected):
    api = Mock()
    api.read_namespaced_job.return_value.status = status
    assert get_job_status(api, "gs-demo-001", "greenscale") == expected


def test_complete_condition_maps_to_success():
    api = Mock()
    api.read_namespaced_job.return_value.status = SimpleNamespace(
        succeeded=0, failed=0, active=0,
        conditions=[SimpleNamespace(type="Complete", status="True")],
    )
    assert get_job_status(api, "gs-demo-001", "greenscale") == "succeeded"


def test_terminal_failed_condition_wins_over_retryable_failed_counter():
    api = Mock()
    job = SimpleNamespace(status=SimpleNamespace(
        succeeded=0, failed=1, active=0,
        conditions=[SimpleNamespace(type="Failed", status="True")],
    ))
    api.read_namespaced_job.return_value = job
    assert is_terminal_failed(job)
    assert get_job_status(api, "gs-demo-001", "greenscale") == "failed"


def test_failed_pod_counter_without_terminal_condition_remains_pending():
    api = Mock()
    api.read_namespaced_job.return_value.status = SimpleNamespace(
        succeeded=0, failed=1, active=0, conditions=[]
    )
    assert get_job_status(api, "gs-demo-001", "greenscale") == "pending"


def test_active_job_with_retryable_failed_pod_is_running():
    api = Mock()
    api.read_namespaced_job.return_value.status = SimpleNamespace(
        succeeded=0, failed=1, active=1, conditions=[]
    )
    assert get_job_status(api, "gs-demo-001", "greenscale") == "running"


@pytest.mark.parametrize("field, value", [
    ("job_id", "Demo-001"),
    ("job_id", "../bad"),
    ("job_id", "a" * 53),
    ("namespace", "Bad Namespace"),
    ("namespace", "a" * 64),
    ("image", "untrusted/image:latest"),
    ("image", ["busybox:1.36"]),
    ("command", "echo hi"),
    ("command", ["sh", "-c", "touch /tmp/pwned"]),
    ("command", ["echo", ""]),
    ("command", ["echo", "x" * 1025]),
    ("command", ["echo", "x\x00y"]),
    ("scheduled_start_utc", "2026-10-24T12:00:00"),
    ("scheduled_start_utc", "2026-10-24T12:00:00+01:00"),
])
def test_rejects_invalid_or_disallowed_request_values(field, value):
    req = {**REQ, field: value}
    with pytest.raises(ValueError):
        build_job_spec(req)


def test_requires_core_contract_fields():
    req = {key: value for key, value in REQ.items() if key != "image"}
    with pytest.raises(ValueError, match="image"):
        build_job_spec(req)


def test_scheduled_time_must_be_text():
    req = {**REQ, "scheduled_start_utc": 1792843200}
    with pytest.raises(ValueError, match="ISO 8601 UTC"):
        build_job_spec(req)
