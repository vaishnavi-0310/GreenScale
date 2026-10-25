from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from execution.job_executor import build_job_spec, get_job_status, submit_job

REQ = {
    "job_id": "demo-001",
    "image": "busybox:1.36",
    "command": ["sh", "-c", "echo hi"],
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


@pytest.mark.parametrize("status, expected", [
    (SimpleNamespace(succeeded=1, failed=0, active=0, conditions=[]), "succeeded"),
    (SimpleNamespace(succeeded=0, failed=0, active=1, conditions=[]), "running"),
    (SimpleNamespace(succeeded=0, failed=0, active=0, conditions=[]), "pending"),
])
def test_maps_job_counters_to_states(status, expected):
    api = Mock()
    api.read_namespaced_job_status.return_value.status = status
    assert get_job_status(api, "gs-demo-001", "greenscale") == expected


def test_terminal_failed_condition_wins_over_retryable_failed_counter():
    api = Mock()
    api.read_namespaced_job_status.return_value.status = SimpleNamespace(
        succeeded=0, failed=1, active=0,
        conditions=[SimpleNamespace(type="Failed", status="True")],
    )
    assert get_job_status(api, "gs-demo-001", "greenscale") == "failed"


def test_failed_pod_counter_without_terminal_condition_remains_pending():
    api = Mock()
    api.read_namespaced_job_status.return_value.status = SimpleNamespace(
        succeeded=0, failed=1, active=0, conditions=[]
    )
    assert get_job_status(api, "gs-demo-001", "greenscale") == "pending"


@pytest.mark.parametrize("field, value", [
    ("job_id", "Demo-001"),
    ("job_id", "../bad"),
    ("job_id", "a" * 61),
    ("namespace", "Bad Namespace"),
    ("image", "busybox:1.36;rm -rf /"),
    ("command", "echo hi"),
    ("command", ["echo", ""]),
])
def test_rejects_invalid_request_values(field, value):
    req = {**REQ, field: value}
    with pytest.raises(ValueError):
        build_job_spec(req)


def test_requires_core_contract_fields():
    req = {key: value for key, value in REQ.items() if key != "image"}
    with pytest.raises(ValueError, match="image"):
        build_job_spec(req)
