"""Unit tests for steps.finalize_component_tests."""

from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path

from steps import finalize_component_tests

class FinalizeComponentTestsTest(unittest.TestCase):
    def _artifacts_dir(self) -> Path:
        tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(tmpdir.cleanup)
        artifacts = Path(tmpdir.name)
        prev = os.environ.get("ARTIFACTS_DIR")
        os.environ["ARTIFACTS_DIR"] = str(artifacts)

        def restore_env() -> None:
            if prev is None:
                os.environ.pop("ARTIFACTS_DIR", None)
            else:
                os.environ["ARTIFACTS_DIR"] = prev

        self.addCleanup(restore_env)
        return artifacts

    def test_missing_plan_exits_2(self) -> None:
        self._artifacts_dir()
        self.assertEqual(finalize_component_tests.main(), 2)

    def test_zero_exit_without_plan_succeeds(self) -> None:
        artifacts = self._artifacts_dir()
        (artifacts / "component-test.exit").write_text("0", encoding="ascii")
        self.assertEqual(finalize_component_tests.main(), 0)

    def test_nonzero_exit_file_logs_but_succeeds(self) -> None:
        artifacts = self._artifacts_dir()
        (artifacts / "component-test-plan.json").write_text("{}", encoding="utf-8")
        (artifacts / "component-test.exit").write_text("1", encoding="ascii")
        self.assertEqual(finalize_component_tests.main(), 0)

    def test_nonzero_exit_without_plan_defers_to_gate(self) -> None:
        artifacts = self._artifacts_dir()
        (artifacts / "component-test.exit").write_text("1", encoding="ascii")
        self.assertEqual(finalize_component_tests.main(), 0)

