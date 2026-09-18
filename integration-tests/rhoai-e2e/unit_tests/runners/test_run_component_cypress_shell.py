"""Tests for component runner env file parsing and Cypress runtime helpers."""

from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock
from unittest.mock import patch

from components.dashboard_cypress.config import (
    discover_cypress_results_subdirs,
    prepend_cypress_shell_env,
)
from components.dashboard_cypress.auth_overlay import _byoidc_cypress_poll_settings
from components.dashboard_cypress.runtime import (
    _apply_gateway_auth_overlay,
    _dashboard_npm_ci_command,
    _hoist_tslib_for_cypress,
    _reset_dashboard_src_if_ref_changed,
    inject_ci_auth_bypass,
    load_component_vault_env,
    patch_gateway_envoyfilter_if_needed,
    patch_runtime_cy_test_config,
    htpasswd_hcp_extra_cypress_skip_tags,
    byoidc_extra_cypress_skip_tags,
    cypress_extra_skip_tags,
    resolve_gateway_auth_overlay,
    resolve_cypress_support_dir,
    resolve_test_clusters_overlay,
    sync_cypress_auth_env_from_config,
)
from suite.component_catalog_models import CypressParallelSet, CypressRunnerConfig
from suite.component_runner_env import load_component_runner_env

class DashboardCypressShellEnvTest(unittest.TestCase):
    @mock.patch("components.dashboard_cypress.runtime.os.geteuid", return_value=1000)
    def test_ensure_pyyaml_uses_staged_tools_python(self, _uid_mock) -> None:
        import builtins

        real_import = builtins.__import__
        yaml_imports = 0

        def fake_import(name, *args, **kwargs):
            nonlocal yaml_imports
            if name == "yaml":
                yaml_imports += 1
                if yaml_imports == 1:
                    raise ImportError("no yaml")
            return real_import(name, *args, **kwargs)

        with tempfile.TemporaryDirectory() as tmp:
            payload = Path(tmp) / "tests-payload"
            python_dir = payload / ".tools" / "python" / "yaml"
            python_dir.mkdir(parents=True)
            (python_dir / "__init__.py").write_text("", encoding="utf-8")
            artifacts = payload / "results"
            artifacts.mkdir(parents=True)
            with mock.patch.dict(os.environ, {"ARTIFACTS_DIR": str(artifacts)}, clear=False):
                with mock.patch("builtins.__import__", side_effect=fake_import):
                    from components.dashboard_cypress.runtime import _ensure_pyyaml_available

                    _ensure_pyyaml_available()
                    self.assertEqual(yaml_imports, 2)
                    tools_python = str(payload / ".tools" / "python")
                    self.assertIn(tools_python, os.environ.get("PYTHONPATH", ""))

    def test_prepend_cypress_shell_env(self) -> None:
        with mock.patch.dict(
            os.environ,
            {
                "OC_TOKEN": "tok",
                "CYPRESS_OC_TOKEN": "tok",
                "ODH_DASHBOARD_URL": "https://dash.example",
            },
            clear=False,
        ):
            cmd = prepend_cypress_shell_env(
                "npm run cypress:run",
                tools_bin="/tmp/.tools/bin",
                kubeconfig="/tmp/kube/config",
            )
        self.assertIn('export PATH="/tmp/.tools/bin:$PATH"', cmd)
        self.assertIn('export KUBECONFIG="/tmp/kube/config"', cmd)
        self.assertIn('export OC_TOKEN="tok"', cmd)
        self.assertIn('export CYPRESS_OC_TOKEN="tok"', cmd)
        self.assertIn("npm run cypress:run", cmd)

    def test_discover_cypress_results_subdirs(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "SmokeSet1").mkdir()
            (root / "SmokeSet2").mkdir()
            (root / "dashboard-src").mkdir()
            self.assertEqual(
                discover_cypress_results_subdirs(root),
                ("SmokeSet1", "SmokeSet2"),
            )
