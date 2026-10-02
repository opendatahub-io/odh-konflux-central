"""Tests for publish-results gate summary population."""

from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path

from runners.report.junit_suite_report import (
    GATE_NOT_RUN_SUMMARY,
    augment_publish_gate_note,
    build_publish_results_gate_summaries,
    gate_summary_from_combined_note,
)
from runners.report.pipeline_test_outputs import build_combined_test_output_payload

class PopulatePublishGateResultsTest(unittest.TestCase):
    def test_gate_summary_from_combined_note(self) -> None:
        note = (
            "bvt: 100% pass rate (9 passed, 0 failed, 0 skipped); "
            "smoke: 61% pass rate (208 passed, 16 failed, 115 skipped)"
        )
        self.assertEqual(
            gate_summary_from_combined_note(note, "bvt"),
            "9 passed, 0 failed, 0 skipped, 9 total (100% pass rate)",
        )
        self.assertEqual(
            gate_summary_from_combined_note(note, "smoke"),
            "208 passed, 16 failed, 115 skipped, 339 total (61% pass rate)",
        )

    def test_build_publish_results_gate_summaries_from_combined_note_only(self) -> None:
        combined = json.dumps(
            {
                "result": "WARNING",
                "successes": 217,
                "failures": 16,
                "skipped": 115,
                "note": (
                    "bvt: 100% pass rate (9 passed, 0 failed, 0 skipped); "
                    "smoke: 61% pass rate (208 passed, 16 failed, 115 skipped)"
                ),
            },
            separators=(",", ":"),
        )
        summaries = build_publish_results_gate_summaries(combined_raw=combined)
        self.assertIn("TESTS_SUMMARY", summaries)
        self.assertIn("BVT_GATE", summaries)
        self.assertIn("SMOKE_GATE", summaries)

    def test_build_publish_results_gate_summaries_smoke_only_sets_bvt_not_run(self) -> None:
        smoke = json.dumps({"successes": 5, "failures": 0, "skipped": 0}, separators=(",", ":"))
        summaries = build_publish_results_gate_summaries(
            bvt_raw="",
            smoke_raw=smoke,
            test_gates="smoke",
        )
        self.assertEqual(summaries["BVT_GATE"], GATE_NOT_RUN_SUMMARY)
        self.assertIn("passed", summaries["SMOKE_GATE"])

    def test_build_publish_results_gate_summaries_smoke_not_run_when_bvt_only(self) -> None:
        combined = json.dumps(
            {
                "result": "WARNING",
                "successes": 4,
                "failures": 1,
                "skipped": 0,
                "note": "bvt: 80% pass rate (4 passed, 1 failed, 0 skipped)",
            },
            separators=(",", ":"),
        )
        bvt = json.dumps(
            {"successes": 4, "failures": 1, "skipped": 0},
            separators=(",", ":"),
        )
        summaries = build_publish_results_gate_summaries(
            combined_raw=combined,
            bvt_raw=bvt,
            smoke_raw="",
            test_gates="bvt,smoke",
        )
        self.assertIn("BVT_GATE", summaries)
        self.assertEqual(summaries["SMOKE_GATE"], GATE_NOT_RUN_SUMMARY)
        note = augment_publish_gate_note(
            "bvt: 80% pass rate (4 passed, 1 failed, 0 skipped)",
            test_gates="bvt,smoke",
            gate_summaries=summaries,
        )
        self.assertIn("smoke: N/A (not run)", note)

    def test_tests_summary_sums_sidecars_not_inflated_combined(self) -> None:
        from runners.report.junit_suite_report import tests_summary_from_gate_sidecars

        bvt = json.dumps({"successes": 9, "failures": 0, "skipped": 0}, separators=(",", ":"))
        smoke = json.dumps(
            {"successes": 553, "failures": 52, "skipped": 118},
            separators=(",", ":"),
        )
        inflated = json.dumps(
            {"successes": 571, "failures": 52, "skipped": 118},
            separators=(",", ":"),
        )
        summaries = build_publish_results_gate_summaries(
            combined_raw=inflated,
            bvt_raw=bvt,
            smoke_raw=smoke,
            test_gates="bvt,smoke",
        )
        self.assertEqual(
            summaries["TESTS_SUMMARY"],
            "562 passed, 52 failed, 118 skipped, 732 total (77% pass rate)",
        )
        self.assertEqual(
            tests_summary_from_gate_sidecars(bvt_raw=bvt, smoke_raw=smoke, test_gates="bvt,smoke"),
            summaries["TESTS_SUMMARY"],
        )

    def test_build_combined_payload_includes_smoke_suites(self) -> None:
        smoke = json.dumps(
            {
                "result": "SUCCESS",
                "successes": 5,
                "failures": 0,
                "skipped": 0,
                "note": "smoke: 100% pass rate (5 passed, 0 failed, 0 skipped)",
                "suites": [
                    {
                        "id": "ai_pipelines-smoke",
                        "passed": 5,
                        "failed": 0,
                        "skipped": 0,
                        "total": 5,
                    }
                ],
            },
            separators=(",", ":"),
        )
        payload = build_combined_test_output_payload([("smoke", smoke)])
        self.assertIsNotNone(payload)
        assert payload is not None
        suites = payload.get("suites")
        self.assertIsInstance(suites, list)
        assert isinstance(suites, list)
        self.assertEqual(len(suites), 1)
        self.assertEqual(suites[0]["id"], "ai_pipelines-smoke")

    def test_populate_overwrites_no_tests_placeholder(self) -> None:
        from runners.report.populate_publish_gate_results import main

        with tempfile.TemporaryDirectory() as tmp:
            combined_path = Path(tmp) / "combined.json"
            bvt_path = Path(tmp) / "bvt.json"
            smoke_path = Path(tmp) / "smoke.json"
            tests_summary_path = Path(tmp) / "tests_summary.txt"
            bvt_gate_path = Path(tmp) / "bvt_gate.txt"
            smoke_gate_path = Path(tmp) / "smoke_gate.txt"
            tests_summary_path.write_text("no tests", encoding="utf-8")
            bvt_gate_path.write_text("no tests", encoding="utf-8")
            smoke_gate_path.write_text("no tests", encoding="utf-8")
            combined_path.write_text(
                json.dumps(
                    {
                        "result": "WARNING",
                        "successes": 14,
                        "failures": 1,
                        "skipped": 0,
                        "note": (
                            "bvt: 100% pass rate (9 passed, 0 failed, 0 skipped)\n"
                            "smoke: 56% pass rate (5 passed, 1 failed, 0 skipped)"
                        ),
                    },
                    separators=(",", ":"),
                ),
                encoding="utf-8",
            )
            bvt_path.write_text(
                json.dumps({"successes": 9, "failures": 0, "skipped": 0}, separators=(",", ":")),
                encoding="utf-8",
            )
            smoke_path.write_text(
                json.dumps({"successes": 5, "failures": 1, "skipped": 0}, separators=(",", ":")),
                encoding="utf-8",
            )
            prev = {
                key: os.environ.get(key)
                for key in (
                    "TEST_OUTPUT_PATH",
                    "BVT_TEST_OUTPUT_PATH",
                    "SMOKE_TEST_OUTPUT_PATH",
                    "TESTS_SUMMARY_PATH",
                    "BVT_GATE_PATH",
                    "SMOKE_GATE_PATH",
                    "TEKTON_RESULTS_DIR",
                )
            }
            os.environ["TEST_OUTPUT_PATH"] = str(combined_path)
            os.environ["BVT_TEST_OUTPUT_PATH"] = str(bvt_path)
            os.environ["SMOKE_TEST_OUTPUT_PATH"] = str(smoke_path)
            os.environ["TESTS_SUMMARY_PATH"] = str(tests_summary_path)
            os.environ["BVT_GATE_PATH"] = str(bvt_gate_path)
            os.environ["SMOKE_GATE_PATH"] = str(smoke_gate_path)
            os.environ["TEKTON_RESULTS_DIR"] = tmp
            try:
                self.assertEqual(main(), 0)
                self.assertIn("passed", tests_summary_path.read_text(encoding="utf-8"))
                self.assertNotEqual(tests_summary_path.read_text(encoding="utf-8").strip(), "no tests")
            finally:
                for key, value in prev.items():
                    if value is None:
                        os.environ.pop(key, None)
                    else:
                        os.environ[key] = value

if __name__ == "__main__":
    raise SystemExit(unittest.main())
