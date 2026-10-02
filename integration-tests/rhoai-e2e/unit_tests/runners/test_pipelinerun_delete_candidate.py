"""Unit tests for pipelinerun_delete_candidate and try_cancel_pipelinerun."""

from __future__ import annotations

from suite.constants import DEFAULT_APP, DEFAULT_NAMESPACE
import unittest
from unittest.mock import MagicMock, patch

from runners.cli.runner_support import pipelinerun_delete_candidate, try_cancel_pipelinerun

def _pr(
    name: str,
    *,
    app: str = DEFAULT_APP,
    reason: str = "",
    owner: str = "",
    completion_time: str = "",
    snapshot: str = "",
    child_refs: list | None = None,
    pipeline_label: str = "rhoai-e2e-test",
) -> dict:
    body: dict = {
        "metadata": {
            "name": name,
            "labels": {
                "appstudio.openshift.io/application": app,
                "tekton.dev/pipeline": pipeline_label,
            },
            "annotations": {},
        },
        "status": {
            "conditions": [{"type": "Succeeded", "status": "Unknown", "reason": reason}],
        },
        "spec": {"params": []},
    }
    if owner:
        body["metadata"]["annotations"]["rhoai-e2e.run-owner"] = owner
    if completion_time:
        body["status"]["completionTime"] = completion_time
    if snapshot:
        body["spec"]["params"] = [{"name": "SNAPSHOT", "value": snapshot}]
    if child_refs is not None:
        body["status"]["childReferences"] = child_refs
    return body

class PipelinerunDeleteCandidateTest(unittest.TestCase):
    def test_pending_incomplete(self) -> None:
        item = _pr("e2e-cli-testops-x", reason="PipelineRunPending")
        ok, why = pipelinerun_delete_candidate(item, app=DEFAULT_APP, run_owner="alice")
        self.assertTrue(ok)
        self.assertEqual(why, "pending")

    def test_resolving_pipeline_ref_pending(self) -> None:
        item = _pr("e2e-cli-testops-x", reason="ResolvingPipelineRef")
        ok, why = pipelinerun_delete_candidate(item, app=DEFAULT_APP, run_owner="alice")
        self.assertTrue(ok)
        self.assertEqual(why, "pending")

    def test_owned_incomplete(self) -> None:
        item = _pr("e2e-cli-testops-x", reason="ResolvingTaskRef", owner="alice")
        ok, why = pipelinerun_delete_candidate(item, app=DEFAULT_APP, run_owner="alice")
        self.assertTrue(ok)
        self.assertEqual(why, "owned")

    def test_owned_via_snapshot_when_pr_annotation_missing(self) -> None:
        item = _pr(
            "e2e-cli-testops-x",
            reason="Running",
            snapshot="snap-alice-1",
        )
        ok, why = pipelinerun_delete_candidate(
            item,
            app=DEFAULT_APP,
            run_owner="alice",
            snapshot_owner="alice",
        )
        self.assertTrue(ok)
        self.assertEqual(why, "owned")

    def test_stuck_running_without_owner_or_tasks_skipped_by_default(self) -> None:
        item = _pr("e2e-cli-testops-x", reason="Running", child_refs=[])
        ok, why = pipelinerun_delete_candidate(item, app=DEFAULT_APP, run_owner="alice")
        self.assertFalse(ok)
        self.assertEqual(why, "")

    def test_include_unowned_stuck_selects_stuck_runs(self) -> None:
        item = _pr("e2e-cli-testops-x", reason="Running", child_refs=[])
        ok, why = pipelinerun_delete_candidate(
            item,
            app=DEFAULT_APP,
            run_owner="alice",
            include_unowned_stuck=True,
        )
        self.assertTrue(ok)
        self.assertEqual(why, "stuck-no-tasks")

    def test_skip_other_users_running_with_tasks(self) -> None:
        item = _pr(
            "e2e-cli-testops-x",
            reason="Running",
            owner="bob",
            child_refs=[{"name": "tr-1", "kind": "TaskRun"}],
        )
        ok, _ = pipelinerun_delete_candidate(item, app=DEFAULT_APP, run_owner="alice")
        self.assertFalse(ok)

    def test_skip_owned_running_with_tasks(self) -> None:
        item = _pr(
            "e2e-cli-testops-x",
            reason="Running",
            owner="alice",
            child_refs=[{"name": "tr-1", "kind": "TaskRun"}],
        )
        ok, _ = pipelinerun_delete_candidate(item, app=DEFAULT_APP, run_owner="alice")
        self.assertFalse(ok)

    def test_stop_owned_running_includes_active_owned(self) -> None:
        item = _pr(
            "e2e-cli-testops-x",
            reason="Running",
            owner="alice",
            child_refs=[{"name": "tr-1", "kind": "TaskRun"}],
        )
        ok, why = pipelinerun_delete_candidate(
            item,
            app=DEFAULT_APP,
            run_owner="alice",
            stop_owned_running=True,
        )
        self.assertTrue(ok)
        self.assertEqual(why, "owned")

    def test_skip_completed(self) -> None:
        item = _pr(
            "e2e-cli-testops-x",
            reason="Failed",
            owner="alice",
            completion_time="2026-06-17T10:00:00Z",
        )
        ok, _ = pipelinerun_delete_candidate(item, app=DEFAULT_APP, run_owner="alice")
        self.assertFalse(ok)

    def test_skip_wrong_app_label(self) -> None:
        item = _pr("e2e-cli-testops-x", reason="PipelineRunPending", app="other-app")
        ok, _ = pipelinerun_delete_candidate(item, app=DEFAULT_APP, run_owner="alice")
        self.assertFalse(ok)

    def test_pending_without_app_label(self) -> None:
        item = _pr("e2e-cli-testops-x", reason="PipelineRunPending")
        item["metadata"]["labels"].pop("appstudio.openshift.io/application")
        ok, why = pipelinerun_delete_candidate(item, app=DEFAULT_APP, run_owner="alice")
        self.assertFalse(ok)
        self.assertEqual(why, "")

    def test_skip_non_rhoai_e2e(self) -> None:
        item = _pr("other-pipeline-x", reason="PipelineRunPending")
        ok, _ = pipelinerun_delete_candidate(item, app=DEFAULT_APP, run_owner="alice")
        self.assertFalse(ok)

    def test_skip_smoke_only_stuck(self) -> None:
        item = _pr(
            "odh-olminstall-smoke-testops-x",
            reason="Running",
            child_refs=[],
            pipeline_label="rhoai-e2e-smoke-test",
        )
        ok, _ = pipelinerun_delete_candidate(item, app=DEFAULT_APP, run_owner="alice")
        self.assertFalse(ok)

    def test_smoke_gate_name_is_not_legacy_smoke_pipeline(self) -> None:
        from suite.constants import rhoai_e2e_smoke_only_pipelinerun, DEFAULT_APP, DEFAULT_NAMESPACE

        self.assertTrue(rhoai_e2e_smoke_only_pipelinerun("olminstall-smoke-nmanos-abc12"))
        self.assertTrue(rhoai_e2e_smoke_only_pipelinerun("odh-olminstall-smoke-testops-abc12"))

