"""Tests for BVT Tekton gate (block smoke only when TEST_OUTPUT is FAILURE or missing)."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from steps.check_bvt_gate import check_bvt_test_output

class CheckBvtGateTest(unittest.TestCase):
    def test_success_passes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "out.json"
            path.write_text(
                json.dumps(
                    {
                        "result": "SUCCESS",
                        "successes": 2,
                        "note": "bvt: 100% pass rate",
                    }
                ),
                encoding="utf-8",
            )
            ec, msg = check_bvt_test_output(path)
            self.assertEqual(ec, 0)
            self.assertIn("100%", msg)

    def test_failure_blocks(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "out.json"
            path.write_text(
                json.dumps(
                    {
                        "result": "FAILURE",
                        "successes": 0,
                        "failures": 1,
                        "skipped": 0,
                        "note": "bvt: 0% pass rate (0 passed, 1 failed, 0 skipped)",
                    }
                ),
                encoding="utf-8",
            )
            ec, msg = check_bvt_test_output(path)
            self.assertEqual(ec, 1)
            self.assertIn("strict", msg.lower())

    def test_warning_with_failures_blocks(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "out.json"
            path.write_text(
                json.dumps(
                    {
                        "result": "WARNING",
                        "successes": 4,
                        "failures": 1,
                        "note": "bvt: 80% pass rate (4 passed, 1 failed, 0 skipped)",
                    }
                ),
                encoding="utf-8",
            )
            ec, msg = check_bvt_test_output(path)
            self.assertEqual(ec, 1)
            self.assertIn("strict", msg.lower())

    def test_all_skipped_bvt_note_passes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "out.json"
            path.write_text(
                json.dumps(
                    {
                        "result": "FAILURE",
                        "successes": 0,
                        "failures": 0,
                        "skipped": 2,
                        "note": "bvt: 0% pass rate (0 passed, 0 failed, 2 skipped)",
                    }
                ),
                encoding="utf-8",
            )
            ec, msg = check_bvt_test_output(path)
            self.assertEqual(ec, 0)
            self.assertIn("skipped", msg.lower())

    def test_partial_skip_with_passes_succeeds(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "out.json"
            path.write_text(
                json.dumps(
                    {
                        "result": "SUCCESS",
                        "successes": 1,
                        "failures": 0,
                        "skipped": 1,
                        "note": "bvt: 50% pass rate (1 passed, 0 failed, 1 skipped)",
                    }
                ),
                encoding="utf-8",
            )
            ec, msg = check_bvt_test_output(path)
            self.assertEqual(ec, 0)
            self.assertIn("50%", msg)

    def test_missing_file_blocks(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            ec, msg = check_bvt_test_output(Path(tmp) / "missing.json")
            self.assertEqual(ec, 1)
            self.assertIn("missing", msg.lower())

