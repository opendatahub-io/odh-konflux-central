"""Tests for OLMINSTALL_OC_VERBOSE behavior in k8s.oc_util."""

from __future__ import annotations

import io
import subprocess
import unittest
from contextlib import redirect_stderr, redirect_stdout
from unittest import mock

from k8s import oc_util

class OcUtilVerboseTest(unittest.TestCase):
    def setUp(self) -> None:
        self._oc_path = mock.patch.object(oc_util, "_oc_path", return_value="/bin/oc")
        self._oc_path.start()

    def tearDown(self) -> None:
        self._oc_path.stop()

    def test_verbose_disabled_uses_subprocess_run(self) -> None:
        with mock.patch.dict(oc_util.os.environ, {"OLMINSTALL_OC_VERBOSE": ""}, clear=False):
            with mock.patch.object(oc_util.subprocess, "run") as run_mock:
                run_mock.return_value = subprocess.CompletedProcess(
                    ["/bin/oc", "version"], 0, stdout="ok", stderr=""
                )
                proc = oc_util.run_oc(["version"], capture_output=True)
                self.assertEqual(proc.stdout, "ok")
                run_mock.assert_called_once()

    def test_verbose_prints_command_and_exit_when_streaming(self) -> None:
        with mock.patch.dict("os.environ", {"OLMINSTALL_OC_VERBOSE": "1"}):
            with mock.patch.object(oc_util.subprocess, "run") as run_mock:
                run_mock.return_value = subprocess.CompletedProcess(
                    ["/bin/oc", "version"], 0, stdout=None, stderr=None
                )
                buf = io.StringIO()
                with redirect_stdout(buf):
                    oc_util.run_oc(["version"], capture_output=False)
                out = buf.getvalue()
                self.assertIn("+ oc version", out)
                self.assertIn("→ exit 0", out)
                run_mock.assert_called_once()

    def test_verbose_bulk_json_omits_output_but_captures(self) -> None:
        payload = '{"items":[]}'
        with mock.patch.dict("os.environ", {"OLMINSTALL_OC_VERBOSE": "true"}):
            with mock.patch.object(oc_util.subprocess, "run") as run_mock:
                run_mock.return_value = subprocess.CompletedProcess(
                    ["/bin/oc", "get", "deployments", "-A", "-o", "json"],
                    0,
                    stdout=payload,
                    stderr="",
                )
                buf = io.StringIO()
                with redirect_stdout(buf):
                    proc = oc_util.run_oc(["get", "deployments", "-A", "-o", "json"], capture_output=True)
                out = buf.getvalue()
                self.assertEqual(proc.stdout, payload)
                self.assertIn("+ oc get deployments -A -o json", out)
                self.assertIn("output omitted: bulk -o format", out)
                self.assertNotIn(payload, out)

    def test_verbose_tees_wait_output(self) -> None:
        with mock.patch.dict("os.environ", {"OLMINSTALL_OC_VERBOSE": "yes"}):
            with mock.patch.object(oc_util, "_run_subprocess_with_tee") as tee_mock:
                tee_mock.return_value = subprocess.CompletedProcess(
                    ["/bin/oc", "wait", "deployment/foo", "-n", "ns"],
                    0,
                    stdout="deployment.apps/foo condition met\n",
                    stderr="",
                )
                buf = io.StringIO()
                with redirect_stdout(buf):
                    proc = oc_util.run_oc(
                        ["wait", "deployment/foo", "-n", "ns"],
                        capture_output=True,
                    )
                out = buf.getvalue()
                self.assertIn("+ oc wait deployment/foo -n ns", out)
                self.assertIn("→ exit 0", out)
                self.assertEqual(proc.stdout, "deployment.apps/foo condition met\n")
                tee_mock.assert_called_once()

    def test_verbose_apply_stdin_label(self) -> None:
        with mock.patch.dict("os.environ", {"OLMINSTALL_OC_VERBOSE": "on"}):
            with mock.patch.object(oc_util, "_run_subprocess_with_tee") as tee_mock:
                with mock.patch.object(oc_util.subprocess, "run") as run_mock:
                    run_mock.return_value = subprocess.CompletedProcess(
                        ["/bin/oc", "apply", "-f", "-"], 0, stdout="", stderr=""
                    )
                    buf = io.StringIO()
                    with redirect_stdout(buf):
                        oc_util.run_oc(
                            ["apply", "-f", "-"],
                            stdin_text='{"kind":"Secret"}',
                            capture_output=True,
                        )
                    self.assertIn("+ oc apply -f -  # <stdin omitted>", buf.getvalue())
                    tee_mock.assert_not_called()
                    run_mock.assert_called_once()

    def test_output_format_bulk_detection(self) -> None:
        self.assertTrue(oc_util._oc_output_format_bulk(["get", "pods", "-o", "json"]))
        self.assertTrue(oc_util._oc_output_format_bulk(["get", "pods", "-o=yaml"]))
        self.assertFalse(oc_util._oc_output_format_bulk(["wait", "deployment/foo", "-n", "ns"]))

