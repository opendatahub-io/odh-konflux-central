#!/usr/bin/env python3
"""Unit tests for per-component RUN_SMOKE_* Tekton result wiring."""

from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unit_tests._paths import RHOAI_E2E_ROOT, REPO_ROOT
from unittest.mock import patch

from suite.component_catalog import default_components_smoke_config_path, load_components_smoke_catalog
from suite.component_smoke_results import component_smoke_result_name, ordered_component_ids
from steps.write_pipeline_test_flags import main

class ComponentSmokeResultsTest(unittest.TestCase):
    def test_ordered_component_ids_run_order_last(self) -> None:
        catalog = load_components_smoke_catalog(default_components_smoke_config_path())
        ordered = ordered_component_ids(
            catalog.component_ids,
            run_order=catalog.component_run_order,
        )
        self.assertEqual(ordered[-1], "platform")
        self.assertIn("workbenches", ordered)
        self.assertLess(ordered.index("llama_stack"), ordered.index("platform"))

    def test_write_flags_sets_only_selected_component_results(self) -> None:
        catalog = load_components_smoke_catalog(default_components_smoke_config_path())
        repo = REPO_ROOT
        with tempfile.TemporaryDirectory() as tmp:
            results = Path(tmp)
            run_config = results / "run-config"
            env = {
                "REPO_ROOT": str(repo),
                "RESULTS_DIR": str(results),
                "TEKTON_RESULTS_DIR": str(results),
                "PRODUCT": "",
                "CLUSTER_SOURCE": "external-kubeconfig-secret",
                "TEST_GATES": "smoke",
                "COMPONENTS": "ai_pipelines",
                "RUN_MINIMAL_DEPS_PATH": str(results / "RUN_MINIMAL_DEPS"),
                "RUN_INSTALL_DEP_OPERATORS_PATH": str(results / "RUN_INSTALL_DEP_OPERATORS"),
                "RUN_SMOKE_PATH": str(results / "RUN_SMOKE"),
                "RUN_BVT_PATH": str(results / "RUN_BVT"),
                "RUN_TIER1_PATH": str(results / "RUN_TIER1"),
                "RUN_OPENDATAHUB_TESTS_PATH": str(results / "RUN_OPENDATAHUB_TESTS"),
                "RUN_COMPONENT_TESTS_PATH": str(results / "RUN_COMPONENT_TESTS"),
                "RUN_COMPONENT_CLUSTER_PREP_IN_DEP_OPERATORS_PATH": str(
                    results / "RUN_COMPONENT_CLUSTER_PREP_IN_DEP_OPERATORS"
                ),
                "RUN_BVT_PLACEHOLDER_ONLY_PATH": str(results / "RUN_BVT_PLACEHOLDER_ONLY"),
                "RUN_DISTRIBUTED_WORKLOADS_TESTS_PATH": str(results / "RUN_DISTRIBUTED_WORKLOADS_TESTS"),
                "SETUP_DEPENDENCIES_ARGS_PATH": str(results / "SETUP_DEPENDENCIES_ARGS"),
                "SETUP_DEPENDENCIES_ARGS_WORKSPACE": str(run_config / "SETUP_DEPENDENCIES_ARGS"),
                "COMPONENTS_CSV_PATH": str(results / "COMPONENTS_CSV"),
                "COMPONENTS_CSV_WORKSPACE": str(run_config / "COMPONENTS_CSV"),
                "SMOKE_AWS_SECRET_PATH": str(results / "SMOKE_AWS_SECRET"),
                "SMOKE_AWS_SECRET_WORKSPACE": str(run_config / "SMOKE_AWS_SECRET"),
            }
            for cid in catalog.component_ids:
                key = component_smoke_result_name(cid)
                env[f"{key}_PATH"] = str(results / key)
            with patch.dict(os.environ, env, clear=False):
                self.assertEqual(main(), 0)
            self.assertEqual((results / "RUN_SMOKE").read_text(), "true")
            self.assertEqual((results / "RUN_COMPONENT_TESTS").read_text(), "true")
            self.assertFalse((results / "RUN_SMOKE_ai_pipelines").exists())

if __name__ == "__main__":
    raise SystemExit(unittest.main())
