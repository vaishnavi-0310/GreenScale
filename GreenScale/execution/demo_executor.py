"""Show the executor lifecycle with a mocked Kubernetes API."""

from types import SimpleNamespace
from unittest.mock import Mock

from execution.job_executor import get_job_status, submit_once


def main() -> None:
    api = Mock()
    request = {
        "job_id": "demo-001",
        "namespace": "greenscale",
        "image": "busybox:1.36",
        "command": ["echo", "GreenScale demo"],
        "scheduled_start_utc": "2026-10-24T12:00:00Z",
    }

    print(f"Submission: {submit_once(api, request)}")
    job_name = "gs-demo-001"
    api.read_namespaced_job.return_value = SimpleNamespace(
        status=SimpleNamespace(succeeded=0, failed=0, active=1, conditions=[])
    )
    print(f"Status: {get_job_status(api, job_name, request['namespace'])}")
    api.read_namespaced_job.return_value = SimpleNamespace(
        status=SimpleNamespace(succeeded=1, failed=0, active=0, conditions=[])
    )
    print(f"Status: {get_job_status(api, job_name, request['namespace'])}")


if __name__ == "__main__":
    main()
