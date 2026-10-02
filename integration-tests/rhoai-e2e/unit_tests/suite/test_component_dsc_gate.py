#!/usr/bin/env python3
"""Unit tests for DSC disabled probes (no cluster)."""

from __future__ import annotations

import unittest
from unittest.mock import patch

from suite.component_dsc_gate import (
    dsc_disabled_reason_from_states,
    llama_stack_smoke_prereq_reason_from_states,
    smoke_component_dsc_disabled,
    smoke_component_prereq_reason_from_states,
    smoke_component_prereq_unavailable,
    wait_for_smoke_dsc_ready_after_patch,
)

class DscDisabledReasonTest(unittest.TestCase):
    def test_workbenches_removed_via_ready_condition(self) -> None:
        reason = dsc_disabled_reason_from_states(
            "workbenches",
            management_states={"dashboard": "Managed", "workbenches": "Managed"},
            ready_reasons={"WorkbenchesReady": "Removed"},
        )
        self.assertEqual(reason, "WorkbenchesReady reason=Removed")

    def test_maas_billing_removed_via_models_as_service(self) -> None:
        reason = dsc_disabled_reason_from_states(
            "maas_billing",
            management_states={"kserve": "Managed"},
            models_as_service_state="Removed",
            operator_version="3.4.0",
        )
        self.assertEqual(reason, "spec.components.kserve.modelsAsService.managementState=Removed")

    def test_maas_billing_removed_via_aigateway_models_as_a_service_on_35(self) -> None:
        with patch(
            "install.dsc_install._dsc_crd_supports_aigateway_models_as_a_service",
            return_value=True,
        ):
            reason = dsc_disabled_reason_from_states(
                "maas_billing",
                management_states={"kserve": "Managed", "aigateway": "Managed"},
                models_as_service_state="Removed",
                operator_version="3.5.0",
            )
        self.assertEqual(
            reason,
            "spec.components.aigateway.modelsAsAService.managementState=Removed",
        )

    def test_maas_billing_enabled(self) -> None:
        reason = dsc_disabled_reason_from_states(
            "maas_billing",
            management_states={"kserve": "Managed"},
            models_as_service_state="Managed",
            ready_reasons={"ModelsAsServiceReady": "Reconciled"},
        )
        self.assertEqual(reason, "")

    def test_ai_pipelines_removed_via_management_state(self) -> None:
        reason = dsc_disabled_reason_from_states(
            "ai_pipelines",
            management_states={"aipipelines": "Removed"},
        )
        self.assertEqual(reason, "spec.components.aipipelines.managementState=Removed")

    @patch("suite.component_dsc_gate._resolve_operator_version_for_dsc", return_value="3.5.1")
    def test_distributed_workloads_35_ignores_trainingoperator_removed(self, _version) -> None:
        reason = dsc_disabled_reason_from_states(
            "distributed_workloads",
            management_states={"trainer": "Managed", "trainingoperator": "Removed"},
            ready_reasons={"TrainingOperatorReady": "Removed", "TrainerReady": "Reconciled"},
        )
        self.assertEqual(reason, "")

    @patch("suite.component_dsc_gate._resolve_operator_version_for_dsc", return_value="3.4.0")
    def test_distributed_workloads_pre35_blocks_on_trainingoperator_removed(self, _version) -> None:
        reason = dsc_disabled_reason_from_states(
            "distributed_workloads",
            management_states={"trainingoperator": "Managed"},
            ready_reasons={"TrainingOperatorReady": "Removed"},
        )
        self.assertEqual(reason, "TrainingOperatorReady reason=Removed")

class LlamaStackSmokePrereqTest(unittest.TestCase):
    def test_ready_when_condition_true(self) -> None:
        reason = llama_stack_smoke_prereq_reason_from_states(
            management_states={"llamastackoperator": "Managed"},
            ready_status="True",
        )
        self.assertEqual(reason, "")

    def test_missing_crd_when_no_ready_condition(self) -> None:
        reason = llama_stack_smoke_prereq_reason_from_states(
            management_states={"llamastackoperator": "Managed"},
            exposes_ready_condition=False,
            crd_present=False,
        )
        self.assertIn("llamastackdistributions.llamastack.io", reason)

    def test_crd_present_without_ready_condition(self) -> None:
        reason = llama_stack_smoke_prereq_reason_from_states(
            management_states={"llamastackoperator": "Managed"},
            exposes_ready_condition=False,
            crd_present=True,
        )
        self.assertEqual(reason, "")

