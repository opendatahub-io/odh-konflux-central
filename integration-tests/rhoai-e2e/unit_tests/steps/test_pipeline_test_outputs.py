"""Tests for bvt/smoke TEST_OUTPUT collection and finalize UI merge."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from runners.report.pipeline_test_outputs import (
    build_combined_test_output_payload,
    build_publish_task_test_output,
    collect_bvt_smoke_outputs,
    combined_test_output_from_sidecars,
)

def _fake_list(taskruns: list) -> list[tuple[str, str]]:
    del taskruns
    return [
        (
            "bvt",
            json.dumps(
                {
                    "result": "SUCCESS",
                    "successes": 5,
                    "failures": 0,
                    "skipped": 0,
                    "note": "bvt: 100% pass rate (5 passed, 0 failed, 0 skipped)",
                    "suites": [{"id": "cluster-health", "passed": 5, "failed": 0, "skipped": 0, "total": 5}],
                }
            ),
        )
    ]

class PipelineTestOutputsTest(unittest.TestCase):
    def test_collect_prefers_workspace_smoke_over_missing_taskrun(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            smoke_path = Path(tmp) / "smoke.json"
            smoke_path.write_text(
                json.dumps(
                    {
                        "result": "WARNING",
                        "successes": 2,
                        "failures": 1,
                        "skipped": 0,
                        "note": "smoke: 67% pass rate (2 passed, 1 failed, 0 skipped)",
                    }
                ),
                encoding="utf-8",
            )
            by_gate = collect_bvt_smoke_outputs(
                [],
                list_from_taskruns=_fake_list,
                smoke_path=str(smoke_path),
            )
        self.assertIn("bvt", by_gate)
        self.assertIn("smoke", by_gate)

    def test_build_combined_payload_note_and_warning(self) -> None:
        outputs = [
            (
                "bvt",
                json.dumps(
                    {
                        "result": "SUCCESS",
                        "successes": 5,
                        "failures": 0,
                        "skipped": 0,
                        "note": "bvt: 100% pass rate (5 passed, 0 failed, 0 skipped)",
                        "suites": [{"id": "cluster-health", "passed": 5, "failed": 0, "skipped": 0, "total": 5}],
                    }
                ),
            ),
            (
                "smoke",
                json.dumps(
                    {
                        "result": "WARNING",
                        "successes": 2,
                        "failures": 1,
                        "skipped": 0,
                        "note": "smoke: 67% pass rate (2 passed, 1 failed, 0 skipped)",
                        "suites": [{"id": "workbenches", "passed": 2, "failed": 1, "skipped": 0, "total": 3}],
                    }
                ),
            ),
        ]
        payload = build_combined_test_output_payload(outputs)
        self.assertIsNotNone(payload)
        assert payload is not None
        self.assertEqual(payload["result"], "WARNING")
        note = str(payload["note"])
        self.assertIn("bvt: 100% pass rate", note)
        self.assertIn("smoke: 67% pass rate", note)

    def test_build_combined_payload_maps_skipped_to_warnings_for_konflux_list(self) -> None:
        outputs = [
            (
                "bvt",
                json.dumps(
                    {
                        "result": "SUCCESS",
                        "successes": 9,
                        "failures": 0,
                        "skipped": 0,
                    }
                ),
            ),
            (
                "smoke",
                json.dumps(
                    {
                        "result": "WARNING",
                        "successes": 209,
                        "failures": 15,
                        "skipped": 135,
                    }
                ),
            ),
        ]
        payload = build_combined_test_output_payload(outputs)
        self.assertIsNotNone(payload)
        assert payload is not None
        self.assertEqual(payload["skipped"], 135)
        self.assertEqual(payload["warnings"], 135)

    def test_resolve_prefers_test_finalize_json_when_no_gate_merge(self) -> None:
        from runners.report.pipeline_test_outputs import resolve_pipeline_test_output_text

        finalize_json = json.dumps(
            {
                "result": "SUCCESS",
                "failures": 0,
                "warnings": 0,
                "successes": 2,
                "skipped": 0,
                "note": "smoke: 100% pass rate (2 passed, 0 failed, 0 skipped)",
                "timestamp": "2026-07-01T18:10:30Z",
            }
        )
        taskruns = [
            {
                "metadata": {"labels": {"tekton.dev/pipelineTask": "test-finalize"}},
                "status": {"results": [{"name": "TEST_OUTPUT", "value": finalize_json}]},
            }
        ]
        resolved = resolve_pipeline_test_output_text(taskruns, test_gates="smoke")
        self.assertIsNotNone(resolved)
        self.assertEqual(json.loads(resolved or "{}"), json.loads(finalize_json))

    def test_merge_prefers_taskrun_over_empty_sidecar(self) -> None:
        rich = json.dumps(
            {
                "result": "SUCCESS",
                "successes": 5,
                "failures": 0,
                "skipped": 0,
                "note": "bvt: 100% pass rate (5 passed, 0 failed, 0 skipped)",
            }
        )
        empty = json.dumps(
            {
                "result": "SUCCESS",
                "successes": 0,
                "failures": 0,
                "skipped": 0,
                "note": "bvt: 100% pass rate (0 passed, 0 failed, 0 skipped)",
            }
        )
        with tempfile.TemporaryDirectory() as tmp:
            bvt_path = Path(tmp) / "bvt.json"
            bvt_path.write_text(empty, encoding="utf-8")
            by_gate = collect_bvt_smoke_outputs(
                [],
                list_from_taskruns=lambda _runs: [("bvt", rich)],
                bvt_path=str(bvt_path),
            )
        self.assertEqual(by_gate["bvt"], rich)

    def test_resolve_rebuilds_from_gates_over_stale_finalize_note(self) -> None:
        from runners.report.pipeline_test_outputs import resolve_pipeline_test_output_text

        finalize_json = json.dumps(
            {
                "result": "WARNING",
                "successes": 2,
                "failures": 1,
                "skipped": 0,
                "note": "bvt: 100% pass rate (0 passed, 0 failed, 0 skipped); smoke: 67%",
            }
        )
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
        taskruns = [
            {
                "metadata": {"labels": {"tekton.dev/pipelineTask": "bvt-health-checks"}},
                "status": {"results": [{"name": "TEST_OUTPUT", "value": bvt_json}]},
            },
            {
                "metadata": {"labels": {"tekton.dev/pipelineTask": "test-finalize"}},
                "status": {"results": [{"name": "TEST_OUTPUT", "value": smoke_json}]},
            },
        ]
        resolved = resolve_pipeline_test_output_text(taskruns, test_gates="bvt,smoke")
        self.assertIsNotNone(resolved)
        data = json.loads(resolved or "{}")
        self.assertEqual(data["successes"], 7)
        self.assertIn("5 passed", str(data["note"]))

    def test_collect_prefers_workspace_bvt_when_taskruns_empty(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            bvt_path = Path(tmp) / "bvt.json"
            bvt_path.write_text(
                json.dumps(
                    {
                        "result": "FAILURE",
                        "successes": 4,
                        "failures": 1,
                        "skipped": 0,
                        "timestamp": "2026-07-02T09:00:00Z",
                        "note": "bvt: 80% pass rate (4 passed, 1 failed, 0 skipped)",
                    }
                ),
                encoding="utf-8",
            )
            expected = bvt_path.read_text(encoding="utf-8").strip()
            by_gate = collect_bvt_smoke_outputs(
                [],
                list_from_taskruns=lambda _runs: [],
                bvt_path=str(bvt_path),
            )
        self.assertEqual(by_gate["bvt"], expected)

    def test_resolve_bvt_only_from_workspace_when_finalize_skipped(self) -> None:
        from runners.report.pipeline_test_outputs import resolve_pipeline_test_output_text

        with tempfile.TemporaryDirectory() as tmp:
            bvt_path = Path(tmp) / "bvt.json"
            bvt_path.write_text(
                json.dumps(
                    {
                        "result": "FAILURE",
                        "successes": 4,
                        "failures": 1,
                        "skipped": 0,
                        "timestamp": "2026-07-02T09:00:00Z",
                        "note": "bvt: 80% pass rate (4 passed, 1 failed, 0 skipped)",
                    }
                ),
                encoding="utf-8",
            )
            resolved = resolve_pipeline_test_output_text(
                [],
                test_gates="bvt,smoke",
                bvt_path=str(bvt_path),
            )
        self.assertIsNotNone(resolved)
        assert resolved is not None
        data = json.loads(resolved)
        self.assertEqual(data["result"], "FAILURE")
        self.assertIn("bvt: 80% pass rate", str(data["note"]))

    def test_build_publish_task_test_output_forces_success(self) -> None:
        payload = build_publish_task_test_output(
            {
                "result": "WARNING",
                "successes": 10,
                "failures": 2,
                "note": "smoke: 83% pass rate (10 passed, 2 failed, 0 skipped)",
            }
        )
        self.assertEqual(payload["result"], "SUCCESS")
        self.assertEqual(payload["failures"], 0)
        self.assertEqual(payload["warnings"], 0)
        self.assertIn("smoke: 83% pass rate", str(payload["note"]))

    def test_finalize_skips_duplicate_bvt_when_test_finalize_combined(self) -> None:
        from runners.report.pipeline_test_outputs import build_finalize_test_output_from_taskruns

        combined_smoke = json.dumps(
            {
                "result": "WARNING",
                "successes": 562,
                "failures": 52,
                "skipped": 118,
                "note": (
                    "bvt: 100% pass rate (9 passed, 0 failed, 0 skipped)\n"
                    "smoke: 76% pass rate (553 passed, 52 failed, 118 skipped)"
                ),
            },
            separators=(",", ":"),
        )
        bvt_only = json.dumps(
            {"result": "SUCCESS", "successes": 9, "failures": 0, "skipped": 0},
            separators=(",", ":"),
        )

        def list_outputs(_taskruns: list[dict[str, object]]) -> list[tuple[str, str]]:
            return [("bvt", bvt_only), ("smoke", combined_smoke)]

        payload = build_finalize_test_output_from_taskruns(
            [],
            test_gates="bvt,smoke",
            list_from_taskruns=list_outputs,
        )
        self.assertIsNotNone(payload)
        assert payload is not None
        self.assertEqual(payload["successes"], 562)
        self.assertEqual(payload["failures"], 52)

    def test_combined_test_output_from_sidecars(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            bvt_path = Path(tmp) / "bvt.json"
            smoke_path = Path(tmp) / "smoke.json"
            bvt_path.write_text(
                json.dumps({"result": "SUCCESS", "successes": 5, "failures": 0, "skipped": 0}),
                encoding="utf-8",
            )
            smoke_path.write_text(
                json.dumps({"result": "WARNING", "successes": 8, "failures": 1, "skipped": 0}),
                encoding="utf-8",
            )
            payload = combined_test_output_from_sidecars(
                test_gates="bvt,smoke",
                bvt_path=str(bvt_path),
                smoke_path=str(smoke_path),
            )
        self.assertIsNotNone(payload)
        assert payload is not None
        self.assertEqual(payload["result"], "WARNING")

if __name__ == "__main__":
    raise SystemExit(unittest.main())
