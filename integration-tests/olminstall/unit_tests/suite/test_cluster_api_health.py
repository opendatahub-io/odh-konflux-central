#!/usr/bin/env python3
"""Unit tests for cluster API unreachable detection."""

from __future__ import annotations

import unittest
from unittest.mock import MagicMock, patch

from suite.cluster_api_health import (
    cluster_api_unreachable_reason,
    cluster_api_unreachable_text,
    cluster_smoke_infra_blocked_reason,
    is_definitive_infra_error,
    operator_admission_webhook_unavailable_reason,
    openshift_console_route_unavailable_reason,
    openshift_guest_rh_ai_route_tekton_unreachable_reason,
)


class ClusterApiUnreachableTextTest(unittest.TestCase):
    def test_detects_no_such_host(self) -> None:
        msg = cluster_api_unreachable_text(
            stderr="dial tcp: lookup foo.elb.amazonaws.com on 172.30.0.10:53: no such host",
        )
        self.assertIn("cluster API unreachable", msg)
        self.assertIn("no such host", msg)

    def test_empty_when_healthy_error(self) -> None:
        msg = cluster_api_unreachable_text(stderr='Error from server (NotFound): datascienceclusters "default-dsc" not found')
        self.assertEqual(msg, "")

    def test_missing_crd_type_is_not_api_death(self) -> None:
        msg = cluster_api_unreachable_text(
            stderr='error: the server doesn\'t have a resource type "datasciencecluster"',
        )
        self.assertEqual(msg, "")


class ClusterApiUnreachableReasonTest(unittest.TestCase):
    @patch("suite.cluster_api_health.oc_run")
    def test_probe_retries_before_reporting_dns_failure(self, mock_oc_run: MagicMock) -> None:
        mock_oc_run.return_value = MagicMock(
            returncode=1,
            stderr="Unable to connect to the server: dial tcp: lookup x: no such host",
            stdout="",
        )
        with patch("suite.cluster_api_health.time.sleep") as mock_sleep:
            reason = cluster_api_unreachable_reason()
        self.assertIn("cluster API unreachable", reason)
        self.assertEqual(mock_oc_run.call_count, 3)
        self.assertEqual(mock_sleep.call_count, 2)

    @patch("suite.cluster_api_health.oc_run")
    def test_probe_empty_when_cluster_responds(self, mock_oc_run: MagicMock) -> None:
        mock_oc_run.return_value = MagicMock(returncode=0, stderr="", stdout="")
        self.assertEqual(cluster_api_unreachable_reason(), "")


class DefinitiveInfraErrorTest(unittest.TestCase):
    def test_api_death(self) -> None:
        self.assertTrue(
            is_definitive_infra_error("cluster API unreachable: no such host"),
        )

    def test_maas_https(self) -> None:
        self.assertTrue(
            is_definitive_infra_error("MaaS gateway HTTPS service not ready after 480s"),
        )

    def test_transient_warn(self) -> None:
        self.assertFalse(is_definitive_infra_error("Cannot build maas-gateway-auth: policy missing"))

    def test_webhook_no_endpoints(self) -> None:
        self.assertTrue(
            is_definitive_infra_error(
                "cluster API unreachable: rhods-operator-service webhook has no endpoints in redhat-ods-operator",
            ),
        )


class OperatorWebhookUnavailableReasonTest(unittest.TestCase):
    @patch("install.dsc_install._discover_operator_admission_webhook_service", return_value="rhods-operator-service")
    @patch("suite.cluster_api_health.oc_run")
    def test_detects_missing_endpoints(self, mock_oc_run: MagicMock, _mock_discover: MagicMock) -> None:
        mock_oc_run.return_value = MagicMock(returncode=0, stdout="", stderr="")
        with patch("suite.cluster_api_health.time.sleep") as mock_sleep:
            reason = operator_admission_webhook_unavailable_reason()
        self.assertIn("webhook has no endpoints", reason)
        self.assertEqual(mock_oc_run.call_count, 3)
        self.assertGreaterEqual(mock_sleep.call_count, 2)

    @patch("install.dsc_install._discover_operator_admission_webhook_service", return_value="rhods-operator-service")
    @patch("suite.cluster_api_health.oc_run")
    def test_empty_when_endpoints_exist(self, mock_oc_run: MagicMock, _mock_discover: MagicMock) -> None:
        mock_oc_run.return_value = MagicMock(returncode=0, stdout="10.0.0.1", stderr="")
        self.assertEqual(operator_admission_webhook_unavailable_reason(), "")


