#!/usr/bin/env python3
"""Unit tests for AIGateway modelsAsAService sync on RHOAI 3.5+."""

from __future__ import annotations

import os
import unittest
from unittest.mock import MagicMock, patch

from install.dsc_install import (
    _dsc_crd_supports_aigateway_models_as_a_service,
    ensure_aigateway_models_as_a_service_managed,
    uses_aigateway_models_as_a_service,
)


class EnsureAigatewayMaasTest(unittest.TestCase):
    def setUp(self) -> None:
        self._hpa_rbac_patcher = patch(
            "components.maas_billing.bbr_pre_processing.ensure_maas_controller_openshift_ingress_hpa_rbac",
        )
        self._hpa_rbac_patcher.start()

    def tearDown(self) -> None:
        self._hpa_rbac_patcher.stop()
        import install.dsc_install as dsc_install

        dsc_install._aigateway_maas_crd_probed = None
        dsc_install._aigateway_maas_crd_supported = False

    @patch("install.dsc_install.oc_run")
    def test_uses_kserve_when_dsc_crd_lacks_aigateway_maas(self, mock_oc) -> None:
        mock_oc.return_value = MagicMock(returncode=1, stdout="", stderr="not found")
        self.assertFalse(_dsc_crd_supports_aigateway_models_as_a_service())
        self.assertFalse(uses_aigateway_models_as_a_service("3.5.0-ea.2"))

    @patch("install.dsc_install.oc_run")
    def test_uses_aigateway_when_dsc_crd_exposes_maas_field(self, mock_oc) -> None:
        mock_oc.return_value = MagicMock(
            returncode=0,
            stdout="FIELD: modelsAsAService <Object>\n",
            stderr="",
        )
        self.assertTrue(_dsc_crd_supports_aigateway_models_as_a_service())
        self.assertTrue(uses_aigateway_models_as_a_service("3.5.0"))
    @patch("install.dsc_install.uses_aigateway_models_as_a_service", return_value=False)
    def test_skips_before_35(self, _use) -> None:
        with patch("install.dsc_install.oc_run") as mock_oc:
            ensure_aigateway_models_as_a_service_managed()
        mock_oc.assert_not_called()

    @patch("install.dsc_install._wait_aigateway_models_as_a_service_reconciled")
    @patch("install.dsc_install._aigateway_models_as_a_service_state", return_value="Removed")
    @patch("install.dsc_install._cr_exists", return_value=True)
    @patch("install.dsc_install.uses_aigateway_models_as_a_service", return_value=True)
    def test_patches_when_not_managed(self, _use, _exists, _state, mock_wait) -> None:
        with patch("install.dsc_install.oc_run", return_value=MagicMock(returncode=0)) as mock_oc:
            ensure_aigateway_models_as_a_service_managed(wait_timeout_sec=1)
        patch_call = mock_oc.call_args_list[0].args[0]
        self.assertEqual(patch_call[0], "patch")
        self.assertEqual(patch_call[1], "aigateway")
        mock_wait.assert_called_once_with(timeout_sec=1)

    @patch("install.dsc_install._wait_aigateway_models_as_a_service_reconciled")
    @patch("install.dsc_install._aigateway_models_as_a_service_state", return_value="Managed")
    @patch("install.dsc_install._cr_exists", return_value=True)
    @patch("install.dsc_install.uses_aigateway_models_as_a_service", return_value=True)
    def test_waits_without_patch_when_already_managed(self, _use, _exists, _state, mock_wait) -> None:
        with patch("install.dsc_install.oc_run") as mock_oc:
            ensure_aigateway_models_as_a_service_managed(wait_timeout_sec=5)
        mock_oc.assert_not_called()
        mock_wait.assert_called_once()
        self.assertLessEqual(mock_wait.call_args.kwargs["timeout_sec"], 5)

    @patch("install.dsc_install._wait_aigateway_models_as_a_service_reconciled")
    @patch("install.dsc_install._aigateway_models_as_a_service_state", return_value="Managed")
    @patch("install.dsc_install._cr_exists", return_value=True)
    @patch("install.dsc_install.uses_aigateway_models_as_a_service", return_value=True)
    def test_default_wait_uses_maas_prep_timeout(self, _use, _exists, _state, mock_wait) -> None:
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("MAAS_PREP_TIMEOUT_SEC", None)
            with patch("install.dsc_install.oc_run") as mock_oc:
                ensure_aigateway_models_as_a_service_managed()
        mock_oc.assert_not_called()
        mock_wait.assert_called_once()
        self.assertGreaterEqual(mock_wait.call_args.kwargs["timeout_sec"], 890)
        self.assertLessEqual(mock_wait.call_args.kwargs["timeout_sec"], 900)

    @patch("install.dsc_install.ensure_aigateway_models_as_a_service_managed")
    @patch("install.dsc_install._cr_exists", return_value=True)
    @patch("install.dsc_install.uses_aigateway_models_as_a_service", return_value=True)
    def test_ensure_dsc_models_as_service_forwards_wait_budget(
        self, _use, _exists, mock_aigateway
    ) -> None:
        from install.dsc_install import ensure_dsc_models_as_service

        with patch("install.dsc_install.oc_run", return_value=MagicMock(returncode=0)):
            ensure_dsc_models_as_service(wait_timeout_sec=600)
        mock_aigateway.assert_called_once_with(wait_timeout_sec=600, wait=True)

    @patch("install.dsc_install.ensure_aigateway_models_as_a_service_managed")
    @patch("install.dsc_install._cr_exists", return_value=True)
    @patch("install.dsc_install.uses_aigateway_models_as_a_service", return_value=True)
    def test_ensure_dsc_models_as_service_can_defer_aigateway_wait(
        self, _use, _exists, mock_aigateway
    ) -> None:
        from install.dsc_install import ensure_dsc_models_as_service

        with patch("install.dsc_install.oc_run", return_value=MagicMock(returncode=0)):
            ensure_dsc_models_as_service(wait_for_aigateway=False)
        mock_aigateway.assert_called_once_with(wait_timeout_sec=900, wait=False)

    @patch("install.dsc_install._cr_exists", return_value=False)
    @patch("install.dsc_install.uses_aigateway_models_as_a_service", return_value=True)
    def test_raises_when_cr_missing_at_timeout(self, _use, _exists) -> None:
        with patch("install.dsc_install.time.sleep"), patch(
            "install.dsc_install.time.time", side_effect=[0.0, 0.0, 181.0]
        ):
            with self.assertRaisesRegex(RuntimeError, "not found after"):
                ensure_aigateway_models_as_a_service_managed(wait_timeout_sec=180)

    @patch("install.dsc_install._wait_aigateway_models_as_a_service_reconciled")
    @patch("install.dsc_install._cr_exists", return_value=False)
    @patch("install.dsc_install.uses_aigateway_models_as_a_service", return_value=True)
    def test_defers_when_cr_missing_and_wait_disabled(self, _use, _exists, mock_wait) -> None:
        with patch("install.dsc_install.oc_run") as mock_oc:
            ensure_aigateway_models_as_a_service_managed(wait=False)
        mock_oc.assert_not_called()
        mock_wait.assert_not_called()

    @patch(
        "install.dsc_install._wait_aigateway_models_as_a_service_reconciled",
        side_effect=RuntimeError("not reconciled after 5s"),
    )
    @patch("install.dsc_install._aigateway_models_as_a_service_state", return_value="Managed")
    @patch("install.dsc_install._cr_exists", return_value=True)
    @patch("install.dsc_install.uses_aigateway_models_as_a_service", return_value=True)
    def test_raises_when_reconcile_wait_fails(self, _use, _exists, _state, _wait) -> None:
        with self.assertRaisesRegex(RuntimeError, "not reconciled after 5s"):
            ensure_aigateway_models_as_a_service_managed(wait_timeout_sec=5)

    @patch("install.dsc_install.time.sleep")
    @patch("install.dsc_install.time.time", side_effect=[0.0, 1.0, 2.0])
    @patch("install.dsc_install._AIGATEWAY_CR", "default-aigateway")
    def test_wait_requires_ready_replicas_not_only_deployment_object(self, _time, _sleep) -> None:
        from install.dsc_install import _wait_aigateway_models_as_a_service_reconciled

        responses = [
            MagicMock(returncode=0, stdout="0"),
            MagicMock(returncode=0, stdout="1"),
        ]

        def fake_oc(args, **kwargs):
            if args[:3] == ["get", "deployment", "maas-api"]:
                return responses.pop(0)
            return MagicMock(returncode=1, stdout="", stderr="")

        with patch("install.dsc_install.oc_run", side_effect=fake_oc):
            _wait_aigateway_models_as_a_service_reconciled(timeout_sec=30)

    @patch("install.dsc_install._nudge_maas_api_after_aigateway_deployments")
    @patch("install.dsc_install.time.sleep")
    @patch("install.dsc_install.time.time")
    def test_deployments_available_without_maas_api_keeps_waiting(
        self, mock_time, _sleep, mock_nudge
    ) -> None:
        from install.dsc_install import _AIGATEWAY_CR, _wait_aigateway_models_as_a_service_reconciled

        class _AdvancingClock:
            def __init__(self) -> None:
                self.value = 0.0

            def __call__(self) -> float:
                self.value += 30.0
                return self.value

        mock_time.side_effect = _AdvancingClock()

        def fake_oc(args, **kwargs):
            if args[:3] == ["get", "deployment", "maas-api"]:
                return MagicMock(returncode=1, stdout="", stderr="not found")
            if args[:3] == ["get", "aigateway", _AIGATEWAY_CR]:
                if "observedGeneration" in args[-1]:
                    return MagicMock(returncode=0, stdout="1\t1")
                if "DeploymentsAvailable" in args[-1]:
                    return MagicMock(returncode=0, stdout="True")
            return MagicMock(returncode=1, stdout="", stderr="")

        with patch("install.dsc_install.oc_run", side_effect=fake_oc):
            with self.assertRaisesRegex(RuntimeError, "not reconciled after"):
                _wait_aigateway_models_as_a_service_reconciled(timeout_sec=120)
        mock_nudge.assert_called_once_with(cycle_spec=True)

    @patch("install.dsc_install.time.time", return_value=1000.0)
    def test_nudge_restarts_both_operators_and_annotates(self, _time) -> None:
        from install.dsc_install import _AIGATEWAY_CR, _nudge_maas_api_after_aigateway_deployments

        rollout_calls: list[str] = []

        def fake_oc(args, **kwargs):
            if args[:2] == ["rollout", "restart"]:
                rollout_calls.append(args[2])
                return MagicMock(returncode=0)
            if args[:2] == ["patch", "aigateway"]:
                return MagicMock(returncode=0)
            return MagicMock(returncode=1)

        with (
            patch(
                "components.maas_billing.bbr_pre_processing.ensure_maas_controller_openshift_ingress_hpa_rbac",
            ),
            patch("install.dsc_install.oc_run", side_effect=fake_oc),
        ):
            _nudge_maas_api_after_aigateway_deployments(cycle_spec=False)
        self.assertEqual(
            rollout_calls,
            [
                "deployment/ai-gateway-operator",
                "deployment/maas-controller",
            ],
        )

    @patch("install.dsc_install.time.sleep")
    @patch("install.dsc_install._cr_exists", return_value=True)
    @patch("install.dsc_install.uses_aigateway_models_as_a_service", return_value=True)
    def test_cycle_patches_dsc_and_aigateway(self, _use, _exists, _sleep) -> None:
        from install.dsc_install import _cycle_aigateway_models_as_a_service_state

        patched: list[tuple[str, str, str]] = []

        def fake_oc(args, **kwargs):
            if args[:2] == ["patch", "datasciencecluster"]:
                patched.append(("dsc", args[2], kwargs.get("capture_output") and "ok" or "ok"))
                return MagicMock(returncode=0)
            if args[:2] == ["patch", "aigateway"]:
                patched.append(("aigateway", args[2], "ok"))
                return MagicMock(returncode=0)
            return MagicMock(returncode=1)

        with patch("install.dsc_install.oc_run", side_effect=fake_oc):
            _cycle_aigateway_models_as_a_service_state()
        self.assertEqual([p[0] for p in patched], ["dsc", "aigateway", "dsc", "aigateway"])


if __name__ == "__main__":
    raise SystemExit(unittest.main())
