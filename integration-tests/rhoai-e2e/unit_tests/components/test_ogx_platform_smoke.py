"""Unit tests for OGX platform diagnostics (no hollow SUCCESS JUnit)."""

from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from components.ogx.platform_smoke import (
    ensure_ogx_junit_after_pytest,
    should_write_ogx_platform_smoke,
    write_ogx_platform_junit,
)


class OgxPlatformSmokeTest(unittest.TestCase):
    def test_write_platform_junit_all_pass(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch(
                "components.ogx.platform_smoke.run_ogx_platform_checks",
                return_value=[
                    ("ogx_crds_present", True, "found ogxservers.ogx.io"),
                    ("llamastackoperator_not_managed", True, "Removed"),
                ],
            ):
                out = write_ogx_platform_junit(Path(tmp))
            text = out.read_text(encoding="utf-8")
            self.assertIn('name="ogx_crds_present"', text)
            self.assertIn('failures="0"', text)
            self.assertNotIn("<failure", text)

    def test_hollow_fill_disabled(self) -> None:
        self.assertFalse(should_write_ogx_platform_smoke())

    def test_ensure_keeps_real_pytest_junit(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            junit = root / "ogx-smoke.xml"
            junit.write_text(
                '<?xml version="1.0"?>\n'
                '<testsuite tests="1" failures="1">\n'
                '  <testcase name="vector"><failure message="x"/></testcase>\n'
                "</testsuite>\n",
                encoding="utf-8",
            )
            with mock.patch.dict(os.environ, {"CLUSTER_SOURCE": "EPHC"}, clear=False):
                ensure_ogx_junit_after_pytest(root)
            self.assertIn("vector", junit.read_text(encoding="utf-8"))

    def test_ensure_does_not_write_success_when_empty(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with (
                mock.patch.dict(os.environ, {"CLUSTER_SOURCE": "EPHC"}, clear=False),
                mock.patch(
                    "components.ogx.platform_smoke.run_ogx_platform_checks",
                    return_value=[("ogx_crds_present", True, "ok")],
                ),
            ):
                ensure_ogx_junit_after_pytest(root)
            self.assertFalse((root / "ogx-smoke.xml").is_file())


if __name__ == "__main__":
    unittest.main()
