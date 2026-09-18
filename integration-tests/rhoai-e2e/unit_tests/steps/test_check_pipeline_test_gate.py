"""Tests for test-finalize pipeline gate enforcement."""

from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from runners.report import check_pipeline_test_gate as gate_mod

class CheckPipelineTestGateTest(unittest.TestCase):
    def test_component_aggregate_below_80_fails_when_smoke_and_bvt_requested(self) -> None:
        bvt_json = json.dumps(
            {
                "result": "SUCCESS",
                "successes": 5,
                "failures": 0,
                "skipped": 0,
                "note": "bvt: 100% pass rate (5 passed, 0 failed, 0 skipped)",
            }
        )
        smoke_json = json.dumps(
            {
                "result": "WARNING",
                "successes": 2,
                "failures": 1,
                "skipped": 0,
                "note": "smoke: 67% pass rate (2 passed, 1 failed, 0 skipped)",
            }
        )
        with tempfile.TemporaryDirectory() as tmp:
            smoke_path = Path(tmp) / "smoke.json"
            smoke_path.write_text(smoke_json, encoding="utf-8")
            env = {
                "TEST_GATES": "bvt,smoke",
                "PIPELINE_RUN_NAME": "pr-1",
                "NAMESPACE": "rhoai-tenant",
                "SMOKE_TEST_OUTPUT_PATH": str(smoke_path),
            }
            with patch.dict(os.environ, env, clear=False):
                with patch.object(gate_mod, "list_taskruns_in_cluster", return_value=[{"metadata": {"name": "tr"}}]):
                    with patch.object(
                        gate_mod,
                        "list_pipeline_test_outputs",
                        return_value=[("bvt", bvt_json)],
                    ):
                        self.assertEqual(gate_mod.main(), 1)

    def test_component_aggregate_warning_passes_between_80_and_99_percent(self) -> None:
        smoke_json = json.dumps(
            {
                "result": "WARNING",
                "successes": 85,
                "failures": 15,
                "skipped": 0,
                "note": "smoke: 85% pass rate (85 passed, 15 failed, 0 skipped)",
            }
        )
        with tempfile.TemporaryDirectory() as tmp:
            smoke_path = Path(tmp) / "smoke.json"
            smoke_path.write_text(smoke_json, encoding="utf-8")
            env = {
                "TEST_GATES": "smoke",
                "PIPELINE_RUN_NAME": "pr-1",
                "NAMESPACE": "rhoai-tenant",
                "SMOKE_TEST_OUTPUT_PATH": str(smoke_path),
            }
            with patch.dict(os.environ, env, clear=False):
                with patch.object(gate_mod, "list_taskruns_in_cluster", return_value=[]):
                    self.assertEqual(gate_mod.main(), 0)

    def test_tier1_only_uses_component_aggregate_sidecar(self) -> None:
        """tier1 has no separate sidecar; pass-rate gate reads the shared component aggregate."""
        component_json = json.dumps(
            {
                "result": "WARNING",
                "successes": 85,
                "failures": 15,
                "skipped": 0,
                "note": "component: 85% pass rate (85 passed, 15 failed, 0 skipped)",
            }
        )
        with tempfile.TemporaryDirectory() as tmp:
            sidecar = Path(tmp) / "component-aggregate.json"
            sidecar.write_text(component_json, encoding="utf-8")
            env = {
                "TEST_GATES": "tier1",
                "PIPELINE_RUN_NAME": "pr-1",
                "NAMESPACE": "rhoai-tenant",
                "SMOKE_TEST_OUTPUT_PATH": str(sidecar),
            }
            with patch.dict(os.environ, env, clear=False):
                with patch.object(gate_mod, "list_taskruns_in_cluster", return_value=[]):
                    self.assertEqual(gate_mod.main(), 0)

if __name__ == "__main__":
    raise SystemExit(unittest.main())
