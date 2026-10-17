"""Catalog runner parsing for non-pytest component tests."""

from __future__ import annotations

import unittest
from pathlib import Path

from suite.component_catalog import (
    load_components_smoke_catalog,
    resolve_shift_left_env_secret,
    resolve_shift_left_env_secret_for_prepare,
    shift_left_env_secret_for_component,
)
from suite.component_catalog_models import default_components_smoke_config_path

class ComponentCatalogRunnerTest(unittest.TestCase):
    def test_model_runtime_splits_accelerator_flag_from_marker(self) -> None:
        catalog = load_components_smoke_catalog(default_components_smoke_config_path())
        comp = catalog.components["model_runtime"]
        self.assertEqual(comp.phase_markers["smoke"], "smoke")
        self.assertIn("--supported-accelerator-type CPU_x86", comp.pytest_extra_args)
        self.assertIn("--tc use_unprivileged_client:False", comp.pytest_extra_args)

    def test_model_server_smoke_marker_strips_shell_quotes(self) -> None:
        catalog = load_components_smoke_catalog(default_components_smoke_config_path())
        comp = catalog.components["model_server"]
        self.assertEqual(comp.phase_markers["smoke"], "smoke and not downstream_only")

    def test_ai_safety_evalhub_skips_mcp_and_conversion_on_smoke(self) -> None:
        catalog = load_components_smoke_catalog(default_components_smoke_config_path())
        comp = catalog.components["ai_safety_evalhub"]
        self.assertIn("evalhub_mcp", comp.pytest_extra_args)
        self.assertIn("evalhub_conversion", comp.pytest_extra_args)

    def test_ai_pipelines_has_golang_runner(self) -> None:
        catalog = load_components_smoke_catalog(default_components_smoke_config_path())
        comp = catalog.components["ai_pipelines"]
        self.assertIsNotNone(comp.runner)
        assert comp.runner is not None
        self.assertEqual(comp.runner.type, "golang-ginkgo")
        self.assertIn("ds-pipelines-tests", comp.runner.image)
        self.assertEqual(comp.runner.working_dir, "/dspa/backend/test/images")
        self.assertIn("test-run.sh", comp.runner.phase_commands["smoke"])
        self.assertIn("--label-filter=Smoke", comp.runner.phase_commands["smoke"])
        assert comp.runner.env_defaults is not None
        self.assertEqual(comp.runner.env_defaults.get("NAMESPACE"), "dspa-test")
        self.assertEqual(comp.runner.env_defaults.get("DSPA_NAME"), "dspa")
        self.assertEqual(comp.tests_subdir, "")

    def test_kuberay_has_golang_test_tier_runner(self) -> None:
        catalog = load_components_smoke_catalog(default_components_smoke_config_path())
        comp = catalog.components["kuberay"]
        self.assertIsNotNone(comp.runner)
        assert comp.runner is not None
        self.assertEqual(comp.runner.type, "golang-test-tier")
        self.assertIn("kuberay-tests", comp.runner.image)
        self.assertEqual(comp.runner.working_dir, "/kuberay/tests")
        self.assertEqual(comp.runner.results_dir, "/kuberay/tests/results")
        self.assertIn("run-tests.sh", comp.runner.phase_commands["smoke"])
        self.assertIn("-testTier=Smoke", comp.runner.phase_commands["smoke"])
        self.assertIn("-testTier=Tier1", comp.runner.phase_commands["tier1"])
        self.assertEqual(comp.tests_subdir, "")
        self.assertEqual(comp.component_test_timeout_by_gate.get("smoke"), "15m")

    def test_mlflow_has_external_pytest_runner(self) -> None:
        catalog = load_components_smoke_catalog(default_components_smoke_config_path())
        comp = catalog.components["mlflow"]
        self.assertIsNotNone(comp.runner)
        assert comp.runner is not None
        self.assertEqual(comp.runner.type, "external-pytest")
        self.assertIn("mlflow-tests", comp.runner.image)
        self.assertEqual(comp.runner.working_dir, "/mlflow")
        self.assertEqual(comp.runner.results_dir, "/mlflow/results")
        self.assertEqual(comp.runner.vault_secret_key, "envfile-mlflow")
        self.assertIn("test-run.sh", comp.runner.phase_commands["smoke"])
        self.assertIn("-m smoke", comp.runner.phase_commands["smoke"])
        assert comp.runner.env_defaults is not None
        self.assertEqual(comp.runner.env_defaults.get("DEPLOY_MLFLOW_OPERATOR"), "false")
        self.assertNotIn("ARTIFACT_BACKENDS", comp.runner.env_defaults)
        self.assertEqual(comp.tests_subdir, "")

    def test_workbenches_still_pytest(self) -> None:
        catalog = load_components_smoke_catalog(default_components_smoke_config_path())
        comp = catalog.components["workbenches"]
        self.assertIsNone(comp.runner)
        self.assertTrue(comp.tests_subdir.startswith("tests/"))

    def test_ogx_uses_envfile_ogx_shift_left_secret(self) -> None:
        catalog = load_components_smoke_catalog(default_components_smoke_config_path())
        comp = catalog.components["ogx"]
        self.assertTrue(comp.requires_shift_left_env)
        self.assertEqual(comp.shift_left_env_secret, "envfile-ogx")
        self.assertEqual(
            shift_left_env_secret_for_component(catalog, "ogx"),
            "envfile-ogx",
        )
        self.assertEqual(
            resolve_shift_left_env_secret(catalog, selected_ids=frozenset({"ogx"})),
            "",
        )

    def test_model_server_uses_catalog_shift_left_secret(self) -> None:
        catalog = load_components_smoke_catalog(default_components_smoke_config_path())
        secret = resolve_shift_left_env_secret(
            catalog, selected_ids=frozenset({"model_server"})
        )
        self.assertEqual(secret, "shiftleft-envfile-model-serving")

    def test_maas_billing_and_ogx_mixed_prepare_uses_model_serving(self) -> None:
        catalog = load_components_smoke_catalog(default_components_smoke_config_path())
        selected = frozenset({"maas_billing", "ogx"})
        self.assertEqual(
            resolve_shift_left_env_secret_for_prepare(catalog, selected_ids=selected),
            "shiftleft-envfile-model-serving",
        )
        self.assertEqual(
            shift_left_env_secret_for_component(catalog, "maas_billing"),
            "shiftleft-envfile-model-serving",
        )
        self.assertEqual(
            shift_left_env_secret_for_component(catalog, "ogx"),
            "envfile-ogx",
        )

    def test_dashboard_cypress_has_cypress_runner(self) -> None:
        catalog = load_components_smoke_catalog(default_components_smoke_config_path())
        comp = catalog.components["dashboard_cypress"]
        self.assertIsNotNone(comp.runner)
        assert comp.runner is not None
        self.assertEqual(comp.runner.type, "cypress-dashboard")
        self.assertIn("cypress-e2e-image", comp.runner.image)
        self.assertEqual(comp.runner.source_repo, "https://github.com/opendatahub-io/odh-dashboard.git")
        self.assertEqual(comp.runner.source_ref, "main")
        self.assertEqual(comp.runner.working_dir, "frontend")
        self.assertEqual(comp.runner.vault_secret_key, "envfile-dashboard-cypress")
        self.assertEqual(comp.runner.phase_commands["smoke"], "cypress-parallel")
        assert comp.runner.cypress is not None
        self.assertEqual(comp.runner.cypress.skip_tags, "@Bug @Maintain @Featureflagged")
        self.assertEqual(len(comp.runner.cypress.gates["smoke"]), 5)
        self.assertEqual(comp.runner.cypress.gates["smoke"][0].grep_tag, "@SmokeSet1")
        assert comp.runner.env_defaults is not None
        self.assertEqual(comp.runner.env_defaults.get("CYPRESS_PARALLEL_TAGS"), "smoke")

    def test_trainer_has_golang_test_tier_runner(self) -> None:
        catalog = load_components_smoke_catalog(default_components_smoke_config_path())
        comp = catalog.components["trainer"]
        self.assertIsNotNone(comp.runner)
        assert comp.runner is not None
        self.assertEqual(comp.runner.type, "golang-test-tier")
        self.assertIn("distributed-workloads-tests", comp.runner.image)
        self.assertEqual(comp.runner.working_dir, "/distributed-workloads/tests")
        self.assertIn("./trainer", comp.runner.phase_commands["smoke"])
        self.assertIn("-testTier=Smoke", comp.runner.phase_commands["smoke"])
        self.assertIn("--junitfile-hide-skipped-tests", comp.runner.phase_commands["smoke"])
        self.assertIn("--junitfile-project-name=trainer", comp.runner.phase_commands["smoke"])
        self.assertEqual(comp.min_rhoai, "3.2")

    def test_distributed_workloads_has_golang_runner(self) -> None:
        catalog = load_components_smoke_catalog(default_components_smoke_config_path())
        comp = catalog.components["distributed_workloads"]
        assert comp.runner is not None
        self.assertIn("./kfto", comp.runner.phase_commands["smoke"])
        self.assertNotIn(
            "--junitfile-hide-skipped-tests",
            comp.runner.phase_commands["smoke"],
        )
        self.assertIn("--junitfile-hide-skipped-tests", comp.runner.phase_commands["tier1"])

    def test_spark_operator_has_platform_e2e_runner(self) -> None:
        catalog = load_components_smoke_catalog(default_components_smoke_config_path())
        comp = catalog.components["spark_operator"]
        assert comp.runner is not None
        self.assertEqual(comp.runner.type, "golang-ginkgo")
        self.assertIn("opendatahub-operator-e2e", comp.runner.image)
        self.assertIn("sparkoperator", comp.runner.phase_commands["smoke"])
        self.assertEqual(comp.min_rhoai, "3.4")

    def test_workbench_images_has_playwright_runner(self) -> None:
        catalog = load_components_smoke_catalog(default_components_smoke_config_path())
        comp = catalog.components["workbench_images"]
        assert comp.runner is not None
        self.assertEqual(comp.runner.type, "playwright")
        self.assertIn("workbench-images-tests", comp.runner.image)
        self.assertIn("playwright test", comp.runner.phase_commands["smoke"])

    def test_codeflare_sdk_has_external_pytest_runner(self) -> None:
        catalog = load_components_smoke_catalog(default_components_smoke_config_path())
        comp = catalog.components["codeflare_sdk"]
        assert comp.runner is not None
        self.assertEqual(comp.runner.type, "external-pytest")
        self.assertEqual(comp.runner.vault_secret_key, "envfile-codeflare-sdk")
        self.assertIn("run-tests.sh", comp.runner.phase_commands["smoke"])

    def test_platform_has_golang_ginkgo_runner_and_run_order_last(self) -> None:
        catalog = load_components_smoke_catalog(default_components_smoke_config_path())
        comp = catalog.components["platform"]
        assert comp.runner is not None
        self.assertEqual(comp.runner.type, "golang-ginkgo")
        self.assertEqual(comp.run_order, "last")
        self.assertIn("run_e2e_tests.sh", comp.runner.phase_commands["smoke"])
        assert comp.runner.env_defaults is not None
        self.assertEqual(comp.runner.env_defaults.get("E2E_TEST_DELETION_POLICY"), "never")

