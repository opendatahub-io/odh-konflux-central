#!/usr/bin/env python3
"""Unit tests for MaaS gateway YAML generation (no cluster)."""

from __future__ import annotations

import os
import unittest
from unittest.mock import MagicMock, patch

from components.maas_billing.common import _maas_smoke_ready  # noqa: E402
from components.maas_billing.gateway import _gateway_route_yaml, _gateway_yaml  # noqa: E402
from components.maas_billing.uwm import _user_workload_monitoring_yaml  # noqa: E402
from components.maas_billing.auth import (  # noqa: E402
    _maas_auth_policy_accepted,
    _wait_maas_api_auth_policy_accepted,
    ensure_maas_auth_gateway_ready,
    ensure_maas_auth_policy_ready,
    ensure_maas_authorino_ready,
)
from components.maas_billing.gateway import ensure_maas_api_auth_policy  # noqa: E402

class MaasGatewayYamlTest(unittest.TestCase):
    def test_includes_authorino_tls_bootstrap_annotation(self) -> None:
        yaml_doc = _gateway_yaml("apps.example.com", "router-certs-default")
        self.assertIn("security.opendatahub.io/authorino-tls-bootstrap: \"true\"", yaml_doc)
        self.assertIn("hostname: \"maas.apps.example.com\"", yaml_doc)

class MaasGatewayRouteYamlTest(unittest.TestCase):
    def test_passthrough_route_to_gateway_service(self) -> None:
        yaml_doc = _gateway_route_yaml("apps.example.com")
        self.assertIn("host: maas.apps.example.com", yaml_doc)
        self.assertIn("termination: passthrough", yaml_doc)
        self.assertIn("name: maas-default-gateway-openshift-default", yaml_doc)
        self.assertIn("targetPort: 443", yaml_doc)

class UserWorkloadMonitoringYamlTest(unittest.TestCase):
    def test_enables_user_workload_monitoring(self) -> None:
        yaml_doc = _user_workload_monitoring_yaml()
        self.assertIn("name: cluster-monitoring-config", yaml_doc)
        self.assertIn("namespace: openshift-monitoring", yaml_doc)
        self.assertIn("enableUserWorkload: true", yaml_doc)

class MaasSmokeReadyTest(unittest.TestCase):
    def test_requires_prereq_condition_when_exposed(self) -> None:
        self.assertFalse(
            _maas_smoke_ready(
                prereq_status="",
                maas_status="True",
                ready_status="True",
                require_prereq_condition=True,
            )
        )
        self.assertTrue(
            _maas_smoke_ready(
                prereq_status="True",
                maas_status="True",
                ready_status="True",
                require_prereq_condition=True,
            )
        )

    def test_skips_missing_prereq_condition_on_rhoai_34(self) -> None:
        self.assertTrue(
            _maas_smoke_ready(
                prereq_status="",
                maas_status="True",
                ready_status="True",
                require_prereq_condition=False,
            )
        )
        self.assertFalse(
            _maas_smoke_ready(
                prereq_status="",
                maas_status="False",
                ready_status="True",
                require_prereq_condition=False,
            )
        )

