"""Unit tests for TaskRun failure detail when step logs are empty."""

from __future__ import annotations

from runners.cli.runner_support import format_taskrun_failure_detail


def test_format_taskrun_failure_detail_image_pull() -> None:
    tr = {
        "status": {
            "conditions": [
                {
                    "type": "Succeeded",
                    "status": "False",
                    "reason": "Failed",
                    "message": (
                        "failed to create task run pod "
                        '"olminstal0937-test-workbench-images-pod": '
                        "Back-off pulling image "
                        '"quay.io/opendatahub/workbench-images-tests:latest"'
                    ),
                }
            ],
            "steps": [
                {"name": "orchestrate", "terminationReason": "TaskRunImagePullFailed"},
                {"name": "run", "terminationReason": "TaskRunImagePullFailed"},
            ],
        }
    }
    pod = {
        "status": {
            "containerStatuses": [
                {
                    "name": "step-run",
                    "state": {
                        "waiting": {
                            "reason": "ImagePullBackOff",
                            "message": "Back-off pulling image quay.io/opendatahub/workbench-images-tests:latest",
                        }
                    },
                }
            ]
        }
    }
    detail = format_taskrun_failure_detail(tr, pod=pod)
    assert "TaskRun condition reason: Failed" in detail
    assert "Back-off pulling image" in detail
    assert "Step termination: TaskRunImagePullFailed" in detail
    assert "Pod step-run: ImagePullBackOff" in detail


def test_format_taskrun_failure_detail_empty_fallback() -> None:
    assert format_taskrun_failure_detail({}) == "(no step logs — TaskRun did not execute any steps)"