class ComponentSmokePrereqTest(unittest.TestCase):
    def test_workbenches_not_ready_when_condition_false(self) -> None:
        reason = smoke_component_prereq_reason_from_states(
            "workbenches",
            management_states={"workbenches": "Managed", "dashboard": "Managed"},
            ready_status="False",
            ready_reason="Progressing",
            exposes_ready_condition=True,
        )
        self.assertIn("WorkbenchesReady status=False", reason)

    def test_maas_billing_missing_authorino(self) -> None:
        reason = smoke_component_prereq_reason_from_states(
            "maas_billing",
            management_states={"kserve": "Managed"},
            models_as_service_state="Managed",
            maas_deps_ready=False,
        )
        self.assertIn("Authorino", reason)

    def test_dashboard_cypress_allows_gateway_without_dashboard_ready(self) -> None:
        reason = smoke_component_prereq_reason_from_states(
            "dashboard_cypress",
            management_states={"dashboard": "Managed"},
            ready_status="False",
            ready_reason="Progressing",
            exposes_ready_condition=True,
            gateway_url_reachable=True,
        )
        self.assertEqual(reason, "")

    def test_dashboard_cypress_blocks_when_gateway_unreachable(self) -> None:
        reason = smoke_component_prereq_reason_from_states(
            "dashboard_cypress",
            management_states={"dashboard": "Managed"},
            gateway_url="https://rh-ai.example.com",
            gateway_url_reachable=False,
        )
        self.assertIn("not reachable", reason)

    def test_dashboard_cypress_requires_reachable_gateway_even_when_dashboard_ready(self) -> None:
        reason = smoke_component_prereq_reason_from_states(
            "dashboard_cypress",
            management_states={"dashboard": "Managed"},
            gateway_url="https://rh-ai.example.com",
            gateway_url_reachable=False,
            ready_status="True",
        )
        self.assertIn("not reachable", reason)

    @patch("suite.component_dsc_gate._resolve_operator_version_for_dsc", return_value="3.5.1")
    def test_distributed_workloads_35_ready_when_trainer_true(self, _version) -> None:
        reason = smoke_component_prereq_reason_from_states(
            "distributed_workloads",
            management_states={"trainer": "Managed", "trainingoperator": "Removed"},
            ready_status="True",
            ready_reason="Reconciled",
            exposes_ready_condition=True,
        )
        self.assertEqual(reason, "")

    @patch("suite.component_dsc_gate._resolve_operator_version_for_dsc", return_value="3.5.1")
    def test_distributed_workloads_35_not_blocked_by_trainingoperator_removed(self, _version) -> None:
        reason = smoke_component_prereq_reason_from_states(
            "distributed_workloads",
            management_states={"trainer": "Managed", "trainingoperator": "Removed"},
            ready_reason="Removed",
            exposes_ready_condition=False,
        )
        self.assertEqual(reason, "TrainerReady reason=Removed")


class ClusterApiPrereqTest(unittest.TestCase):
    @patch("suite.component_dsc_gate.cluster_smoke_infra_blocked_reason")
    @patch("suite.component_dsc_gate.smoke_component_dsc_disabled")
    def test_api_unreachable_blocks_before_dsc_probe(
        self,
        mock_disabled,
        mock_api,
    ) -> None:
        mock_api.return_value = "cluster API unreachable: no such host"
        unavailable, reason = smoke_component_prereq_unavailable("kuberay")
        self.assertTrue(unavailable)
        self.assertIn("cluster API unreachable", reason)
        mock_disabled.assert_not_called()


