#!/usr/bin/env python3
"""Gateway Programmed checks for MaaS smoke prep."""

from __future__ import annotations

import unittest
from unittest.mock import patch

from components.maas_billing.common import (  # noqa: E402
    _AUTHORINO_TLS_ANNOTATION,
    _maas_gateway_annotations_ready,
    _maas_gateway_https_service_ready,
    _maas_gateway_programmed,
    _maas_gateway_ready_for_smoke,
    maas_functional_smoke_ready,
    maas_smoke_acceptable_for_run,
)

class MaasGatewayProgrammedTest(unittest.TestCase):
    @patch("components.maas_billing.common.oc_run")
    def test_programmed_true(self, oc_run: object) -> None:
        oc_run.return_value.returncode = 0
        oc_run.return_value.stdout = "True"
        ready, reason = _maas_gateway_programmed()
        self.assertTrue(ready)
        self.assertEqual(reason, "")

    @patch("components.maas_billing.common.oc_run")
    def test_programmed_false(self, oc_run: object) -> None:
        oc_run.return_value.returncode = 0
        oc_run.return_value.stdout = "False"
        ready, reason = _maas_gateway_programmed()
        self.assertFalse(ready)
        self.assertIn("Programmed=False", reason)

    @patch("components.maas_billing.common.oc_run")
    def test_annotation_read_uses_gateway_json(self, oc_run: object) -> None:
        oc_run.return_value.returncode = 0
        oc_run.return_value.stdout = (
            '{"metadata":{"annotations":{'
            f'"{_AUTHORINO_TLS_ANNOTATION}":"true"'
            "}}}"
        )
        ready, reason = _maas_gateway_annotations_ready()
        self.assertTrue(ready)
        self.assertEqual(reason, "")
        oc_args = oc_run.call_args[0][0]
        self.assertIn("json", oc_args)

    @patch("components.maas_billing.common.cluster_source_is_ephc", return_value=False)
    @patch(
        "components.maas_billing.common._maas_gateway_programmed",
        return_value=(False, "Programmed=Unknown"),
    )
    @patch("components.maas_billing.common._secret_exists", return_value=True)
    @patch("components.maas_billing.auth._authorino_deployment_ready", return_value=True)
    @patch("components.maas_billing.auth._authorino_namespace", return_value="kuadrant-system")
    @patch("install.dependency_operators.maas_dependency_operators_ready", return_value=True)
    @patch("components.maas_billing.common.oc_run")
    def test_functional_ready_requires_programmed(
        self,
        oc_run: object,
        *_mocks: object,
    ) -> None:
        oc_run.return_value.returncode = 0
        ready, reason = maas_functional_smoke_ready()
        self.assertFalse(ready)
        self.assertIn("Programmed", reason)

    @patch("components.maas_billing.common.cluster_source_is_ephc", return_value=True)
    @patch(
        "components.maas_billing.common._dsc_maas_prerequisites_met",
        return_value=(True, "gateway annotations configured"),
    )
    @patch(
        "components.maas_billing.common._maas_gateway_annotations_ready",
        return_value=(False, "missing annotation"),
    )
    @patch(
        "components.maas_billing.common._maas_gateway_programmed",
        return_value=(False, "Programmed=Unknown"),
    )
    def test_ephc_accepts_gateway_when_dsc_prereq_met(self, *_mocks: object) -> None:
        ready, reason = _maas_gateway_ready_for_smoke()
        self.assertTrue(ready)
        self.assertIn("DSC MaaSPrerequisitesMet", reason)

    @patch("components.maas_billing.common.cluster_source_is_ephc", return_value=True)
    @patch(
        "components.maas_billing.common._maas_gateway_annotations_ready",
        return_value=(True, ""),
    )
    @patch(
        "components.maas_billing.common._maas_gateway_programmed",
        return_value=(False, "Programmed=Unknown"),
    )
    def test_ephc_accepts_gateway_without_programmed(self, *_mocks: object) -> None:
        ready, reason = _maas_gateway_ready_for_smoke()
        self.assertTrue(ready)
        self.assertIn("EPHC", reason)

    @patch("components.maas_billing.common.cluster_source_is_ephc", return_value=True)
    @patch(
        "components.maas_billing.common._maas_gateway_annotations_ready",
        return_value=(True, ""),
    )
    @patch(
        "components.maas_billing.common._maas_gateway_programmed",
        return_value=(False, "Programmed=Unknown"),
    )
    @patch("components.maas_billing.common._secret_exists", return_value=True)
    @patch("components.maas_billing.auth._authorino_deployment_ready", return_value=True)
    @patch("components.maas_billing.auth._authorino_namespace", return_value="kuadrant-system")
    @patch("install.dependency_operators.maas_dependency_operators_ready", return_value=True)
    @patch("components.maas_billing.common.oc_run")
    def test_functional_ready_ephc_without_programmed(
        self,
        oc_run: object,
        *_mocks: object,
    ) -> None:
        oc_run.return_value.returncode = 0
        ready, _reason = maas_functional_smoke_ready()
        self.assertTrue(ready)

    @patch("components.maas_billing.common.deps_only_install_dependencies_smoke", return_value=False)
    @patch("components.maas_billing.common._dsc_condition_types", return_value={"MaaSPrerequisitesAvailable"})
    @patch(
        "components.maas_billing.common._dsc_condition",
        side_effect=[
            ("True", "MaaSPrerequisitesMet", "gateway configured"),
            ("True", "Reconciled", "maas-api available"),
            ("True", "Reconciled", "dsc ready"),
        ],
    )
    @patch("install.dependency_operators.maas_dependency_operators_ready", return_value=True)
    @patch(
        "components.maas_billing.common.maas_functional_smoke_ready",
        return_value=(False, "Programmed=Unknown"),
    )
    def test_acceptable_trusts_dsc_when_all_conditions_true(
        self,
        *_mocks: object,
    ) -> None:
        acceptable, reason = maas_smoke_acceptable_for_run()
        self.assertTrue(acceptable)
        self.assertEqual(reason, "")

    @patch("components.maas_billing.common.deps_only_install_dependencies_smoke", return_value=False)
    @patch(
        "components.maas_billing.common.models_as_service_ready_condition_type",
        return_value="ModelsAsAServiceReady",
    )
    @patch(
        "components.maas_billing.common._dsc_condition_types",
        return_value={"ModelsAsAServiceReady", "Ready"},
    )
    @patch(
        "components.maas_billing.common._dsc_condition",
        side_effect=[
            ("", "", ""),
            ("", "", ""),
            ("True", "Reconciled", "dsc ready"),
        ],
    )
    @patch("install.dependency_operators.maas_dependency_operators_ready", return_value=True)
    @patch(
        "components.maas_billing.common.maas_functional_smoke_ready",
        return_value=(True, ""),
    )
    @patch(
        "components.maas_billing.common._maas_gateway_annotations_ready",
        return_value=(True, ""),
    )
    def test_acceptable_functional_when_models_as_a_service_lagging(
        self,
        *_mocks: object,
    ) -> None:
        acceptable, reason = maas_smoke_acceptable_for_run()
        self.assertTrue(acceptable)
        self.assertIn("functional MaaS ready", reason)
        self.assertIn("ModelsAsAServiceReady lagging", reason)


class MaasGatewayHttpsServiceTest(unittest.TestCase):
    @patch("components.maas_billing.common.oc_run")
    def test_https_service_ready_from_gateway_label(self, oc_run: object) -> None:
        oc_run.side_effect = [
            type("R", (), {"returncode": 0, "stdout": '{"items":[{"metadata":{"name":"maas-default-gateway-openshift-default"},"spec":{"ports":[{"port":443,"name":"https"}]}}]}'})(),
        ]
        ready, detail = _maas_gateway_https_service_ready()
        self.assertTrue(ready)
        self.assertIn("openshift-ingress", detail)

    @patch("components.maas_billing.common.oc_run")
    def test_https_service_missing(self, oc_run: object) -> None:
        oc_run.side_effect = [
            type("R", (), {"returncode": 0, "stdout": '{"items":[]}'})(),
            type("R", (), {"returncode": 1, "stdout": ""})(),
        ]
        ready, detail = _maas_gateway_https_service_ready()
        self.assertFalse(ready)
        self.assertIn("no gateway-owned HTTPS service", detail)

