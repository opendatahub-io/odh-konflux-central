"""Unit tests for conforma skip TEST_OUTPUT helper."""

from __future__ import annotations

import json
import unittest

from runners.report.pipeline_test_outputs import konflux_conforma_skip_test_output_json


class ConformaSkipTestOutputTest(unittest.TestCase):
    def test_warning_payload(self) -> None:
        raw = konflux_conforma_skip_test_output_json(note="Skipped: conforma failed")
        data = json.loads(raw)
        self.assertEqual(data["result"], "WARNING")
        self.assertGreaterEqual(int(data["warnings"]), 1)
        self.assertEqual(int(data["failures"]), 0)

    def test_truncates_long_note(self) -> None:
        long_note = "x" * 5000
        raw = konflux_conforma_skip_test_output_json(note=long_note)
        data = json.loads(raw)
        self.assertLessEqual(len(data["note"]), 3000)


if __name__ == "__main__":
    unittest.main()