class OpenShiftConsoleRouteUnavailableReasonTest(unittest.TestCase):
    @patch("suite.cluster_api_health.oc_run")
    def test_detects_console_dns_failure(self, mock_oc_run: MagicMock) -> None:
        mock_oc_run.return_value = MagicMock(
            returncode=1,
            stderr="Failed to resolve downloads-openshift-console.apps.example.osp.rh-ods.com",
            stdout="",
        )
        reason = openshift_console_route_unavailable_reason()
        self.assertIn("openshift console route unreachable", reason)

    @patch("suite.cluster_api_health.socket.getaddrinfo", side_effect=OSError("no such host"))
    @patch("suite.cluster_api_health.oc_run")
    def test_skips_dns_probe_for_konflux_ocp_ci_guest_console(self, mock_oc_run: MagicMock, _mock_dns: MagicMock) -> None:
        mock_oc_run.return_value = MagicMock(
            returncode=0,
            stderr="",
            stdout="https://console-openshift-console.apps.34de191502702cfbdd1d.prod.konflux-ocp-ci.dev\n",
        )
        self.assertEqual(openshift_console_route_unavailable_reason(), "")

    @patch("suite.cluster_api_health.socket.getaddrinfo", side_effect=OSError("no such host"))
    @patch("suite.cluster_api_health.oc_run")
    def test_probes_console_hostname_after_whoami_success(self, mock_oc_run: MagicMock, _mock_dns: MagicMock) -> None:
        mock_oc_run.return_value = MagicMock(
            returncode=0,
            stderr="",
            stdout="https://console-openshift-console.apps.example.osp.rh-ods.com\n",
        )
        reason = openshift_console_route_unavailable_reason()
        self.assertIn("openshift console route unreachable", reason)
        self.assertIn("no such host", reason)

    @patch("suite.cluster_api_health.socket.getaddrinfo", return_value=[(None, None, None, None, ("10.0.0.1", 443))])
    @patch("suite.cluster_api_health.oc_run")
    def test_empty_when_console_hostname_resolves(self, mock_oc_run: MagicMock, _mock_dns: MagicMock) -> None:
        mock_oc_run.return_value = MagicMock(
            returncode=0,
            stderr="",
            stdout="https://console-openshift-console.apps.example.osp.rh-ods.com\n",
        )
        self.assertEqual(openshift_console_route_unavailable_reason(), "")


class OpenshiftGuestRhAiRouteTektonUnreachableTest(unittest.TestCase):
    @patch("suite.cluster_api_health._extended_ephc_infra_probes_enabled", return_value=False)
    def test_empty_when_not_in_pipeline(self, _mock_extended: MagicMock) -> None:
        self.assertEqual(openshift_guest_rh_ai_route_tekton_unreachable_reason(), "")

    @patch("suite.cluster_api_health._extended_ephc_infra_probes_enabled", return_value=True)
    @patch("suite.cluster_api_health.socket.getaddrinfo", side_effect=OSError("no such host"))
    @patch("suite.cluster_api_health.oc_run")
    def test_reports_rh_ai_dns_on_oci_guest(
        self,
        mock_oc_run: MagicMock,
        _mock_dns: MagicMock,
        _mock_extended: MagicMock,
    ) -> None:
        mock_oc_run.side_effect = [
            MagicMock(
                returncode=0,
                stderr="",
                stdout="https://console-openshift-console.apps.abc.prod.konflux-ocp-ci.dev\n",
            ),
            MagicMock(
                returncode=0,
                stderr="",
                stdout="rh-ai.apps.abc.prod.konflux-ocp-ci.dev",
            ),
        ]
        reason = openshift_guest_rh_ai_route_tekton_unreachable_reason()
        self.assertIn("openshift console route unreachable", reason)
        self.assertIn("no such host", reason)


