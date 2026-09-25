"""Tests for in-task component skip recording."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from steps import record_component_skipped

class RecordComponentSkippedTest(unittest.TestCase):
    def test_skip_reason_uses_version_gate_from_plan(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            plan_path = Path(tmp) / "component-test-plan.json"
            plan_path.write_text(
                json.dumps(
                    {
                        "operator_version": "2.5.0-rc.1",
                        "components": [
                            {
                                "id": "ai_safety",
                                "version_skip_reason": "maxRhoai 3.4",
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )
            reason = record_component_skipped._skip_reason("ai_safety", plan_path)
            self.assertEqual(reason, "maxRhoai 3.4 (RHOAI 2.5.0-rc.1)")

if __name__ == "__main__":
    raise SystemExit(unittest.main())
