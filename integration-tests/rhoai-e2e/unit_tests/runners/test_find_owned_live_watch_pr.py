"""find_owned_live_watch_pr must ignore completed PipelineRuns."""

from __future__ import annotations

from unittest import mock

from runners.cli.runner_mixin_list import RunnerListMixin


class _ListHarness(RunnerListMixin):
    def __init__(self, items: list[dict]) -> None:
        self.args = mock.Mock(namespace="rhoai-tenant", app="rhoai-fbc-fragment-ocp-420")
        self.run_owner = "nmanos"
        self._items = items

    def get_pipelineruns(self, _namespace: str) -> list[dict]:
        return self._items


def _pr(*, name: str, completed: bool, owner: str = "nmanos") -> dict:
    status: dict = {}
    if completed:
        status["completionTime"] = "2026-07-16T10:29:00Z"
        status["conditions"] = [
            {"type": "Succeeded", "status": "False", "reason": "Failed"},
        ]
    else:
        status["conditions"] = [
            {"type": "Succeeded", "status": "Unknown", "reason": "Running"},
        ]
    return {
        "metadata": {
            "name": name,
            "creationTimestamp": "2026-07-16T09:00:00Z",
            "labels": {
                "appstudio.openshift.io/application": "rhoai-fbc-fragment-ocp-420",
                "tekton.dev/pipeline": "rhoai-e2e-test",
            },
            "annotations": {"rhoai-e2e.run-owner": owner},
        },
        "spec": {"params": []},
        "status": status,
    }


def test_find_owned_live_watch_pr_skips_completed_failed() -> None:
    harness = _ListHarness(
        [
            _pr(name="e2e-cli-nmanos-rhoai-nmanos-konflux1-bvt-smoke-fmk75", completed=True),
            _pr(name="e2e-cli-nmanos-rhoai-nmanos-konflux1-bvt-smoke-alive", completed=False),
        ]
    )
    assert harness.find_owned_live_watch_pr() == "e2e-cli-nmanos-rhoai-nmanos-konflux1-bvt-smoke-alive"


def test_find_owned_live_watch_pr_empty_when_only_completed() -> None:
    harness = _ListHarness(
        [_pr(name="e2e-cli-nmanos-rhoai-nmanos-konflux1-bvt-smoke-fmk75", completed=True)]
    )
    assert harness.find_owned_live_watch_pr() == ""
