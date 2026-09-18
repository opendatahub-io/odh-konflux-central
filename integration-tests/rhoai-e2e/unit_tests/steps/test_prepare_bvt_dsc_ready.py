"""Unit tests for BVT DSC ready gate."""

from __future__ import annotations

import unittest
from unittest.mock import patch

from steps import prepare_bvt_dsc_ready as mod


class PrepareBvtDscReadyTest(unittest.TestCase):
    @patch.dict("os.environ", {"PRODUCT": ""}, clear=False)
    def test_skips_for_existing_product(self) -> None:
        self.assertEqual(mod.prepare_bvt_dsc_ready(), 0)

    @patch.dict("os.environ", {"PRODUCT": "rhoai"}, clear=False)
    @patch("install.dsc_install.dsc_crd_available", return_value=False)
    def test_skips_without_dsc_crd(self, _crd: object) -> None:
        self.assertEqual(mod.prepare_bvt_dsc_ready(), 0)

    @patch.dict("os.environ", {"PRODUCT": "rhoai"}, clear=False)
    @patch("steps.prepare_bvt_apps_namespace.wait_dashboard_pods_ready_for_bvt")
    @patch("components.maas_billing.wait.require_dsc_ready_for_bvt")
    @patch(
        "components.maas_billing.bbr_pre_processing.repair_payload_pre_processing_selector_conflict",
        return_value=False,
    )
    @patch("components.maas_billing.bbr_pre_processing.cleanup_stale_maas_ingress_workloads")
    @patch("install.dsc_install.dsc_crd_available", return_value=True)
    def test_waits_for_dsc_ready_on_rhoai_install(
        self,
        _crd: object,
        _cleanup: object,
        _repair: object,
        wait_ready: object,
        wait_pods: object,
    ) -> None:
        self.assertEqual(mod.prepare_bvt_dsc_ready(), 0)
        wait_ready.assert_called_once()
        wait_pods.assert_called_once()

    @patch.dict("os.environ", {"PRODUCT": "rhoai"}, clear=False)
    @patch(
        "components.maas_billing.wait.require_dsc_ready_for_bvt",
        side_effect=RuntimeError("DSC not Ready"),
    )
    @patch(
        "components.maas_billing.bbr_pre_processing.repair_payload_pre_processing_selector_conflict",
        return_value=False,
    )
    @patch("components.maas_billing.bbr_pre_processing.cleanup_stale_maas_ingress_workloads")
    @patch("install.dsc_install.dsc_crd_available", return_value=True)
    def test_returns_error_when_dsc_not_ready(
        self,
        _crd: object,
        _cleanup: object,
        _repair: object,
        _wait_ready: object,
    ) -> None:
        with patch("steps.prepare_bvt_apps_namespace.wait_dashboard_pods_ready_for_bvt") as wait_pods:
            self.assertEqual(mod.prepare_bvt_dsc_ready(), 1)
            wait_pods.assert_not_called()


if __name__ == "__main__":
    unittest.main()
