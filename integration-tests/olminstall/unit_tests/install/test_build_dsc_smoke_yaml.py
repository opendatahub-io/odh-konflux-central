#!/usr/bin/env python3
"""Unit tests for smoke DSC YAML generation (no cluster)."""

from __future__ import annotations

import unittest

from install.dsc_install import _build_dsc_smoke_yaml

class BuildDscSmokeYamlTest(unittest.TestCase):
    def test_maas_billing_enables_kserve_models_as_service(self) -> None:
        yaml_doc = _build_dsc_smoke_yaml("maas_billing", operator_version="3.4.0")
        self.assertIn("modelsAsService:", yaml_doc)

    def test_maas_billing_enables_aigateway_models_as_a_service_on_35(self) -> None:
        yaml_doc = _build_dsc_smoke_yaml("maas_billing", operator_version="3.5.0")
        self.assertIn("    aigateway:", yaml_doc)
        self.assertIn("      modelsAsAService:", yaml_doc)
        self.assertNotIn("      modelsAsService:", yaml_doc)

    def test_install_path_defers_models_as_service(self) -> None:
        yaml_doc = _build_dsc_smoke_yaml("maas_billing,model_server", enable_models_as_service=False)
        self.assertIn("    kserve:", yaml_doc)
        self.assertIn("      managementState: Managed", yaml_doc)
        self.assertNotIn("modelsAsService:", yaml_doc)

    def test_kserve_smoke_install_uses_raw_deployment(self) -> None:
        yaml_doc = _build_dsc_smoke_yaml("model_server", enable_models_as_service=False)
        self.assertIn("      defaultDeploymentMode: RawDeployment", yaml_doc)
        self.assertIn("        managementState: Removed", yaml_doc)
        self.assertIn("        name: knative-serving", yaml_doc)

    def test_maas_billing_does_not_enable_dashboard(self) -> None:
        yaml_doc = _build_dsc_smoke_yaml("maas_billing")
        self.assertIn("    dashboard:\n      managementState: Removed", yaml_doc)
        self.assertIn("    workbenches:\n      managementState: Removed", yaml_doc)

    def test_workbenches_enables_dashboard(self) -> None:
        yaml_doc = _build_dsc_smoke_yaml("workbenches")
        self.assertIn("    dashboard:\n      managementState: Managed", yaml_doc)
        self.assertIn("    workbenches:\n      managementState: Managed", yaml_doc)

    def test_ai_pipelines_uses_aipipelines_key(self) -> None:
        yaml_doc = _build_dsc_smoke_yaml("ai_pipelines")
        self.assertIn("    aipipelines:", yaml_doc)
        self.assertNotIn("datasciencepipelines", yaml_doc)

    def test_dashboard_cypress_enables_smoke_dsc_stack(self) -> None:
        yaml_doc = _build_dsc_smoke_yaml("dashboard_cypress")
        self.assertIn("    dashboard:\n      managementState: Managed", yaml_doc)
        self.assertIn("    modelregistry:\n      managementState: Managed", yaml_doc)
        self.assertIn("    workbenches:\n      managementState: Managed", yaml_doc)
        self.assertIn("    kserve:\n      managementState: Managed", yaml_doc)
        self.assertIn("    aipipelines:\n      managementState: Managed", yaml_doc)
        self.assertIn("    feastoperator:\n      managementState: Managed", yaml_doc)
        self.assertNotIn("      modelsAsService:", yaml_doc)

    def test_llama_stack_enables_llamastackoperator(self) -> None:
        yaml_doc = _build_dsc_smoke_yaml("llama_stack")
        self.assertIn("    llamastackoperator:", yaml_doc)

    def test_trainer_enables_trainer_trainingoperator(self) -> None:
        yaml_doc = _build_dsc_smoke_yaml("trainer", operator_version="3.4.0")
        self.assertIn("    trainer:\n      managementState: Managed", yaml_doc)
        self.assertIn("    trainingoperator:\n      managementState: Managed", yaml_doc)
        self.assertIn("    kueue:\n      managementState: Removed", yaml_doc)

    def test_trainer_35_install_defers_trainingoperator(self) -> None:
        yaml_doc = _build_dsc_smoke_yaml(
            "trainer",
            defer_for_install=True,
            operator_version="3.5.0-ea.2",
        )
        self.assertIn("    trainer:\n      managementState: Managed", yaml_doc)
        self.assertIn("    trainingoperator:\n      managementState: Removed", yaml_doc)

    def test_35_install_defers_ogx_llama_spark(self) -> None:
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
        self.assertIn("    workbenches:\n      managementState: Managed", yaml_doc)

    def test_ogx_enables_ogx_component(self) -> None:
        yaml_doc = _build_dsc_smoke_yaml("ogx")
        self.assertIn("    ogx:\n      managementState: Managed", yaml_doc)

    def test_distributed_workloads_enables_trainer_ray_on_36(self) -> None:
        yaml_doc = _build_dsc_smoke_yaml(
            "distributed_workloads",
            operator_version="3.6.0-ea.1",
        )
        self.assertIn("    trainer:\n      managementState: Managed", yaml_doc)
        self.assertIn("    ray:\n      managementState: Managed", yaml_doc)
        self.assertIn("    trainingoperator:\n      managementState: Removed", yaml_doc)
        self.assertIn("    kueue:\n      managementState: Removed", yaml_doc)

    def test_distributed_workloads_pre35_includes_trainingoperator(self) -> None:
        yaml_doc = _build_dsc_smoke_yaml(
            "distributed_workloads",
            operator_version="3.4.0",
        )
        self.assertIn("    trainingoperator:\n      managementState: Managed", yaml_doc)
        self.assertIn("    trainer:\n      managementState: Managed", yaml_doc)
        self.assertIn("    ray:\n      managementState: Managed", yaml_doc)

    def test_spark_operator_enables_sparkoperator(self) -> None:
        yaml_doc = _build_dsc_smoke_yaml("spark_operator")
        self.assertIn("    sparkoperator:\n      managementState: Managed", yaml_doc)

if __name__ == "__main__":
    raise SystemExit(unittest.main())
