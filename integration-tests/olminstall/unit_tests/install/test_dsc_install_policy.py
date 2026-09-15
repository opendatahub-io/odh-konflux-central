#!/usr/bin/env python3
"""Unit tests for olminstall-dsc-install.yaml resolution."""

from __future__ import annotations

import unittest
from unittest.mock import patch

from install.dsc_install import _build_dsc_smoke_yaml
from install.dsc_install_policy import (
    default_dsc_install_policy_path,
    load_dsc_install_policy,
    resolve_managed_dsc_keys,
    stale_removed_dsc_keys_for_smoke,
)

class DscInstallPolicyTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.policy_path = default_dsc_install_policy_path()
        cls.doc = load_dsc_install_policy(cls.policy_path)

    def test_policy_loads_smoke_mappings(self) -> None:
        self.assertIn("ogx", self.doc.smoke_components)
        self.assertEqual(self.doc.smoke_components["ogx"].keys, ("ogx",))

    def test_35_install_defers_ogx_llama_spark(self) -> None:
        csv = "ogx,llama_stack,spark_operator,trainer,workbenches"
        managed = resolve_managed_dsc_keys(
            csv,
            "3.5.0-ea.2",
            for_install=True,
        )
        self.assertIn("trainer", managed)
        self.assertIn("workbenches", managed)
        self.assertNotIn("ogx", managed)
        self.assertNotIn("llamastackoperator", managed)
        self.assertNotIn("sparkoperator", managed)
        self.assertNotIn("trainingoperator", managed)

    def test_install_path_skips_components_smoke_catalog(self) -> None:
        with patch(
            "suite.component_catalog.load_components_smoke_catalog",
            side_effect=AssertionError("catalog must not load during install DSC resolve"),
        ):
            managed = resolve_managed_dsc_keys(
                "llama_stack,ogx",
                "3.5.0-ea.2",
                for_install=True,
            )
        self.assertEqual(managed, set())

    def test_runtime_prep_enables_ogx(self) -> None:
        managed = resolve_managed_dsc_keys("ogx", for_install=False)
        self.assertEqual(managed, {"ogx"})

    def test_pre35_trainer_includes_trainingoperator(self) -> None:
        managed = resolve_managed_dsc_keys("trainer", "3.4.0", for_install=True)
        self.assertEqual(managed, {"trainer", "trainingoperator"})

    def test_build_yaml_matches_jenkins_35_install(self) -> None:
        csv = "ogx,llama_stack,spark_operator,trainer,workbenches"
        yaml_doc = _build_dsc_smoke_yaml(
            csv,
            defer_for_install=True,
            operator_version="3.5.0-ea.2",
            enable_models_as_service=False,
        )
        self.assertIn("    ogx:\n      managementState: Removed", yaml_doc)
        self.assertIn("    llamastackoperator:\n      managementState: Removed", yaml_doc)
        self.assertIn("    sparkoperator:\n      managementState: Removed", yaml_doc)
        self.assertIn("    trainer:\n      managementState: Managed", yaml_doc)
        self.assertIn("    trainingoperator:\n      managementState: Removed", yaml_doc)

    def test_stale_removed_for_ai_safety_on_35(self) -> None:
        stale = stale_removed_dsc_keys_for_smoke("ai_safety", "3.5.0-ea.2")
        self.assertIn("ogx", stale)
        self.assertIn("llamastackoperator", stale)
        self.assertNotIn("trustyai", stale)

    def test_stale_removed_llamastack_when_ogx_managed_on_35(self) -> None:
        stale = stale_removed_dsc_keys_for_smoke("ogx", "3.5.0-ea.2")
        self.assertIn("llamastackoperator", stale)
        self.assertNotIn("ogx", stale)

    def test_full_matrix_on_35_skips_llama_stack_dsc_keys(self) -> None:
        csv = (
            "workbenches,model_registry,model_server,model_runtime,maas_billing,"
            "ai_pipelines,kuberay,mlflow,ogx,ai_safety,llama_stack,dashboard_cypress"
        )
        managed = resolve_managed_dsc_keys(csv, "3.5.0-ea.2", for_install=False)
        self.assertIn("ogx", managed)
        self.assertNotIn("llamastackoperator", managed)

    def test_ogx_selection_drops_llamastack_even_without_operator_version(self) -> None:
        managed = resolve_managed_dsc_keys("ogx", "", for_install=False)
        self.assertIn("ogx", managed)
        self.assertNotIn("llamastackoperator", managed)

    def test_ogx_with_llama_stack_keeps_llamastackoperator(self) -> None:
        managed = resolve_managed_dsc_keys("ogx,llama_stack", "", for_install=False)
        self.assertIn("ogx", managed)
        self.assertIn("llamastackoperator", managed)

    def test_ai_safety_evalhub_enables_trustyai(self) -> None:
        managed = resolve_managed_dsc_keys("ai_safety_evalhub", "3.6.0-ea.1", for_install=False)
        self.assertEqual(managed, {"trustyai", "kserve"})

    def test_distributed_workloads_36_uses_trainer_not_trainingoperator(self) -> None:
        managed = resolve_managed_dsc_keys(
            "distributed_workloads",
            "3.6.0-ea.1",
            for_install=False,
        )
        self.assertEqual(managed, {"ray", "trainer"})
        self.assertNotIn("trainingoperator", managed)


class BatchEnsureDscManagedTest(unittest.TestCase):
    @patch("install.dsc_install._resolve_operator_version_for_dsc", return_value="")
    @patch("install.dsc_install._cr_exists", return_value=True)
    @patch("install.dsc_install.dsc_component_management_state", return_value="Managed")
    @patch("install.dsc_install.ensure_dsc_component_removed")
    @patch("install.dsc_install.ensure_dsc_component_managed")
    def test_does_not_reenable_llamastack_when_ogx_selected(
        self,
        ensure_managed,
        ensure_removed,
        _state,
        _exists,
        _ver,
    ) -> None:
        from install.dsc_install import batch_ensure_dsc_managed_for_smoke

        batch_ensure_dsc_managed_for_smoke({"ogx"})
        ensure_removed.assert_called_with("llamastackoperator")
        managed_keys = [call.args[0] for call in ensure_managed.call_args_list]
        self.assertIn("ogx", managed_keys)
        self.assertNotIn("llamastackoperator", managed_keys)

    @patch("install.dsc_install._resolve_operator_version_for_dsc", return_value="")
    @patch("install.dsc_install._cr_exists", return_value=True)
    @patch("install.dsc_install.dsc_component_management_state", return_value="Managed")
    @patch("install.dsc_install.ensure_dsc_component_removed")
    @patch("install.dsc_install.ensure_dsc_component_managed")
    def test_keeps_llamastack_when_ogx_and_llama_stack_selected(
        self,
        ensure_managed,
        ensure_removed,
        _state,
        _exists,
        _ver,
    ) -> None:
        from install.dsc_install import batch_ensure_dsc_managed_for_smoke

        batch_ensure_dsc_managed_for_smoke({"ogx", "llama_stack"})
        ensure_removed.assert_not_called()


if __name__ == "__main__":
    raise SystemExit(unittest.main())
