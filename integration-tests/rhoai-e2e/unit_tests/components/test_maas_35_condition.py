#!/usr/bin/env python3
"""Unit tests for RHOAI 3.5 MaaS DSC condition and maas-api namespace helpers."""

from __future__ import annotations

import unittest
from unittest.mock import MagicMock, patch

from components.maas_billing.common import (
    maas_api_deployment_exists,
    maas_api_namespace,
    models_as_service_ready_condition_type,
)


class ModelsAsServiceReadyConditionTest(unittest.TestCase):
    @patch(
        "components.maas_billing.common._dsc_condition_types",
        return_value={"Ready", "ModelsAsAServiceReady"},
    )
    def test_prefers_models_as_a_service_ready_on_35(self, _types) -> None:
        self.assertEqual(
            models_as_service_ready_condition_type(),
            "ModelsAsAServiceReady",
        )

    @patch(
        "components.maas_billing.common._dsc_condition_types",
        return_value={"Ready", "ModelsAsServiceReady"},
    )
    def test_falls_back_to_legacy_condition(self, _types) -> None:
        self.assertEqual(
            models_as_service_ready_condition_type(),
            "ModelsAsServiceReady",
        )


class MaasApiNamespaceTest(unittest.TestCase):
    @patch("components.maas_billing.common.oc_run")
    def test_prefers_redhat_ai_gateway_infra(self, mock_oc) -> None:
        def side_effect(args, **kwargs):
            ns = args[4] if len(args) > 4 else ""
            if ns == "redhat-ai-gateway-infra":
                return MagicMock(returncode=0)
            return MagicMock(returncode=1)

        mock_oc.side_effect = side_effect
        self.assertEqual(maas_api_namespace(), "redhat-ai-gateway-infra")
        self.assertTrue(maas_api_deployment_exists())

    @patch("components.maas_billing.common.oc_run", return_value=MagicMock(returncode=1))
    def test_defaults_to_apps_namespace_when_missing(self, _mock_oc) -> None:
        self.assertEqual(maas_api_namespace(), "redhat-ods-applications")
        self.assertFalse(maas_api_deployment_exists())


if __name__ == "__main__":
    raise SystemExit(unittest.main())
