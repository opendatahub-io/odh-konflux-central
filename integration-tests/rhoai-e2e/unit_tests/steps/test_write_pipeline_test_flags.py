#!/usr/bin/env python3
"""Unit tests for RUN_MINIMAL_DEPS / setup-dependencies gating."""

from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from unit_tests._paths import REPO_ROOT
from unittest.mock import patch

from steps.write_pipeline_test_flags import main  # noqa: E402

class WritePipelineTestFlagsMinimalDepsTest(unittest.TestCase):
    def _run(
        self,
        *,
        product: str,
        tests: str,
        components: str = "",
        external_kubeconfig_secret: str = "",
        install_dependencies: str = "",
    ) -> int:
        repo = REPO_ROOT
        with tempfile.TemporaryDirectory() as tmp:
            results = Path(tmp)
            run_config = results / "run-config"
            env = {
                "REPO_ROOT": str(repo),
                "RESULTS_DIR": str(results),
                "TEKTON_RESULTS_DIR": str(results),
                "PRODUCT": product,
                "TEST_GATES": tests,
                "COMPONENTS": components,
                "CLUSTER_SOURCE": external_kubeconfig_secret,
                "INSTALL_DEPENDENCIES": install_dependencies,
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
            with patch.dict(os.environ, env, clear=False):
                rc = main()
            if rc != 0:
                return rc
            self._last_results = {
                p.stem: p.read_text(encoding="utf-8")
                for p in results.iterdir()
                if p.is_file()
            }
            if run_config.is_dir():
                for p in run_config.iterdir():
                    self._last_results[p.stem] = p.read_text(encoding="utf-8")
            return 0

    def test_product_existing_workbenches_smoke_skips_minimal_deps(self) -> None:
        self.assertEqual(self._run(product="", tests="smoke", components="workbenches"), 0)
        self.assertEqual(self._last_results["RUN_MINIMAL_DEPS"], "false")
        self.assertEqual(self._last_results["RUN_COMPONENT_TESTS"], "false")
        self.assertEqual(self._last_results["RUN_SMOKE"], "false")
        self.assertEqual(self._last_results["SETUP_DEPENDENCIES_ARGS"], "")

    def test_product_existing_maas_smoke_without_cluster_disables_smoke(self) -> None:
        self.assertEqual(self._run(product="", tests="smoke", components="maas_billing"), 0)
        self.assertEqual(self._last_results["RUN_COMPONENT_TESTS"], "false")
        self.assertEqual(self._last_results["RUN_INSTALL_DEP_OPERATORS"], "false")
        self.assertEqual(self._last_results["RUN_MINIMAL_DEPS"], "false")

    def test_product_existing_maas_smoke_enables_install_dep_operators_with_external(self) -> None:
        self.assertEqual(
            self._run(
                product="",
                tests="smoke",
                components="maas_billing",
                external_kubeconfig_secret="my-kubeconfig",
            ),
            0,
        )
        self.assertEqual(self._last_results["RUN_MINIMAL_DEPS"], "true")
        self.assertEqual(self._last_results["RUN_INSTALL_DEP_OPERATORS"], "true")
        self.assertEqual(self._last_results["SETUP_DEPENDENCIES_ARGS"], "-M")
        self.assertEqual(self._last_results["RUN_COMPONENT_CLUSTER_PREP_IN_DEP_OPERATORS"], "true")

    def test_product_existing_bvt_only_sets_placeholder_flag(self) -> None:
        self.assertEqual(self._run(product="", tests="bvt"), 0)
        self.assertEqual(self._last_results["RUN_BVT"], "true")
        self.assertEqual(self._last_results["RUN_BVT_PLACEHOLDER_ONLY"], "true")
        self.assertEqual(self._last_results["RUN_COMPONENT_TESTS"], "false")
        self.assertEqual(self._last_results["RUN_OPENDATAHUB_TESTS"], "true")

    def test_trainer_smoke_sets_distributed_workloads_flag(self) -> None:
        self.assertEqual(
            self._run(
                product="",
                tests="smoke",
                components="trainer",
                external_kubeconfig_secret="my-kubeconfig",
            ),
            0,
        )
        self.assertEqual(self._last_results["RUN_DISTRIBUTED_WORKLOADS_TESTS"], "true")

    def test_product_rhoai_enables_minimal_deps(self) -> None:
        self.assertEqual(self._run(product="rhoai", tests="bvt"), 0)
        self.assertEqual(self._last_results["RUN_MINIMAL_DEPS"], "true")
        self.assertEqual(self._last_results["RUN_INSTALL_DEP_OPERATORS"], "true")

    def test_product_rhoai_smoke_defers_cluster_prep_to_post_install(self) -> None:
        self.assertEqual(
            self._run(product="rhoai", tests="bvt,smoke", components="maas_billing"),
            0,
        )
        self.assertEqual(self._last_results["RUN_INSTALL_DEP_OPERATORS"], "true")
        self.assertEqual(self._last_results["RUN_COMPONENT_CLUSTER_PREP_IN_DEP_OPERATORS"], "false")
        self.assertEqual(self._last_results["RUN_COMPONENT_TESTS"], "true")

    def test_product_existing_skips_install_dep_operators_without_external(self) -> None:
        self.assertEqual(self._run(product="", tests="smoke", components="workbenches"), 0)
        self.assertEqual(self._last_results["RUN_INSTALL_DEP_OPERATORS"], "false")

    def test_external_kubeconfig_skips_install_dep_for_non_maas_smoke(self) -> None:
        self.assertEqual(
            self._run(
                product="",
                tests="smoke",
                components="workbenches",
                external_kubeconfig_secret="my-kubeconfig",
            ),
            0,
        )
        self.assertEqual(self._last_results["RUN_INSTALL_DEP_OPERATORS"], "false")

    def test_external_kubeconfig_enables_install_dep_for_maas_smoke_without_flag(self) -> None:
        self.assertEqual(
            self._run(
                product="",
                tests="smoke",
                components="maas_billing",
                external_kubeconfig_secret="my-kubeconfig",
            ),
            0,
        )
        self.assertEqual(self._last_results["RUN_INSTALL_DEP_OPERATORS"], "true")
        self.assertEqual(self._last_results["RUN_MINIMAL_DEPS"], "true")
        self.assertEqual(self._last_results["RUN_COMPONENT_CLUSTER_PREP_IN_DEP_OPERATORS"], "true")

    def test_external_kubeconfig_skips_install_dep_for_llama_stack_smoke(self) -> None:
        self.assertEqual(
            self._run(
                product="",
                tests="smoke",
                components="llama_stack",
                external_kubeconfig_secret="my-kubeconfig",
            ),
            0,
        )
        self.assertEqual(self._last_results["RUN_INSTALL_DEP_OPERATORS"], "false")
        self.assertEqual(self._last_results["RUN_MINIMAL_DEPS"], "false")

    def test_product_existing_llama_stack_smoke_skips_install_dep_operators(self) -> None:
        self.assertEqual(self._run(product="", tests="smoke", components="llama_stack"), 0)
        self.assertEqual(self._last_results["RUN_INSTALL_DEP_OPERATORS"], "false")
        self.assertEqual(self._last_results["RUN_MINIMAL_DEPS"], "false")

    def test_ogx_smoke_prepare_uses_unused_shift_left_secret(self) -> None:
        self.assertEqual(
            self._run(
                product="",
                tests="smoke",
                components="ogx",
                external_kubeconfig_secret="my-kubeconfig",
            ),
            0,
        )
        self.assertEqual(self._last_results["SMOKE_AWS_SECRET"], "unused-smoke-aws-secret")

    def test_maas_billing_and_ogx_smoke_prepare_uses_model_serving(self) -> None:
        self.assertEqual(
            self._run(
                product="",
                tests="smoke",
                components="maas_billing,ogx",
                external_kubeconfig_secret="my-kubeconfig",
            ),
            0,
        )
        self.assertEqual(
            self._last_results["SMOKE_AWS_SECRET"],
            "shiftleft-envfile-model-serving",
        )

    def test_install_dependencies_enables_dep_operators_and_moves_cluster_prep(self) -> None:
        self.assertEqual(
            self._run(
                product="",
                tests="smoke",
                components="model_server",
                external_kubeconfig_secret="my-kubeconfig",
                install_dependencies="true",
            ),
            0,
        )
        self.assertEqual(self._last_results["RUN_INSTALL_DEP_OPERATORS"], "true")
        self.assertEqual(self._last_results["RUN_MINIMAL_DEPS"], "true")
        self.assertEqual(self._last_results["SETUP_DEPENDENCIES_ARGS"], "-M")
        self.assertEqual(self._last_results["RUN_COMPONENT_CLUSTER_PREP_IN_DEP_OPERATORS"], "true")

    def test_install_dependencies_without_smoke_skips_dep_operators(self) -> None:
        self.assertEqual(
            self._run(
                product="",
                tests="bvt",
                install_dependencies="true",
            ),
            0,
        )
        self.assertEqual(self._last_results["RUN_INSTALL_DEP_OPERATORS"], "false")
        self.assertEqual(self._last_results["RUN_BVT_PLACEHOLDER_ONLY"], "true")
        self.assertEqual(self._last_results["RUN_COMPONENT_CLUSTER_PREP_IN_DEP_OPERATORS"], "false")

if __name__ == "__main__":
    raise SystemExit(unittest.main())
