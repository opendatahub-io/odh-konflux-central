"""Unit tests for hollow-green gate checks in publish-results."""

from __future__ import annotations

import os
import unittest
from unittest.mock import patch

from runners.report.check_requested_gates_ran import (
    collect_hollow_green_failures,
    format_install_blocked_publish_note,
    main as check_main,
    upstream_blocked_test_gates,
)
from suite.conforma_gate import CONFORMA_GATE_SKIP


class CheckRequestedGatesRanTest(unittest.TestCase):
    @patch("runners.report.check_requested_gates_ran.require_pipeline_tasks_ran", return_value=[])
    def test_passes_when_no_test_gates(self, _mock: object) -> None:
        with patch.dict(os.environ, {"TEST_GATES": ""}, clear=False):
            self.assertEqual(check_main(), 0)

    def test_passes_when_conforma_gate_skip(self) -> None:
        env = {
            "TEST_GATES": "bvt,smoke",
            "PRODUCT": "rhoai",
            "CONFORMA_GATE": CONFORMA_GATE_SKIP,
            "CONFORMA_GATE_DETAIL": (
                "Skipped: snapshot catalog line '2.25' below MIN_RHOAI_VERSION 3.5 — e2e smoke not run"
            ),
            "PIPELINE_RUN_NAME": "pr-1",
            "PIPELINE_NAMESPACE": "ns",
        }
        with (
            patch.dict(os.environ, env, clear=False),
            patch("runners.report.check_requested_gates_ran.require_pipeline_tasks_ran") as mock_req,
        ):
            self.assertEqual(check_main(), 0)
            mock_req.assert_not_called()

    def test_collect_hollow_green_fails_when_conforma_skip_without_min_rhoai(self) -> None:
        failures = collect_hollow_green_failures(
            test_gates="bvt,smoke",
            product="rhoai",
            conforma_gate=CONFORMA_GATE_SKIP,
            gate_values={},
        )
        self.assertTrue(any("conforma gate skipped e2e" in f for f in failures))

    def test_collect_hollow_green_empty_when_min_rhoai_conforma_skip(self) -> None:
        with patch.dict(
            os.environ,
            {
                "CONFORMA_GATE": CONFORMA_GATE_SKIP,
                "CONFORMA_GATE_DETAIL": (
                    "Skipped: snapshot catalog line '2.25' below MIN_RHOAI_VERSION 3.5 — e2e smoke not run"
                ),
            },
            clear=False,
        ):
            failures = collect_hollow_green_failures(
                test_gates="bvt,smoke",
                product="rhoai",
                conforma_gate=CONFORMA_GATE_SKIP,
                gate_values={},
            )
        self.assertEqual(failures, [])

    def test_fails_when_conforma_skip_not_min_rhoai(self) -> None:
        env = {
            "TEST_GATES": "bvt,smoke",
            "PRODUCT": "rhoai",
            "CONFORMA_GATE": CONFORMA_GATE_SKIP,
            "CONFORMA_GATE_DETAIL": "Skipped: conforma failed (ec-run-1) — e2e smoke not run",
        }
        with patch.dict(os.environ, env, clear=False):
            self.assertEqual(check_main(), 1)
            failures = collect_hollow_green_failures(conforma_gate=CONFORMA_GATE_SKIP)
            self.assertTrue(any("conforma gate skipped e2e" in f for f in failures))

    @patch("runners.report.check_requested_gates_ran.require_pipeline_tasks_ran", return_value=[])
    def test_passes_when_gates_ran(self, _mock: object) -> None:
        env = {
            "TEST_GATES": "bvt,smoke",
            "PRODUCT": "rhoai",
            "PIPELINE_RUN_NAME": "pr-1",
            "PIPELINE_NAMESPACE": "ns",
            "BVT_GATE_PATH": "/tmp/bvt-gate-ok",
            "SMOKE_GATE_PATH": "/tmp/smoke-gate-ok",
        }
        with patch.dict(os.environ, env, clear=False):
            with open("/tmp/bvt-gate-ok", "w", encoding="utf-8") as fh:
                fh.write("5 passed, 0 failed, 0 skipped, 5 total (100% pass rate)")
            with open("/tmp/smoke-gate-ok", "w", encoding="utf-8") as fh:
                fh.write("10 passed, 0 failed, 0 skipped, 10 total (100% pass rate)")
            try:
                self.assertEqual(check_main(), 0)
            finally:
                os.remove("/tmp/bvt-gate-ok")
                os.remove("/tmp/smoke-gate-ok")

    def test_passes_when_existing_product_skips_install(self) -> None:
        with patch(
            "runners.report.check_requested_gates_ran.require_pipeline_tasks_ran",
            return_value=[],
        ) as mock_req:
            failures = collect_hollow_green_failures(
                test_gates="smoke",
                product="",
                gate_values={"SMOKE_GATE": "38 passed, 4 failed, 0 skipped, 42 total"},
            )
            self.assertEqual(failures, [])
            mock_req.assert_not_called()

    @patch(
        "runners.report.check_requested_gates_ran.require_pipeline_tasks_ran",
        return_value=["install-rhoai: skipped (when false)"],
    )
    def test_fails_when_install_skipped(self, _mock: object) -> None:
        env = {
            "TEST_GATES": "bvt,smoke",
            "PRODUCT": "rhoai",
            "PIPELINE_RUN_NAME": "pr-1",
            "PIPELINE_NAMESPACE": "ns",
        }
        with patch.dict(os.environ, env, clear=False):
            self.assertEqual(check_main(), 1)

    def test_fails_when_bvt_gate_placeholder(self) -> None:
        env = {
            "TEST_GATES": "bvt,smoke",
            "PRODUCT": "rhoai",
            "BVT_GATE_PATH": "/tmp/bvt-gate",
            "SMOKE_GATE_PATH": "/tmp/smoke-gate",
        }
        with patch.dict(os.environ, env, clear=False):
            with open("/tmp/bvt-gate", "w", encoding="utf-8") as fh:
                fh.write("N/A (not run)")
            with open("/tmp/smoke-gate", "w", encoding="utf-8") as fh:
                fh.write("N/A (not run)")
            with patch(
                "runners.report.check_requested_gates_ran.require_pipeline_tasks_ran",
                return_value=[],
            ):
                try:
                    failures = collect_hollow_green_failures()
                    self.assertTrue(any("placeholder" in f for f in failures))
                    self.assertEqual(check_main(), 1)
                finally:
                    os.remove("/tmp/bvt-gate")
                    os.remove("/tmp/smoke-gate")

    def test_fails_when_opendatahub_disabled_but_gate_placeholder(self) -> None:
        env = {
            "TEST_GATES": "bvt",
            "PRODUCT": "rhoai",
            "RUN_OPENDATAHUB_TESTS": "false",
            "BVT_GATE_PATH": "/tmp/bvt-gate-disabled",
        }
        with patch.dict(os.environ, env, clear=False):
            with open("/tmp/bvt-gate-disabled", "w", encoding="utf-8") as fh:
                fh.write("N/A (not run)")
            try:
                failures = collect_hollow_green_failures()
                self.assertTrue(any("placeholder" in f for f in failures))
            finally:
                os.remove("/tmp/bvt-gate-disabled")

    def test_fails_without_pipeline_context_and_missing_gate(self) -> None:
        failures = collect_hollow_green_failures(
            test_gates="smoke",
            product="",
            gate_values={},
        )
        self.assertTrue(any("no PipelineRun context" in f for f in failures))

    @patch(
        "runners.report.check_requested_gates_ran.pipeline_task_execution_state",
        side_effect=lambda task, **kwargs: (
            ("failed", "TaskRunFailed")
            if task == "install-rhoai"
            else ("skipped", "when false")
        ),
    )
    @patch(
        "runners.report.check_requested_gates_ran.require_pipeline_tasks_ran",
        return_value=[],
    )
    def test_passes_when_install_failed_and_gates_not_run(
        self,
        _mock_req: object,
        _mock_state: object,
    ) -> None:
        env = {
            "TEST_GATES": "bvt,smoke",
            "PRODUCT": "rhoai",
            "PIPELINE_RUN_NAME": "pr-1",
            "PIPELINE_NAMESPACE": "ns",
            "BVT_GATE_PATH": "/tmp/bvt-gate-install-fail",
            "SMOKE_GATE_PATH": "/tmp/smoke-gate-install-fail",
        }
        with patch.dict(os.environ, env, clear=False):
            with open("/tmp/bvt-gate-install-fail", "w", encoding="utf-8") as fh:
                fh.write("N/A (not run)")
            with open("/tmp/smoke-gate-install-fail", "w", encoding="utf-8") as fh:
                fh.write("N/A (not run)")
            try:
                blockers = upstream_blocked_test_gates()
                self.assertTrue(any("install-rhoai" in b for b in blockers))
                self.assertEqual(collect_hollow_green_failures(), [])
                self.assertEqual(check_main(), 0)
            finally:
                os.remove("/tmp/bvt-gate-install-fail")
                os.remove("/tmp/smoke-gate-install-fail")

    @patch(
        "runners.report.check_requested_gates_ran.pipeline_task_execution_state",
        side_effect=lambda task, **kwargs: (
            ("failed", "TaskRunFailed")
            if task == "verify-operator-ready"
            else ("succeeded", "")
        ),
    )
    @patch(
        "runners.report.check_requested_gates_ran.require_pipeline_tasks_ran",
        side_effect=lambda tasks, **kwargs: (
            [f"{tasks[0]}: skipped (when false)"]
            if tasks and str(tasks[0]).startswith("run-")
            else []
        ),
    )
    def test_fails_when_verify_operator_ready_failed_and_gates_not_run(
        self,
        _mock_req: object,
        _mock_state: object,
    ) -> None:
        env = {
            "TEST_GATES": "bvt,smoke",
            "PRODUCT": "rhoai",
            "PIPELINE_RUN_NAME": "pr-1",
            "PIPELINE_NAMESPACE": "ns",
            "BVT_GATE_PATH": "/tmp/bvt-gate-verify-fail",
            "SMOKE_GATE_PATH": "/tmp/smoke-gate-verify-fail",
        }
        with patch.dict(os.environ, env, clear=False):
            with open("/tmp/bvt-gate-verify-fail", "w", encoding="utf-8") as fh:
                fh.write("N/A (not run)")
            with open("/tmp/smoke-gate-verify-fail", "w", encoding="utf-8") as fh:
                fh.write("N/A (not run)")
            try:
                failures = collect_hollow_green_failures()
                self.assertTrue(any("verify-operator-ready" in f for f in failures))
                self.assertEqual(check_main(), 1)
            finally:
                os.remove("/tmp/bvt-gate-verify-fail")
                os.remove("/tmp/smoke-gate-verify-fail")

    def test_format_install_blocked_publish_note(self) -> None:
        note = format_install_blocked_publish_note(
            ["install-rhoai: failed"],
            test_gates="bvt,smoke",
        )
        self.assertIn("install-rhoai: failed", note)
        self.assertIn("bvt: N/A (not run)", note)
        self.assertIn("smoke: N/A (not run)", note)


if __name__ == "__main__":
    unittest.main()
