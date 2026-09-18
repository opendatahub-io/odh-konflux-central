"""Unit tests for pipeline_task_state helpers."""

from __future__ import annotations

import unittest

from steps.pipeline_task_state import pipeline_task_execution_state, require_pipeline_tasks_ran


def _taskrun(task: str, *, status: str = "True", reason: str = "Succeeded") -> dict:
    return {
        "metadata": {"labels": {"tekton.dev/pipelineTask": task}},
        "status": {
            "conditions": [{"type": "Succeeded", "status": status, "reason": reason}],
        },
    }


class PipelineTaskStateTest(unittest.TestCase):
    def test_taskrun_succeeded(self) -> None:
        runs = [_taskrun("install-rhoai")]
        state, _ = pipeline_task_execution_state("install-rhoai", taskruns=runs)
        self.assertEqual(state, "succeeded")

    def test_taskrun_failed_by_status_false(self) -> None:
        runs = [_taskrun("install-rhoai", status="False", reason="Failed")]
        state, _ = pipeline_task_execution_state("install-rhoai", taskruns=runs)
        self.assertEqual(state, "failed")

    def test_taskrun_running_by_status_unknown(self) -> None:
        runs = [_taskrun("install-rhoai", status="Unknown", reason="Running")]
        state, _ = pipeline_task_execution_state("install-rhoai", taskruns=runs)
        self.assertEqual(state, "running")

    def test_skipped_from_pipelinerun_status(self) -> None:
        pr = {
            "status": {
                "skippedTasks": [
                    {
                        "name": "install-dep-operators",
                        "reason": "When Expressions evaluated to false",
                    }
                ]
            }
        }
        state, detail = pipeline_task_execution_state(
            "install-dep-operators",
            taskruns=[],
            pr_doc=pr,
        )
        self.assertEqual(state, "skipped")
        self.assertIn("When Expressions", detail)

    def test_require_pipeline_tasks_ran_errors_on_skipped(self) -> None:
        pr = {
            "status": {
                "skippedTasks": [
                    {"name": "install-rhoai", "reason": "When Expressions evaluated to false"}
                ]
            }
        }
        state, detail = pipeline_task_execution_state(
            "install-rhoai",
            taskruns=[],
            pr_doc=pr,
        )
        self.assertEqual(state, "skipped")
        self.assertIn("When Expressions", detail)


if __name__ == "__main__":
    unittest.main()