class SmokeComponentDscDisabledTest(unittest.TestCase):
    @patch("suite.component_dsc_gate._models_as_service_management_state", return_value="Removed")
    @patch("suite.component_dsc_gate.components_need_models_as_service", return_value=True)
    @patch("suite.component_dsc_gate._dsc_smoke_managed_components", return_value=[])
    @patch("suite.component_dsc_gate._resolve_operator_version_for_dsc", return_value="3.4.0")
    @patch("suite.component_dsc_gate.oc_run")
    def test_maas_removed_returns_tuple(
        self,
        oc_run,
        _version,
        _keys,
        _needs_maas,
        _maas_state,
    ) -> None:
        oc_run.return_value = type("R", (), {"returncode": 0, "stdout": "", "stderr": ""})()
        disabled, reason = smoke_component_dsc_disabled("maas_billing")
        self.assertTrue(disabled)
        self.assertIn("modelsAsService.managementState=Removed", reason)

    @patch("suite.component_dsc_gate._resolve_operator_version_for_dsc", return_value="3.5.1")
    @patch("suite.component_dsc_gate._dsc_smoke_managed_components", return_value={"trainer"})
    @patch("suite.component_dsc_gate._component_management_state", return_value="Managed")
    @patch("suite.component_dsc_gate._dsc_condition")
    @patch("suite.component_dsc_gate.oc_run")
    def test_distributed_workloads_35_uses_trainer_ready_not_trainingoperator(
        self,
        oc_run,
        dsc_condition,
        _mgmt,
        _keys,
        _version,
    ) -> None:
        oc_run.return_value = type("R", (), {"returncode": 0, "stdout": "", "stderr": ""})()

        def cond_side_effect(ready_type: str) -> tuple[str, str, str]:
            if ready_type == "TrainerReady":
                return ("True", "Reconciled", "")
            if ready_type == "TrainingOperatorReady":
                return ("False", "Removed", "deprecated")
            return ("?", "?", "")

        dsc_condition.side_effect = cond_side_effect
        disabled, reason = smoke_component_dsc_disabled("distributed_workloads")
        self.assertFalse(disabled)
        self.assertEqual(reason, "")
        checked = [call.args[0] for call in dsc_condition.call_args_list]
        self.assertIn("TrainerReady", checked)
        self.assertNotIn("TrainingOperatorReady", checked)