class ClusterSmokeInfraBlockedReasonTest(unittest.TestCase):
    @patch("suite.cluster_api_health._clear_cluster_api_unreachable_marker")
    @patch("suite.cluster_api_health.cluster_api_unreachable_reason", return_value="")
    @patch("suite.cluster_api_health._persist_cluster_api_unreachable")
    @patch("suite.cluster_api_health._prior_cluster_api_unreachable_reason", return_value="prior elb death")
    def test_clears_stale_marker_when_api_recovers(
        self,
        _mock_prior: MagicMock,
        mock_persist: MagicMock,
        mock_api: MagicMock,
        mock_clear: MagicMock,
    ) -> None:
        with patch(
            "suite.cluster_api_health._extended_ephc_infra_probes_enabled",
            return_value=False,
        ):
            self.assertEqual(cluster_smoke_infra_blocked_reason(), "")
        mock_api.assert_called_once_with(probe=True)
        mock_clear.assert_called_once_with()
        mock_persist.assert_not_called()

    @patch("suite.cluster_api_health._prior_cluster_api_unreachable_reason", return_value="prior elb death")
    def test_returns_prior_marker_without_probe(
        self,
        _mock_prior: MagicMock,
    ) -> None:
        with patch(
            "suite.cluster_api_health.cluster_api_unreachable_reason",
        ) as mock_api:
            self.assertEqual(cluster_smoke_infra_blocked_reason(probe=False), "prior elb death")
            mock_api.assert_not_called()

    @patch("suite.cluster_api_health._extended_ephc_infra_probes_enabled", return_value=False)
    @patch("suite.cluster_api_health.cluster_api_unreachable_reason")
    def test_api_only_when_not_in_pipeline(
        self,
        mock_api: MagicMock,
        _mock_extended: MagicMock,
    ) -> None:
        mock_api.return_value = ""
        with patch(
            "suite.cluster_api_health.operator_admission_webhook_unavailable_reason",
            return_value="webhook dead",
        ) as mock_webhook:
            self.assertEqual(cluster_smoke_infra_blocked_reason(), "")
            mock_webhook.assert_not_called()

    @patch("suite.cluster_api_health._extended_ephc_infra_probes_enabled", return_value=True)
    @patch("suite.cluster_api_health.openshift_console_route_unavailable_reason", return_value="console dead")
    @patch("suite.cluster_api_health.operator_admission_webhook_unavailable_reason", return_value="")
    @patch("suite.cluster_api_health.cluster_api_unreachable_reason")
    def test_returns_console_reason_when_not_oci_guest(
        self,
        mock_api: MagicMock,
        _mock_webhook: MagicMock,
        mock_console: MagicMock,
        _mock_extended: MagicMock,
    ) -> None:
        mock_api.return_value = ""
        self.assertEqual(cluster_smoke_infra_blocked_reason(), "console dead")

    @patch("suite.cluster_api_health._persist_cluster_api_unreachable")
    @patch("suite.cluster_api_health._prior_cluster_api_unreachable_reason", return_value="")
    @patch("suite.cluster_api_health._extended_ephc_infra_probes_enabled", return_value=True)
    @patch("suite.cluster_api_health.openshift_console_route_unavailable_reason", return_value="")
    @patch("suite.cluster_api_health.operator_admission_webhook_unavailable_reason", return_value="")
    @patch("suite.cluster_api_health.cluster_api_unreachable_reason")
    def test_returns_first_non_empty(
        self,
        mock_api: MagicMock,
        _mock_webhook: MagicMock,
        _mock_console: MagicMock,
        _mock_extended: MagicMock,
        _mock_prior: MagicMock,
        mock_persist: MagicMock,
    ) -> None:
        mock_api.return_value = "cluster API unreachable: no such host"
        self.assertEqual(
            cluster_smoke_infra_blocked_reason(),
            "cluster API unreachable: no such host",
        )
        mock_persist.assert_called_once_with("cluster API unreachable: no such host")


if __name__ == "__main__":
    unittest.main()
