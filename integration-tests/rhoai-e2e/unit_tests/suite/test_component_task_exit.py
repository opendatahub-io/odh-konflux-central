"""Tests for shared component/BVT Tekton exit resolution."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from suite.component_task_exit import (
    component_exit_file_path,
    resolve_component_exit_codes,
    resolve_junit_aggregate_exit,
)

class ComponentTaskExitTest(unittest.TestCase):
    def test_component_exit_file_path_rejects_path_separators(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with self.assertRaises(ValueError):
                component_exit_file_path(root, "../other")
            with self.assertRaises(ValueError):
                component_exit_file_path(root, "foo/bar")

    def test_partial_pass_keeps_tekton_exit_zero(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "workbenches-smoke.xml").write_text(
                """<?xml version="1.0" encoding="UTF-8"?>
<testsuite name="workbenches" tests="3" failures="1" errors="0" skipped="0">
  <testcase classname="a" name="t1"/>
  <testcase classname="a" name="t2"/>
  <testcase classname="a" name="t3"><failure message="x"/></testcase>
</testsuite>
""",
                encoding="utf-8",
            )
            strict, tekton = resolve_component_exit_codes(
                {"id": "workbenches", "artifact_prefix": "workbenches-smoke"},
                raw_ec=1,
                artifacts_dir=root,
            )
            self.assertEqual(strict, 1)
            self.assertEqual(tekton, 0)

    def test_bvt_partial_pass_keeps_tekton_exit_zero(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "cluster-health.xml").write_text(
                """<?xml version="1.0" encoding="UTF-8"?>
<testsuite name="cluster" tests="1" failures="0" errors="0" skipped="0">
  <testcase classname="a" name="t1"/>
</testsuite>
""",
                encoding="utf-8",
            )
            (root / "operator-health.xml").write_text(
                """<?xml version="1.0" encoding="UTF-8"?>
<testsuite name="operator" tests="1" failures="1" errors="0" skipped="0">
  <testcase classname="a" name="t1"><failure message="x"/></testcase>
</testsuite>
""",
                encoding="utf-8",
            )
            strict, tekton = resolve_junit_aggregate_exit(
                root,
                (root / "cluster-health.xml", root / "operator-health.xml"),
                raw_ec=1,
            )
            self.assertEqual(strict, 1)
            self.assertEqual(tekton, 0)

    def test_bvt_partial_skip_passes_tekton_step(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "cluster-health.xml").write_text(
                """<?xml version="1.0" encoding="UTF-8"?>
<testsuite name="cluster" tests="1" failures="0" errors="0" skipped="0">
  <testcase classname="a" name="t1"/>
</testsuite>
""",
                encoding="utf-8",
            )
            (root / "operator-health.xml").write_text(
                """<?xml version="1.0" encoding="UTF-8"?>
<testsuite name="operator" tests="1" failures="0" errors="0" skipped="1">
  <testcase classname="a" name="t1"><skipped message="n/a"/></testcase>
</testsuite>
""",
                encoding="utf-8",
            )
            strict, tekton = resolve_junit_aggregate_exit(
                root,
                (root / "cluster-health.xml", root / "operator-health.xml"),
                raw_ec=0,
            )
            self.assertEqual(strict, 0)
            self.assertEqual(tekton, 0)

    def test_bvt_all_skipped_passes_tekton_step(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "cluster-health.xml").write_text(
                """<?xml version="1.0" encoding="UTF-8"?>
<testsuite name="cluster" tests="1" failures="0" errors="0" skipped="1">
  <testcase classname="a" name="t1"><skipped message="n/a"/></testcase>
</testsuite>
""",
                encoding="utf-8",
            )
            (root / "operator-health.xml").write_text(
                """<?xml version="1.0" encoding="UTF-8"?>
<testsuite name="operator" tests="1" failures="0" errors="0" skipped="1">
  <testcase classname="a" name="t1"><skipped message="n/a"/></testcase>
</testsuite>
""",
                encoding="utf-8",
            )
            strict, tekton = resolve_junit_aggregate_exit(
                root,
                (root / "cluster-health.xml", root / "operator-health.xml"),
                raw_ec=1,
            )
            self.assertEqual(strict, 0)
            self.assertEqual(tekton, 0)

    def test_no_junit_failed_run_fails_tekton_step(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            strict, tekton = resolve_component_exit_codes(
                {"id": "workbenches", "artifact_prefix": "workbenches-smoke"},
                raw_ec=1,
                artifacts_dir=root,
            )
            self.assertEqual(strict, 1)
            self.assertEqual(tekton, 1)

    def test_skip_only_fails_tekton_step(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "llama_stack-smoke.xml").write_text(
                """<?xml version="1.0" encoding="UTF-8"?>
<testsuite name="llama_stack" tests="1" failures="0" errors="0" skipped="1">
  <testcase classname="a" name="version_not_supported"><skipped message="maxRhoai=3.4"/></testcase>
</testsuite>
""",
                encoding="utf-8",
            )
            strict, tekton = resolve_component_exit_codes(
                {"id": "llama_stack", "artifact_prefix": "llama_stack-smoke"},
                raw_ec=1,
                artifacts_dir=root,
            )
            self.assertEqual(strict, 1)
            self.assertEqual(tekton, 1)

    def test_zero_pass_fails_tekton_step(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "workbenches-smoke.xml").write_text(
                """<?xml version="1.0" encoding="UTF-8"?>
<testsuite name="workbenches" tests="17" failures="10" errors="0" skipped="7">
  <testcase classname="a" name="t1"><failure message="x"/></testcase>
</testsuite>
""",
                encoding="utf-8",
            )
            strict, tekton = resolve_component_exit_codes(
                {"id": "workbenches", "artifact_prefix": "workbenches-smoke"},
                raw_ec=1,
                artifacts_dir=root,
            )
            self.assertEqual(strict, 1)
            self.assertEqual(tekton, 1)

    def test_unreadable_junit_file_fails_tekton_step(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "workbenches-smoke.xml").write_text("not xml", encoding="utf-8")
            strict, tekton = resolve_component_exit_codes(
                {"id": "workbenches", "artifact_prefix": "workbenches-smoke"},
                raw_ec=1,
                artifacts_dir=root,
            )
            self.assertEqual(strict, 1)
            self.assertEqual(tekton, 1)

