#!/usr/bin/env python3
"""Unit tests for export_component_plan version gates."""

from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from unit_tests._paths import REPO_ROOT
from unittest.mock import patch

from steps.export_component_plan import main  # noqa: E402

class ExportComponentPlanVersionGateTest(unittest.TestCase):
    def test_llama_stack_skipped_on_35(self) -> None:
        repo = REPO_ROOT
        cfg = repo / "integration-tests/rhoai-e2e/config/rhoai-e2e-components-smoke.yaml"
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "plan.json"
            env = {
                "COMPONENTS_CSV": "llama_stack,ogx",
                "COMPONENTS_CONFIG": str(cfg),
                "COMPONENT_TEST_PLAN_JSON": str(out),
                "TEST_GATES": "smoke",
                "PRODUCT": "",
                "OPERATOR_VERSION": "3.5.0-ea.2",
            }
            with patch.dict(os.environ, env, clear=False):
                self.assertEqual(main(), 0)
            plan = json.loads(out.read_text(encoding="utf-8"))
            by_id = {c["id"]: c for c in plan["components"]}
            self.assertIn("version_skip_reason", by_id["llama_stack"])
            self.assertNotIn("version_skip_reason", by_id["ogx"])
            self.assertEqual(plan["operator_version"], "3.5.0-ea.2")

    def test_distributed_workloads_skipped_on_36(self) -> None:
        repo = REPO_ROOT
        cfg = repo / "integration-tests/rhoai-e2e/config/rhoai-e2e-components-smoke.yaml"
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "plan.json"
            env = {
                "COMPONENTS_CSV": "distributed_workloads,trainer",
                "COMPONENTS_CONFIG": str(cfg),
                "COMPONENT_TEST_PLAN_JSON": str(out),
                "TEST_GATES": "smoke",
                "PRODUCT": "",
                "OPERATOR_VERSION": "3.6.0-ea.1",
            }
            with patch.dict(os.environ, env, clear=False):
                self.assertEqual(main(), 0)
            plan = json.loads(out.read_text(encoding="utf-8"))
            by_id = {c["id"]: c for c in plan["components"]}
            self.assertIn("version_skip_reason", by_id["distributed_workloads"])
            self.assertNotIn("version_skip_reason", by_id["trainer"])

    def test_plan_version_skips_match_dsc_install_policy_gates(self) -> None:
        """export_component_plan and DSC prep use the same catalog enablement (Jenkins parity)."""
        from install.dsc_install_policy import _version_gated_smoke_ids
        from suite.component_catalog import default_components_smoke_config_path, load_components_smoke_catalog

        repo = REPO_ROOT
        cfg = default_components_smoke_config_path()
        catalog = load_components_smoke_catalog(cfg)
        csv = ",".join(catalog.component_ids)
        ver = "3.6.0-ea.2"
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "plan.json"
            env = {
                "COMPONENTS_CSV": csv,
                "COMPONENTS_CONFIG": str(cfg),
                "COMPONENT_TEST_PLAN_JSON": str(out),
                "TEST_GATES": "smoke",
                "PRODUCT": "rhoai",
                "OPERATOR_VERSION": ver,
            }
            with patch.dict(os.environ, env, clear=False):
                self.assertEqual(main(), 0)
            plan = json.loads(out.read_text(encoding="utf-8"))
        skipped = {
            str(c["id"])
            for c in plan["components"]
            if str(c.get("version_skip_reason", "")).strip()
        }
        gated = _version_gated_smoke_ids(ver)
        self.assertTrue(skipped <= gated)
        self.assertIn("llama_stack", skipped)
        self.assertIn("distributed_workloads", skipped)
        self.assertIn("ai_safety", skipped)

