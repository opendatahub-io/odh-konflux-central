#!/usr/bin/env python3
"""Unit tests for 3.5+ MaaS aigateway path selection."""

from __future__ import annotations

import unittest
from unittest.mock import patch

from install.dsc_install import uses_aigateway_models_as_a_service

_AIGATEWAY_CRD_PATCH = patch(
    "install.dsc_install._dsc_crd_supports_aigateway_models_as_a_service",
    return_value=True,
)


class UsesAigatewayMaasTest(unittest.TestCase):
    def tearDown(self) -> None:
        import install.dsc_install as dsc_install

        dsc_install._aigateway_maas_crd_probed = None
        dsc_install._aigateway_maas_crd_supported = False

    def test_operator_version_35(self) -> None:
        with _AIGATEWAY_CRD_PATCH:
            self.assertTrue(uses_aigateway_models_as_a_service("3.5.0"))

    def test_update_channel_stable_35_env(self) -> None:
        with _AIGATEWAY_CRD_PATCH:
            with patch.dict("os.environ", {"UPDATE_CHANNEL": "stable-3.5"}, clear=False):
                self.assertTrue(uses_aigateway_models_as_a_service(""))

    def test_probed_subscription_channel(self) -> None:
        with _AIGATEWAY_CRD_PATCH:
            with patch.dict("os.environ", {"KUBECONFIG": "/tmp/kc", "UPDATE_CHANNEL": ""}, clear=False):
                with patch(
                    "install.dsc_install._probe_update_channel_from_cluster",
                    return_value="stable-3.5",
                ):
                    with patch("install.dsc_install._resolve_operator_version_for_dsc", return_value=""):
                        self.assertTrue(uses_aigateway_models_as_a_service())

    def test_pre_35_uses_kserve_path(self) -> None:
        with patch.dict("os.environ", {}, clear=True):
            self.assertFalse(uses_aigateway_models_as_a_service("3.4.0"))

    def test_pre_35_ignores_stale_channel_heuristics(self) -> None:
        with patch.dict("os.environ", {"UPDATE_CHANNEL": "stable-3.5"}, clear=False):
            self.assertFalse(uses_aigateway_models_as_a_service("3.4.0"))


if __name__ == "__main__":
    unittest.main()