class WaitForSmokeDscReadyTest(unittest.TestCase):
    @patch("suite.component_dsc_gate._dsc_condition_types", return_value=["WorkbenchesReady"])
    @patch("suite.component_dsc_gate._dsc_condition", side_effect=[("True", "Reconciled", "")])
    def test_returns_when_ready(self, _cond, _types) -> None:
        wait_for_smoke_dsc_ready_after_patch("workbenches", timeout_sec=5)

    @patch("suite.component_dsc_gate._dsc_condition_types", return_value=[])
    def test_skips_when_condition_not_exposed(self, _types) -> None:
        wait_for_smoke_dsc_ready_after_patch("workbenches", timeout_sec=5)

    @patch("suite.component_dsc_gate._dsc_smoke_managed_components", return_value={"workbenches"})
    @patch("suite.component_dsc_gate._component_management_state", return_value="Managed")
    @patch("suite.component_dsc_gate._dsc_condition_types", return_value=["WorkbenchesReady"])
    @patch("suite.component_dsc_gate._dsc_condition")
    @patch("suite.component_dsc_gate.time.sleep")
    def test_waits_past_stale_removed_reason(
        self,
        _sleep,
        cond,
        _types,
        _mgmt,
        _keys,
    ) -> None:
        cond.side_effect = [
            ("False", "Removed", "reconciling"),
            ("True", "Reconciled", ""),
        ] + [("True", "Reconciled", "")] * 20
        wait_for_smoke_dsc_ready_after_patch("workbenches", timeout_sec=60)

    @patch("suite.component_dsc_gate._dsc_smoke_managed_components", return_value={"kserve"})
    @patch("suite.component_dsc_gate._component_management_state", return_value="Managed")
    @patch("suite.component_dsc_gate._dsc_condition_types", return_value=["ModelsAsServiceReady"])
    @patch("suite.component_dsc_gate._dsc_condition")
    @patch("suite.component_dsc_gate.time.sleep")
    def test_skips_maas_wait_when_prerequisites_not_met(
        self,
        _sleep,
        cond,
        _types,
        _mgmt,
        _keys,
    ) -> None:
        cond.return_value = (
            "False",
            "PrerequisitesNotMet",
            "database Secret 'maas-db-config' not found",
        )
        wait_for_smoke_dsc_ready_after_patch("maas_billing", timeout_sec=60)
        _sleep.assert_not_called()

    @patch("suite.component_dsc_gate._dsc_condition_types", return_value=["KserveReady", "ModelsAsServiceReady"])
    @patch("suite.component_dsc_gate._dsc_condition")
    @patch("suite.component_dsc_gate.time.sleep")
    def test_batch_wait_defers_models_as_service_for_maas_matrix(
        self,
        _sleep,
        cond,
        _types,
    ) -> None:
        from suite.component_dsc_gate import wait_for_smoke_dsc_ready_batch

        cond.return_value = ("True", "Reconciled", "")
        wait_for_smoke_dsc_ready_batch(
            {"maas_billing", "model_server", "workbenches"},
            timeout_sec=60,
        )
        called_types = {call.args[0] for call in cond.call_args_list}
        self.assertNotIn("ModelsAsServiceReady", called_types)
        self.assertIn("KserveReady", called_types)

    @patch("suite.component_dsc_gate._dsc_smoke_managed_components", return_value={"ogx"})
    @patch("suite.component_dsc_gate._dsc_condition_types", return_value=["OGXReady"])
    @patch("suite.component_dsc_gate._dsc_condition")
    @patch("suite.component_dsc_gate.time.sleep")
    def test_batch_wait_fails_fast_when_ogx_reason_error(
        self,
        sleep,
        cond,
        _types,
        _keys,
    ) -> None:
        from suite.component_dsc_gate import wait_for_smoke_dsc_ready_batch

        cond.return_value = (
            "False",
            "Error",
            "LlamaStackOperator is set to Managed, it has been deprecated, please set it to Removed",
        )
        with self.assertRaises(RuntimeError) as ctx:
            wait_for_smoke_dsc_ready_batch({"ogx"}, timeout_sec=600)
        self.assertIn("OGXReady", str(ctx.exception))
        self.assertIn("Error", str(ctx.exception))
        sleep.assert_not_called()

    @patch("suite.component_dsc_gate._dsc_smoke_managed_components", return_value={"workbenches"})
    @patch("suite.component_dsc_gate._dsc_condition_types", return_value=["WorkbenchesReady"])
    @patch("suite.component_dsc_gate._dsc_condition")
    def test_wait_succeeds_if_ready_on_timeout_recheck(self, cond, _types, _keys) -> None:
        clock_t = {"t": 1_000.0}

        def fake_time() -> float:
            return clock_t["t"]

        def fake_sleep(seconds: float) -> None:
            clock_t["t"] += seconds

        def fake_cond(_name: str) -> tuple[str, str, str]:
            if clock_t["t"] >= 1_005.0:
                return ("True", "Reconciled", "")
            return ("False", "DeploymentsNotReady", "0/1 deployments ready")

        cond.side_effect = fake_cond
        with patch("suite.component_dsc_gate.time.time", fake_time):
            with patch("suite.component_dsc_gate.time.sleep", fake_sleep):
                wait_for_smoke_dsc_ready_after_patch("workbenches", timeout_sec=5)

    @patch("suite.component_dsc_gate.smoke_component_dsc_disabled", return_value=(False, ""))
    @patch("components.maas_billing.common.maas_functional_smoke_ready", return_value=(True, ""))
    @patch("components.maas_billing.common.deps_only_install_dependencies_smoke", return_value=True)
    def test_model_server_available_on_deps_only_functional_gate(
        self,
        _deps_only,
        _functional,
        _disabled,
    ) -> None:
        unavailable, reason = smoke_component_prereq_unavailable("model_server")
        self.assertFalse(unavailable)
        self.assertEqual(reason, "")

    @patch("suite.component_dsc_gate.smoke_component_dsc_disabled", return_value=(False, ""))
    @patch(
        "components.maas_billing.common.maas_functional_smoke_ready",
        return_value=(False, "Authorino deployment not ready in kuadrant-system"),
    )
    @patch("components.maas_billing.common.deps_only_install_dependencies_smoke", return_value=True)
    def test_model_server_blocked_when_functional_gate_fails(
        self,
        _deps_only,
        _functional,
        _disabled,
    ) -> None:
        unavailable, reason = smoke_component_prereq_unavailable("model_server")
        self.assertTrue(unavailable)
        self.assertIn("Authorino deployment not ready", reason)

if __name__ == "__main__":
    raise SystemExit(unittest.main())
