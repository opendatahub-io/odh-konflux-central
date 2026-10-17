"""Unit tests for pipelinerun_list_state (-l STATE column)."""

from __future__ import annotations

import unittest

from runners.cli.runner_support import pipelinerun_list_state

def _pr(*, status: str, reason: str = "", completion_time: str = "", child_refs: list | None = None) -> dict:
    body: dict = {
        "status": {
            "conditions": [{"type": "Succeeded", "status": status, "reason": reason}],
        }
    }
    if completion_time:
        body["status"]["completionTime"] = completion_time
    if child_refs is not None:
        body["status"]["childReferences"] = child_refs
    return body

class PipelinerunListStateTest(unittest.TestCase):
    def test_succeeded(self) -> None:
        self.assertEqual(pipelinerun_list_state(_pr(status="True")), "completed")

    def test_failed(self) -> None:
        self.assertEqual(pipelinerun_list_state(_pr(status="False", reason="Failed")), "failed")

    def test_pending(self) -> None:
        self.assertEqual(
            pipelinerun_list_state(_pr(status="Unknown", reason="PipelineRunPending")),
            "pending",
        )

    def test_resolving_task_ref_is_failed_not_running(self) -> None:
        self.assertEqual(
            pipelinerun_list_state(_pr(status="Unknown", reason="ResolvingTaskRef")),
            "failed",
        )

    def test_running_with_child_refs(self) -> None:
        self.assertEqual(
            pipelinerun_list_state(
                _pr(status="Unknown", reason="Running", child_refs=[{"name": "tr1"}])
            ),
            "running",
        )

