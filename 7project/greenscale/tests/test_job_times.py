from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import Mock

from execution.job_times import get_job_times

START = datetime(2026, 10, 30, 0, 14, 19, tzinfo=timezone.utc)
END = datetime(2026, 10, 30, 0, 14, 22, tzinfo=timezone.utc)


def api_returning(**status_fields):
    fields = {"start_time": None, "completion_time": None, "conditions": []}
    fields.update(status_fields)
    api = Mock()
    api.read_namespaced_job.return_value = SimpleNamespace(
        status=SimpleNamespace(**fields)
    )
    return api


def test_returns_kubernetes_start_and_completion_times():
    api = api_returning(start_time=START, completion_time=END)
    assert get_job_times(api, "gs-9", "greenscale") == (START, END)


def test_running_job_has_start_but_no_finish():
    api = api_returning(start_time=START)
    assert get_job_times(api, "gs-9", "greenscale") == (START, None)


def test_failed_job_uses_failed_condition_time():
    failed = SimpleNamespace(type="Failed", status="True", last_transition_time=END)
    api = api_returning(start_time=START, conditions=[failed])
    assert get_job_times(api, "gs-9", "greenscale") == (START, END)


def test_values_that_are_not_datetimes_are_ignored():
    api = api_returning(start_time="not a date", completion_time=12345)
    assert get_job_times(api, "gs-9", "greenscale") == (None, None)


def test_reads_the_job_without_needing_jobs_status_permission():
    api = api_returning(start_time=START, completion_time=END)
    get_job_times(api, "gs-9", "greenscale")
    api.read_namespaced_job.assert_called_once_with(name="gs-9", namespace="greenscale")
    api.read_namespaced_job_status.assert_not_called()
