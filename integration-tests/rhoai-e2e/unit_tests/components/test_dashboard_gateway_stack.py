"""Tests for dashboard gateway stack health checks."""

from __future__ import annotations

import unittest
from unittest.mock import patch

from components.dashboard_cypress.runtime import (
    gateway_auth_stack_ready,
    verify_gateway_stack_healthy,
)


class VerifyGatewayStackHealthyTest(unittest.TestCase):
    @patch("components.dashboard_cypress.runtime.oc_run")
    def test_returns_true_when_gateway_and_authorino_ready(self, oc_run_mock: object) -> None:
        def _side_effect(cmd: list[str], **_kwargs: object) -> object:
            joined = " ".join(cmd)
            if "kube-auth-proxy" in joined and "jsonpath" in joined:
                return type("R", (), {"returncode": 0, "stdout": "1", "stderr": ""})()
            if "data-science-gateway" in joined and "jsonpath" in joined:
                return type("R", (), {"returncode": 0, "stdout": "2", "stderr": ""})()
            if joined.startswith("get deployment/authorino") and "jsonpath" not in joined:
                return type("R", (), {"returncode": 0, "stdout": "", "stderr": ""})()
            if "deployment/authorino" in joined and "jsonpath" in joined:
                return type("R", (), {"returncode": 0, "stdout": "1", "stderr": ""})()
            return type("R", (), {"returncode": 1, "stdout": "", "stderr": ""})()

        oc_run_mock.side_effect = _side_effect
        self.assertTrue(verify_gateway_stack_healthy())

    @patch("components.dashboard_cypress.runtime.oc_run")
    def test_returns_false_when_kube_auth_proxy_missing(self, oc_run_mock: object) -> None:
        oc_run_mock.return_value = type("R", (), {"returncode": 1, "stdout": "", "stderr": ""})()
        self.assertFalse(verify_gateway_stack_healthy())


class GatewayAuthStackReadyTest(unittest.TestCase):
    @patch("components.maas_billing.auth.authorino_workload_tls_ready", return_value=True)
    @patch("install.dependency_operators.maas_dependency_operators_ready", return_value=True)
    def test_ready_when_deps_and_tls_ok(self, _maas: object, _authorino: object) -> None:
        self.assertTrue(gateway_auth_stack_ready())

    @patch("components.maas_billing.auth.authorino_workload_tls_ready", return_value=True)
    @patch("install.dependency_operators.maas_dependency_operators_ready", return_value=False)
    def test_not_ready_when_maas_deps_missing(self, _maas: object, _authorino: object) -> None:
        self.assertFalse(gateway_auth_stack_ready())

    @patch("components.maas_billing.auth.authorino_workload_tls_ready", return_value=False)
    @patch("install.dependency_operators.maas_dependency_operators_ready", return_value=True)
    def test_not_ready_when_authorino_tls_missing(self, _maas: object, _authorino: object) -> None:
        self.assertFalse(gateway_auth_stack_ready())


if __name__ == "__main__":
    raise SystemExit(unittest.main())
