#!/usr/bin/env python3
"""Unit tests for version-aware RUN_SMOKE flag refresh."""

from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from suite.component_catalog import default_components_smoke_config_path, load_components_smoke_catalog
from suite.component_smoke_flag_refresh import (
    catalog_ids_with_run_smoke_result,
    compute_version_aware_run_smoke_flags,
    format_version_skipped_summary,
    version_skipped_entries,
    write_run_smoke_tekton_results,
    write_version_skipped_manifest,
)
from steps.refresh_component_smoke_flags import main as refresh_main

class ComponentSmokeFlagRefreshTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        catalog = load_components_smoke_catalog(default_components_smoke_config_path())
        cls.catalog_ids = catalog.component_ids
        cls.run_smoke_ids = catalog_ids_with_run_smoke_result(cls.catalog_ids)

    def test_compute_flags_version_skip(self) -> None:
        plan = {
            "operator_version": "3.5.0-ea.2",
            "components": [
                {"id": "mlflow"},
                {"id": "ogx"},
                {"id": "llama_stack", "version_skip_reason": "maxRhoai=3.4"},
                {"id": "ai_safety", "version_skip_reason": "maxRhoai=3.4"},
                {"id": "ai_safety_evalhub"},
            ],
        }
        flags = compute_version_aware_run_smoke_flags(
            plan, run_component_tests=True, catalog_component_ids=self.catalog_ids
        )
        self.assertTrue(flags["mlflow"])
        self.assertFalse(flags["llama_stack"])
        self.assertFalse(flags["ai_safety"])
        self.assertTrue(flags["ai_safety_evalhub"])
        self.assertTrue(flags["ogx"])

    def test_compute_flags_disabled(self) -> None:
        flags = compute_version_aware_run_smoke_flags(
            {}, run_component_tests=False, catalog_component_ids=self.catalog_ids
        )
        self.assertFalse(any(flags.values()))

    def test_version_skipped_entries_and_summary(self) -> None:
        plan = {
            "operator_version": "3.5.0-ea.2",
            "components": [
                {"id": "llama_stack", "version_skip_reason": "maxRhoai=3.4"},
            ],
        }
        entries = version_skipped_entries(plan)
        self.assertEqual(len(entries), 1)
        self.assertIn("llama_stack", format_version_skipped_summary(entries))

    def test_write_manifest(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "version_skipped.json"
            plan = {
                "operator_version": "3.5.0-ea.2",
                "components": [
                    {"id": "ai_safety", "version_skip_reason": "maxRhoai=3.4"},
                ],
            }
            write_version_skipped_manifest(path, plan)
            doc = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(doc["components"][0]["id"], "ai_safety")
            self.assertIn("summary", doc)

    def test_refresh_step_writes_tekton_results(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            results = Path(tmp) / "results"
            results.mkdir()
            plan_path = Path(tmp) / "plan.json"
            plan_path.write_text(
                json.dumps(
                    {
                        "operator_version": "3.5.0-ea.2",
                        "components": [
                            {"id": "mlflow"},
                            {"id": "llama_stack", "version_skip_reason": "maxRhoai=3.4"},
                        ],
                    }
                ),
                encoding="utf-8",
            )
            env = {
                "RUN_COMPONENT_TESTS": "true",
                "COMPONENT_TEST_PLAN_JSON": str(plan_path),
                "RESULTS_DIR": str(results),
                "COMPONENTS_CONFIG": str(default_components_smoke_config_path()),
            }
            for cid in self.run_smoke_ids:
                key = f"RUN_SMOKE_{cid}_PATH"
                env[key] = str(results / key)
            with patch.dict(os.environ, env, clear=True):
                self.assertEqual(refresh_main(), 0)
            self.assertEqual((results / "RUN_SMOKE_mlflow_PATH").read_text(), "true")
            self.assertEqual((results / "RUN_SMOKE_llama_stack_PATH").read_text(), "false")

if __name__ == "__main__":
    raise SystemExit(unittest.main())
