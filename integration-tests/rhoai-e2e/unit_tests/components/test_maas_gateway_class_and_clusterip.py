#!/usr/bin/env python3
"""GatewayClass + EPHC HTTPS ClusterIP fallback."""

from __future__ import annotations

import unittest
from unittest.mock import patch

from components.maas_billing.gateway import (
    ensure_maas_gateway_https_service_clusterip,
    ensure_openshift_default_gateway_class,
    wait_openshift_default_gateway_class_accepted,
)


class GatewayClassEnsureTest(unittest.TestCase):
    @patch("components.maas_billing.gateway.oc_run")
    def test_skips_when_accepted(self, oc_run: object) -> None:
        oc_run.return_value.returncode = 0
        oc_run.return_value.stdout = "True"
        ensure_openshift_default_gateway_class()
        self.assertEqual(oc_run.call_count, 1)
        self.assertEqual(oc_run.call_args[0][0][1], "gatewayclass")

    @patch("components.maas_billing.gateway.oc_run")
    def test_applies_when_missing(self, oc_run: object) -> None:
        get_r = type("R", (), {"returncode": 1, "stdout": "", "stderr": "not found"})()
        apply_r = type("R", (), {"returncode": 0, "stdout": "created", "stderr": ""})()
        oc_run.side_effect = [get_r, apply_r]
        ensure_openshift_default_gateway_class()
        self.assertEqual(oc_run.call_count, 2)
        apply_args = oc_run.call_args_list[1]
        self.assertEqual(apply_args[0][0][:2], ["apply", "-f"])
        stdin = apply_args.kwargs.get("stdin_text") or ""
        self.assertIn("kind: GatewayClass", stdin)
        self.assertIn("name: openshift-default", stdin)


class GatewayClassWaitTest(unittest.TestCase):
    @patch("components.maas_billing.gateway.openshift_default_gateway_class_accepted")
    @patch("components.maas_billing.gateway.ensure_openshift_default_gateway_class")
    def test_returns_true_when_already_accepted(
        self,
        ensure: object,
        accepted: object,
    ) -> None:
        accepted.return_value = True
        self.assertTrue(wait_openshift_default_gateway_class_accepted(timeout_sec=30))
        ensure.assert_called_once()

    @patch("components.maas_billing.gateway.time.sleep")
    @patch("components.maas_billing.gateway.time.time")
    @patch(
        "components.maas_billing.gateway.openshift_default_gateway_class_accepted",
        side_effect=[False, True],
    )
    @patch("components.maas_billing.gateway.ensure_openshift_default_gateway_class")
    def test_polls_until_accepted(
        self,
        ensure: object,
        accepted: object,
        mock_time: object,
        sleep: object,
    ) -> None:
        mock_time.side_effect = [0, 0, 0, 1]
        accepted.side_effect = [False, False, True]
        self.assertTrue(wait_openshift_default_gateway_class_accepted(timeout_sec=30))
        sleep.assert_called()


class EaasHttpsClusterIpTest(unittest.TestCase):
    @patch("install.gateway_config.cluster_source_is_ephc", return_value=True)
    @patch(
        "install.rosa_hcp_pull_setup.is_hypershift_managed_cluster",
        return_value=True,
    )
    @patch(
        "components.maas_billing.common._maas_gateway_https_service_ready",
        side_effect=[(False, "missing"), (True, "openshift-ingress/svc")],
    )
    @patch("components.maas_billing.gateway.oc_run")
    def test_creates_clusterip_when_pods_exist(
        self,
        oc_run: object,
        *_mocks: object,
    ) -> None:
        pods = type(
            "R",
            (),
            {"returncode": 0, "stdout": "gw-pod-1", "stderr": ""},
        )()
        apply = type("R", (), {"returncode": 0, "stdout": "created", "stderr": ""})()
        oc_run.side_effect = [pods, apply]
        self.assertTrue(ensure_maas_gateway_https_service_clusterip())
        stdin = oc_run.call_args_list[1].kwargs.get("stdin_text") or ""
        self.assertIn("kind: Service", stdin)
        self.assertIn("type: ClusterIP", stdin)
        self.assertIn("port: 443", stdin)

    @patch("install.gateway_config.cluster_source_is_ephc", return_value=False)
    @patch(
        "install.rosa_hcp_pull_setup.is_hypershift_managed_cluster",
        return_value=False,
    )
    def test_skips_non_ephc(self, *_mocks: object) -> None:
        self.assertFalse(ensure_maas_gateway_https_service_clusterip())


if __name__ == "__main__":
    unittest.main()
