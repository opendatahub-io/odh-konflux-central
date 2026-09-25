#!/usr/bin/env python3
"""Tests for CodeFlare SDK run-tests.sh dashboard URL patch."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest import mock

from components.codeflare_sdk.dashboard_patch import (  # noqa: E402
    codeflare_run_tests_dashboard_patch_shell,
    patch_run_tests_dashboard,
    prepend_codeflare_dashboard_patch,
    prepend_codeflare_run_command_patches,
    stage_codeflare_dashboard_patch_helper,
)

class CodeflareSdkDashboardPatchTest(unittest.TestCase):
    def test_patch_replaces_consolelink_lookup(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            script = Path(tmp) / "run-tests.sh"
            script.write_text(
                "#!/bin/bash\n"
                "set -e\n"
                "ODH_DASHBOARD_URL=$(oc get consolelink rhodslink -o jsonpath='{.spec.href}')\n"
                "echo done\n",
                encoding="utf-8",
            )
            self.assertTrue(patch_run_tests_dashboard(script))
            text = script.read_text(encoding="utf-8")
            self.assertIn('if [ -n "${ODH_DASHBOARD_URL:-}" ]', text)
            self.assertIn("2>/dev/null || true", text)
            self.assertNotIn(
                "ODH_DASHBOARD_URL=$(oc get consolelink rhodslink -o jsonpath='{.spec.href}')",
                text,
            )

    def test_patch_shell_stages_helper_under_artifacts(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            artifacts = Path(tmp)
            shell = codeflare_run_tests_dashboard_patch_shell(artifacts)
            helper = stage_codeflare_dashboard_patch_helper(artifacts)
            self.assertTrue(helper.is_file())
            self.assertIn(str(helper), shell)
            self.assertIn("python3", shell)

    def test_prepend_exports_dashboard_url_before_patch(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            artifacts = Path(tmp)
            out = prepend_codeflare_dashboard_patch(
                "bash run-tests.sh -m smoke",
                dashboard_url="https://dash.example.com",
                artifacts_dir=artifacts,
            )
            self.assertIn("export ODH_DASHBOARD_URL='https://dash.example.com'", out)
            self.assertTrue((artifacts / "codeflare_patch_run_tests_dashboard.py").is_file())
            self.assertTrue(out.endswith("bash run-tests.sh -m smoke"))

    def test_combined_patches_ephc_then_dashboard(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            artifacts = Path(tmp)
            with mock.patch(
                "components.codeflare_sdk.dashboard_patch.prepend_codeflare_ephc_kubeconfig_auth",
                return_value="export CLUSTER_AUTH=openshift; bash run-tests.sh -m smoke",
            ):
                out = prepend_codeflare_run_command_patches(
                    "bash run-tests.sh -m smoke",
                    dashboard_url="https://dash.example.com",
                    artifacts_dir=artifacts,
                )
            self.assertIn("export CLUSTER_AUTH=openshift", out)
            self.assertIn("export ODH_DASHBOARD_URL='https://dash.example.com'", out)
            self.assertIn("codeflare_patch_run_tests_dashboard.py", out)

