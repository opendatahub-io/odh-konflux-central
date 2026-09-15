"""Unit tests for install phase loading (no cluster)."""

from __future__ import annotations

import os
import unittest
from unittest.mock import patch

from install.install_and_verify import validate_dns_label, validate_operator_namespace

class InstallValidationTest(unittest.TestCase):
    def test_validate_operator_namespace_accepts_default(self) -> None:
        validate_operator_namespace("redhat-ods-operator")

    def test_validate_dns_label_rejects_empty(self) -> None:
        with self.assertRaises(SystemExit):
            validate_dns_label("", "TEST")

class LoadInstallContextTest(unittest.TestCase):
    def test_load_install_context_missing_env(self) -> None:
        env = {k: v for k, v in os.environ.items() if not k.startswith("INSTALL_")}
        with patch.dict(os.environ, env, clear=True):
            with self.assertRaises(SystemExit):
                from install.install_phases import load_install_context

                load_install_context()


class PostInstallDscTest(unittest.TestCase):
    def test_phase_post_install_dsc_defers_aigateway_when_maas_api_missing(self) -> None:
        from install.install_phases import phase_post_install_dsc

        with patch.dict(os.environ, {"COMPONENTS_CSV": "maas_billing"}, clear=False):
            with patch("install.install_phases.setup_dsc_resources"):
                with patch("install.install_phases._ensure_gateway_before_dsc_ready"):
                    with patch("install.install_phases.wait_dsc_ready", return_value=True):
                        with patch("install.install_phases.ensure_rhoai_gateway_for_install"):
                            with patch("install.install_phases.gateway_config_ready", return_value=True):
                                with patch(
                                    "components.maas_billing.common.maas_api_deployment_exists",
                                    return_value=False,
                                ):
                                    with patch(
                                        "install.install_phases.ensure_dsc_models_as_service",
                                    ) as dsc:
                                        phase_post_install_dsc(None)  # type: ignore[arg-type]
                                        dsc.assert_called_once_with(wait_for_aigateway=False)