class MaasAuthGatewayReadyTest(unittest.TestCase):
    def test_maas_auth_policy_accepted_true(self) -> None:
        ok = MagicMock(returncode=0, stdout="True")
        with patch("components.maas_billing.auth._oc_run", return_value=ok):
            self.assertTrue(_maas_auth_policy_accepted())

    def test_maas_auth_policy_accepted_false(self) -> None:
        pending = MagicMock(returncode=0, stdout="False")
        with patch("components.maas_billing.auth._oc_run", return_value=pending):
            self.assertFalse(_maas_auth_policy_accepted())

    def test_wait_restarts_kuadrant_on_missing_dependency(self) -> None:
        calls: list[list[str]] = []

        def fake_oc_run(args, **kwargs):
            calls.append(list(args))
            if args[:2] == ["get", "authpolicy"] and "Accepted" in (args[-1] if args else ""):
                return MagicMock(returncode=0, stdout="False")
            if args[:3] == ["get", "kuadrant", "kuadrant"]:
                return MagicMock(returncode=0, stdout="False\tMissingDependency")
            return MagicMock(returncode=0, stdout="")

        with patch("components.maas_billing.gateway.ensure_maas_api_auth_policy"):
            with patch("components.maas_billing.auth._oc_run", side_effect=fake_oc_run):
                with patch("components.maas_billing.auth._sleep"):
                    with self.assertRaises(RuntimeError):
                        _wait_maas_api_auth_policy_accepted(timeout_sec=1)

        delete_calls = [c for c in calls if c[:2] == ["delete", "pod"]]
        self.assertGreaterEqual(len(delete_calls), 1)
        self.assertIn("control-plane=controller-manager", delete_calls[0])

    def test_wait_proceeds_on_functional_readiness_after_grace(self) -> None:
        with patch.dict(os.environ, {"MAAS_AUTH_POLICY_ACCEPT_GRACE_SEC": "0"}):
            with patch("components.maas_billing.auth._maas_auth_policy_accepted", return_value=False):
                with patch(
                    "components.maas_billing.auth._maas_auth_policy_functionally_ready",
                    return_value=True,
                ):
                    with patch(
                        "components.maas_billing.auth._kuadrant_ready_status",
                        return_value=("True", ""),
                    ):
                        with patch("components.maas_billing.gateway.ensure_maas_api_auth_policy"):
                            with patch(
                                "components.maas_billing.auth._maas_api_deployment_ready_now",
                                return_value=False,
                            ):
                                with patch("components.maas_billing.auth._sleep"):
                                    _wait_maas_api_auth_policy_accepted(timeout_sec=5)

    @patch("components.maas_billing.gateway.oc_run")
    @patch("components.maas_billing.gateway._clone_models_as_a_service")
    @patch(
        "components.maas_billing.gateway.maas_api_auth_validate_url",
        return_value="https://maas-api.redhat-ai-gateway-infra.svc.cluster.local:8443/internal/v1/api-keys/validate",
    )
    def test_ensure_maas_api_auth_policy_replaces_stale_callback(
        self,
        _validate_url,
        clone_repo,
        oc_run,
    ) -> None:
        stale = "https://maas-api.redhat-ods-applications.svc.cluster.local:8443/internal/v1/api-keys/validate"
        expected = "https://maas-api.redhat-ai-gateway-infra.svc.cluster.local:8443/internal/v1/api-keys/validate"
        policy_path_calls = {"n": 0}

        def side_effect(args, **kwargs):
            if args[:2] == ["delete", "authpolicy"] and args[3] == "-n":
                return MagicMock(returncode=0, stdout="")
            if args[:2] == ["get", "authpolicy"]:
                policy_path_calls["n"] += 1
                url = stale if policy_path_calls["n"] == 1 else expected
                return MagicMock(returncode=0, stdout=url)
            if args[:2] == ["apply", "-n"]:
                return MagicMock(returncode=0, stdout="")
            return MagicMock(returncode=0, stdout="")

        oc_run.side_effect = side_effect
        repo = MagicMock()
        repo.__truediv__ = lambda self, other: MagicMock(
            is_file=lambda: True,
            read_text=lambda encoding: (
                "https://maas-api.placehold.svc.cluster.local:8443/internal/v1/api-keys/validate"
            ),
        )
        clone_repo.return_value = repo
        ensure_maas_api_auth_policy()
        delete_calls = [c for c in [list(call.args[0]) for call in oc_run.call_args_list] if c[:2] == ["delete", "authpolicy"]]
        self.assertTrue(any(c[2] == "maas-api-auth-policy" for c in delete_calls))

    @patch("components.maas_billing.auth._restart_maas_auth_workloads")
    @patch("components.maas_billing.auth._wait_maas_api_auth_policy_accepted")
    @patch("components.maas_billing.auth._wait_maas_api_deployment_ready")
    @patch("components.maas_billing.auth._run_post_install_rhcl", return_value=True)
    @patch("components.maas_billing.auth._wait_authorino_workload_ready", return_value="kuadrant-system")
    def test_ensure_maas_authorino_ready_orchestrates(
        self,
        _wait_authorino,
        run_rhcl,
        _wait_maas_api,
        _wait_policy,
        _restart,
    ) -> None:
        ns = ensure_maas_authorino_ready()
        self.assertEqual(ns, "kuadrant-system")
        _wait_authorino.assert_called_once_with(timeout_sec=600)
        run_rhcl.assert_called_once()

    @patch("components.maas_billing.auth._restart_maas_auth_workloads")
    @patch("components.maas_billing.auth._wait_maas_api_auth_policy_accepted")
    @patch("components.maas_billing.auth._wait_maas_api_deployment_ready")
    def test_ensure_maas_auth_policy_ready_orchestrates(
        self,
        wait_maas_api,
        wait_policy,
        restart_workloads,
    ) -> None:
        ensure_maas_auth_policy_ready(authorino_ns="kuadrant-system")
        wait_maas_api.assert_called_once_with(timeout_sec=600)
        wait_policy.assert_called_once_with(timeout_sec=600)
        restart_workloads.assert_called_once_with("kuadrant-system")

    @patch("components.maas_billing.auth.ensure_maas_auth_policy_ready")
    @patch("components.maas_billing.auth.ensure_maas_authorino_ready", return_value="kuadrant-system")
    def test_ensure_maas_auth_gateway_ready_orchestrates(
        self,
        authorino_ready,
        policy_ready,
    ) -> None:
        ensure_maas_auth_gateway_ready()
        authorino_ready.assert_called_once()
        policy_ready.assert_called_once_with(authorino_ns="kuadrant-system")

if __name__ == "__main__":
    raise SystemExit(unittest.main())
