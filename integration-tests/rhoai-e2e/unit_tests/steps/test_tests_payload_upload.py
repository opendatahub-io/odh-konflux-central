#!/usr/bin/env python3
"""Unit tests for tests-payload upload helpers."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from suite.tests_config import load_artifact_upload_config
from steps.tests_payload import (
    COLLECT_DIAGNOSTICS_DONE_MARKER,
    collect_upload_files,
    has_publishable_artifacts,
    mark_collect_diagnostics_done,
    oci_upload_marker,
    resolve_tests_payload_root,
    stage_tests_payload_for_upload,
)
from steps.tests_payload import tests_payload_tools_bin_dir as payload_tools_bin_dir
from suite.constants import default_tests_config_path
from steps.write_artifacts_url import write_artifacts_url_result

class TestsPayloadUploadTest(unittest.TestCase):
    def test_resolve_tests_payload_root_from_shared_workspace(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            shared = Path(tmp)
            payload = shared / "tests-payload" / "results"
            payload.mkdir(parents=True)
            self.assertEqual(resolve_tests_payload_root(shared), shared / "tests-payload")
            self.assertEqual(resolve_tests_payload_root(payload), shared / "tests-payload")

    def test_has_publishable_and_marker(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "tests-payload" / "results"
            root.mkdir(parents=True)
            self.assertFalse(has_publishable_artifacts(root))
            (root / "workbenches-smoke.xml").write_text("<testsuite/>", encoding="utf-8")
            self.assertTrue(has_publishable_artifacts(root))
            marker = oci_upload_marker(root)
            marker.write_text("", encoding="utf-8")
            self.assertTrue(marker.is_file())

    def test_mark_collect_diagnostics_done(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            shared = Path(tmp) / "tests-shared"
            marker = mark_collect_diagnostics_done(
                shared,
                artifact_name="existing-diagnostic-2026-06-24T120000Z.log",
                status="done",
            )
            self.assertEqual(marker.name, COLLECT_DIAGNOSTICS_DONE_MARKER)
            body = marker.read_text(encoding="utf-8")
            self.assertIn("done", body)
            self.assertIn("existing-diagnostic", body)

    def test_collect_upload_excludes_oc_binary(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            payload = Path(tmp) / "tests-payload"
            results = payload / "results"
            results.mkdir(parents=True)
            (results / "cluster_health.xml").write_text("<testsuite/>", encoding="utf-8")
            tools_bin = payload_tools_bin_dir(payload)
            tools_bin.mkdir(parents=True)
            (tools_bin / "oc").write_bytes(b"\x7fELF")
            files = collect_upload_files(payload)
            self.assertEqual([p.name for p in files], ["cluster_health.xml"])

    def test_stage_upload_uses_config_subdir(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            payload = Path(tmp) / "tests-payload"
            results = payload / "results"
            results.mkdir(parents=True)
            (results / "bvt.xml").write_text("<testsuite/>", encoding="utf-8")
            staging = stage_tests_payload_for_upload(
                payload, oci_subdir="test-payload-results", patterns=("*.xml",)
            )
            staged = staging / "test-payload-results" / "bvt.xml"
            self.assertTrue(staged.is_file())

    def test_load_artifact_upload_config_from_repo(self) -> None:
        cfg = load_artifact_upload_config(default_tests_config_path())
        self.assertEqual(cfg.oci_subdir, "test-payload-results")
        self.assertIn("*.xml", cfg.include_patterns)

    def test_write_artifacts_url_only_after_upload(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            payload = Path(tmp) / "tests-payload"
            payload.mkdir()
            out = Path(tmp) / "artifacts_url"
            with patch("steps.write_artifacts_url.write_result") as write_result:
                url = write_artifacts_url_result(
                    artifacts_url_path=str(out),
                    pr_name="run-1",
                    browser_base="https://browser.example",
                    repo_path="odh-ci-artifacts",
                    tests_payload_dir=payload,
                )
                self.assertEqual(url, "")
                write_result.assert_called_once_with(str(out), "")
                (payload / ".oci-upload-ok").write_text("", encoding="utf-8")
                url = write_artifacts_url_result(
                    artifacts_url_path=str(out),
                    pr_name="run-1",
                    browser_base="https://browser.example",
                    repo_path="odh-ci-artifacts",
                    tests_payload_dir=payload,
                )
                self.assertIn("run-1", url)
                self.assertEqual(write_result.call_count, 2)

if __name__ == "__main__":
    raise SystemExit(unittest.main())
