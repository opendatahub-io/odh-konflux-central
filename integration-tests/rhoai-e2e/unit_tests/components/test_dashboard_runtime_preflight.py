"""Unit tests for dashboard HTTP/DNS preflight helpers."""

from __future__ import annotations

import os
import unittest
from unittest.mock import patch

from components.dashboard_cypress.runtime import (
    curl_preflight_is_dns_failure,
    dashboard_reachable_probe,
    verify_dashboard_reachable_in_cluster,
)


class DashboardRuntimePreflightTest(unittest.TestCase):
    def test_curl_preflight_is_dns_failure(self) -> None:
        self.assertTrue(
            curl_preflight_is_dns_failure("", "curl: (6) Could not resolve host: rh-ai.example.com")
        )
        self.assertTrue(
            curl_preflight_is_dns_failure(
                "000",
                "curl: (6) Could not resolve host: rh-ai.example.com",
            )
        )
        self.assertFalse(curl_preflight_is_dns_failure("503", "curl: (22) The requested URL returned error: 503"))

    @patch("components.dashboard_cypress.runtime.dashboard_curl_preflight", return_value=("200", ""))
    def test_dashboard_reachable_probe_ok(self, *_mocks: object) -> None:
        ok, kind = dashboard_reachable_probe("https://rh-ai.example.com")
        self.assertTrue(ok)
        self.assertEqual(kind, "")

    @patch("components.dashboard_cypress.runtime.verify_dashboard_reachable_in_cluster", return_value=True)
    @patch(
        "components.dashboard_cypress.runtime.dashboard_curl_preflight",
        return_value=("", "curl: (6) Could not resolve host: rh-ai.example.com"),
    )
    @patch.dict(os.environ, {"PRODUCT": "rhoai"}, clear=False)
    def test_dashboard_reachable_probe_ephc_without_cluster_source_env(
        self, in_cluster_mock: object, *_mocks: object
    ) -> None:
        ok, kind = dashboard_reachable_probe("https://rh-ai.example.com")
        self.assertTrue(ok)
        self.assertEqual(kind, "")
        in_cluster_mock.assert_called_once_with("https://rh-ai.example.com")

    @patch("components.dashboard_cypress.runtime.verify_dashboard_reachable_in_cluster", return_value=True)
    @patch(
        "components.dashboard_cypress.runtime.dashboard_curl_preflight",
        return_value=("", "curl: (6) Could not resolve host: rh-ai.example.com"),
    )
    @patch.dict(os.environ, {"CLUSTER_SOURCE": "EPHC"}, clear=False)
    def test_dashboard_reachable_probe_ephc_in_cluster_fallback(
        self, in_cluster_mock: object, *_mocks: object
    ) -> None:
        ok, kind = dashboard_reachable_probe("https://rh-ai.example.com")
        self.assertTrue(ok)
        self.assertEqual(kind, "")
        in_cluster_mock.assert_called_once_with("https://rh-ai.example.com")

    @patch(
        "components.dashboard_cypress.runtime.dashboard_curl_preflight",
        return_value=("", "curl: (6) Could not resolve host: rh-ai.example.com"),
    )
    @patch.dict(os.environ, {"CLUSTER_SOURCE": "my-external-secret"}, clear=False)
    def test_dashboard_reachable_probe_external_dns_failure(self, *_mocks: object) -> None:
        ok, kind = dashboard_reachable_probe("https://rh-ai.example.com")
        self.assertFalse(ok)
        self.assertEqual(kind, "dns")

    @patch("components.dashboard_cypress.runtime.oc_run")
    def test_verify_dashboard_reachable_in_cluster_reads_pod_logs(self, oc_run_mock: object) -> None:
        def _side_effect(cmd: list[str], **_kwargs: object) -> object:
            if cmd and cmd[0] == "run":
                return type("R", (), {"returncode": 0, "stdout": "", "stderr": ""})()
            if cmd and cmd[0] == "wait":
                return type("R", (), {"returncode": 0, "stdout": "", "stderr": ""})()
            if cmd and cmd[0] == "logs":
                return type("R", (), {"returncode": 0, "stdout": "302", "stderr": ""})()
            return type("R", (), {"returncode": 0, "stdout": "", "stderr": ""})()

        oc_run_mock.side_effect = _side_effect
        self.assertTrue(verify_dashboard_reachable_in_cluster("https://rh-ai.example.com"))
