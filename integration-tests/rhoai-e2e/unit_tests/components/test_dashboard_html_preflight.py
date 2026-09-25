"""Unit tests for dashboard gateway HTML preflight (cy.visit text/html gate)."""

from __future__ import annotations

import unittest
from unittest.mock import MagicMock, patch

from components.dashboard_cypress.runtime import (
    _content_type_is_html,
    verify_dashboard_serves_html,
)


class ContentTypeHtmlTest(unittest.TestCase):
    def test_html_variants(self) -> None:
        self.assertTrue(_content_type_is_html("text/html"))
        self.assertTrue(_content_type_is_html("text/html; charset=utf-8"))
        self.assertTrue(_content_type_is_html("application/xhtml+xml"))

    def test_rejects_plain_and_empty(self) -> None:
        self.assertFalse(_content_type_is_html("text/plain"))
        self.assertFalse(_content_type_is_html(""))
        self.assertFalse(_content_type_is_html("application/json"))


class VerifyDashboardServesHtmlTest(unittest.TestCase):
    @patch("components.dashboard_cypress.runtime.time.sleep")
    @patch("components.dashboard_cypress.runtime._curl_response_content_type")
    def test_passes_when_html(self, mock_curl: MagicMock, _sleep: MagicMock) -> None:
        mock_curl.return_value = ("200", "text/html; charset=utf-8")
        self.assertTrue(verify_dashboard_serves_html("https://rh-ai.example/", timeout_sec=30))

    @patch("components.dashboard_cypress.runtime.verify_dashboard_serves_html_in_cluster")
    @patch("components.dashboard_cypress.runtime._is_ephc_cluster_source", return_value=True)
    @patch("components.dashboard_cypress.runtime.time.sleep")
    @patch("components.dashboard_cypress.runtime._curl_response_content_type")
    def test_ephc_fallback_on_dns_failure(
        self, mock_curl: MagicMock, _sleep: MagicMock, _ephc: MagicMock, mock_fallback: MagicMock
    ) -> None:
        # First attempt: DNS failure (code 000)
        # Second attempt: Success (mock_fallback returns True)
        mock_curl.return_value = ("000", "")
        mock_fallback.return_value = True
        self.assertTrue(verify_dashboard_serves_html("https://rh-ai.example/", timeout_sec=30))
        mock_fallback.assert_called_once()

    @patch("components.dashboard_cypress.runtime.verify_dashboard_serves_html_in_cluster")
    @patch("components.dashboard_cypress.runtime._is_ephc_cluster_source", return_value=True)
    @patch("components.dashboard_cypress.runtime.time.sleep")
    @patch("components.dashboard_cypress.runtime._curl_response_content_type")
    def test_ephc_fallback_on_dns_failure_with_error(
        self, mock_curl: MagicMock, _sleep: MagicMock, _ephc: MagicMock, mock_fallback: MagicMock
    ) -> None:
        # Check that 000 with curl error message also triggers fallback
        mock_curl.return_value = ("000 curl: (6) Could not resolve host", "")
        mock_fallback.return_value = True
        self.assertTrue(verify_dashboard_serves_html("https://rh-ai.example/", timeout_sec=30))
        mock_fallback.assert_called_once()

    @patch("components.dashboard_cypress.runtime.time.sleep")
    @patch("components.dashboard_cypress.runtime.time.time", side_effect=[0, 0, 200])
    @patch("components.dashboard_cypress.runtime._curl_response_content_type")
    def test_fails_on_text_plain(
        self, mock_curl: MagicMock, _time: MagicMock, _sleep: MagicMock
    ) -> None:
        mock_curl.return_value = ("200", "text/plain")
        self.assertFalse(verify_dashboard_serves_html("https://rh-ai.example/", timeout_sec=10))


if __name__ == "__main__":
    raise SystemExit(unittest.main())