class TryCancelPipelinerunTest(unittest.TestCase):
    @patch("runners.cli.runner_support.shutil.which", return_value=None)
    def test_missing_tkn(self, _which: MagicMock) -> None:
        ok, detail = try_cancel_pipelinerun("pr-1", "ns")
        self.assertFalse(ok)
        self.assertEqual(detail, "tkn not in PATH")

    @patch("runners.cli.runner_support.run_cmd")
    @patch("runners.cli.runner_support.shutil.which", return_value="/usr/bin/tkn")
    def test_cancel_success(self, _which: MagicMock, run_cmd: MagicMock) -> None:
        run_cmd.return_value = MagicMock(returncode=0, stdout="Cancelled", stderr="")
        ok, detail = try_cancel_pipelinerun("pr-1", "ns")
        self.assertTrue(ok)
        self.assertEqual(detail, "Cancelled")
        run_cmd.assert_called_once()
        self.assertEqual(run_cmd.call_args.args[0][:4], ["tkn", "pipelinerun", "cancel", "pr-1"])

    @patch("runners.cli.runner_support.run_cmd")
    @patch("runners.cli.runner_support.shutil.which", return_value="/usr/bin/tkn")
    def test_cancel_fallback_without_grace(self, _which: MagicMock, run_cmd: MagicMock) -> None:
        run_cmd.side_effect = [
            MagicMock(returncode=1, stdout="", stderr="grace unsupported"),
            MagicMock(returncode=0, stdout="ok", stderr=""),
        ]
        ok, detail = try_cancel_pipelinerun("pr-1", "ns")
        self.assertTrue(ok)
        self.assertEqual(detail, "ok")
        self.assertEqual(run_cmd.call_count, 2)

