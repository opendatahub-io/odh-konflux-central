"""Tests for test-finalize pass-rate tiers."""

from __future__ import annotations

import json
import unittest

from runners.report.pipeline_test_outputs import apply_test_finalize_display_result
from suite.test_output_pass_rate import classify_result_by_pass_rate, gate_pass_rate


class TestOutputPassRateTest(unittest.TestCase):
    def test_gate_pass_rate_ignores_skips(self) -> None:
        self.assertAlmostEqual(gate_pass_rate(passed=900, failed=0, skipped=100), 1.0)
        self.assertAlmostEqual(gate_pass_rate(passed=90, failed=10, skipped=100), 0.9)

    def test_classify_skip_only_is_failure(self) -> None:
        self.assertEqual(
            classify_result_by_pass_rate(passed=0, failed=0, skipped=50),
            "FAILURE",
        )

    def test_classify_success_ignores_skips_when_executed_pass_rate_high(self) -> None:
        self.assertEqual(
            classify_result_by_pass_rate(passed=100, failed=0, skipped=200),
            "SUCCESS",
        )

    def test_classify_success_at_99_percent(self) -> None:
        self.assertEqual(
            classify_result_by_pass_rate(passed=99, failed=1, skipped=0),
            "SUCCESS",
        )
        self.assertEqual(
            classify_result_by_pass_rate(passed=990, failed=10, skipped=0),
            "SUCCESS",
        )

    def test_classify_warning_between_80_and_99(self) -> None:
        self.assertEqual(
            classify_result_by_pass_rate(passed=85, failed=15, skipped=0),
            "WARNING",
        )
        self.assertEqual(
            classify_result_by_pass_rate(passed=80, failed=20, skipped=0),
            "WARNING",
        )

    def test_classify_failure_below_80(self) -> None:
        self.assertEqual(
            classify_result_by_pass_rate(passed=19, failed=55, skipped=0),
            "FAILURE",
        )
        self.assertEqual(
            classify_result_by_pass_rate(passed=2, failed=1, skipped=0),
            "FAILURE",
        )

    def test_classify_failure_when_zero_passed(self) -> None:
        self.assertEqual(classify_result_by_pass_rate(passed=0, failed=5, skipped=0), "FAILURE")


class ApplyTestFinalizeDisplayResultTest(unittest.TestCase):
    def test_smoke_rate_applied_bvt_unchanged(self) -> None:
        payload = {
            "result": "WARNING",
            "successes": 21,
            "failures": 56,
            "skipped": 0,
            "note": "combined",
        }
        by_gate = {
            "bvt": json.dumps(
                {
                    "result": "SUCCESS",
                    "successes": 5,
                    "failures": 0,
                    "skipped": 0,
                }
            ),
            "smoke": json.dumps(
                {
                    "result": "WARNING",
                    "successes": 19,
                    "failures": 55,
                    "skipped": 0,
                }
            ),
        }
        out = apply_test_finalize_display_result(
            payload,
            by_gate=by_gate,
            test_gates="bvt,smoke",
        )
        self.assertEqual(out["result"], "FAILURE")

    def test_smoke_success_when_above_99_percent(self) -> None:
        payload = {"result": "WARNING", "successes": 995, "failures": 5, "skipped": 0, "note": "smoke"}
        by_gate = {
            "smoke": json.dumps(
                {"result": "WARNING", "successes": 995, "failures": 5, "skipped": 0}
            ),
        }
        out = apply_test_finalize_display_result(payload, by_gate=by_gate, test_gates="smoke")
        self.assertEqual(out["result"], "SUCCESS")


if __name__ == "__main__":
    raise SystemExit(unittest.main())
