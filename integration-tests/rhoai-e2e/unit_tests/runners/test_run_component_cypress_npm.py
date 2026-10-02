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

class DashboardCypressNpmCiTest(unittest.TestCase):
    def test_dashboard_npm_ci_command_scopes_workspaces(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "package.json").write_text('{"name":"odh-dashboard","workspaces":["frontend","packages/*"]}', encoding="utf-8")
            (root / "frontend").mkdir()
            (root / "frontend" / "package.json").write_text('{"name":"odh-dashboard-frontend"}', encoding="utf-8")
            (root / "packages" / "cypress").mkdir(parents=True)
            (root / "packages" / "cypress" / "package.json").write_text(
                '{"name":"@odh-dashboard/cypress"}',
                encoding="utf-8",
            )
            self.assertEqual(
                _dashboard_npm_ci_command(root, "frontend"),
                ["npm", "ci", "--ignore-scripts", "-w=odh-dashboard-frontend", "-w=@odh-dashboard/cypress"],
            )

    def test_reset_dashboard_src_when_ref_changes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            artifacts = Path(tmp)
            dashboard_src = artifacts / "dashboard-src"
            dashboard_src.mkdir()
            (dashboard_src / "node_modules").mkdir()
            (artifacts / ".dashboard-source-ref").write_text('{"ref": "main", "repo": "old/repo"}\n', encoding="utf-8")
            _reset_dashboard_src_if_ref_changed(artifacts, dashboard_src, "new/repo", "rhoai-3.4")
            self.assertFalse(dashboard_src.exists())
            self.assertEqual(
                (artifacts / ".dashboard-source-ref").read_text(encoding="utf-8"),
                '{"ref": "rhoai-3.4", "repo": "new/repo"}\n',
            )

    def test_hoist_tslib_symlinks_frontend_dep_to_root(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            frontend_tslib = root / "frontend" / "node_modules" / "tslib"
            frontend_tslib.mkdir(parents=True)
            (frontend_tslib / "package.json").write_text('{"name":"tslib"}', encoding="utf-8")
            _hoist_tslib_for_cypress(root)
            root_tslib = root / "node_modules" / "tslib"
            self.assertTrue(root_tslib.is_symlink())
            self.assertEqual(root_tslib.resolve(), frontend_tslib.resolve())
