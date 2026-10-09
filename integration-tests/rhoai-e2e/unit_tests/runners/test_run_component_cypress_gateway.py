"""Unit tests for dashboard Cypress gateway preflight fail-fast."""

from __future__ import annotations

import os
import unittest
from unittest.mock import patch

from runners.run_component_cypress import (
    _fail_or_warn_gateway,
    _gateway_checks_fail_fast,
    _gateway_preflight_issues,
)


class GatewayFailFastHelpersTest(unittest.TestCase):
    def test_fail_fast_defaults_to_strict(self) -> None:
        with patch.dict(os.environ, {}, clear=True):
            self.assertTrue(_gateway_checks_fail_fast())

    @patch.dict(os.environ, {"RHCL_GATEWAY_FAIL_FAST": "0"})
    def test_fail_fast_disabled(self) -> None:
        self.assertFalse(_gateway_checks_fail_fast())

    @patch.dict(os.environ, {"RHCL_GATEWAY_FAIL_FAST": "1"})
    def test_fail_or_warn_gateway_strict(self) -> None:
        self.assertEqual(_fail_or_warn_gateway("gateway unhealthy"), 2)

    @patch.dict(os.environ, {"RHCL_GATEWAY_FAIL_FAST": "0"})
    def test_fail_or_warn_gateway_warn_only(self) -> None:
        self.assertIsNone(_fail_or_warn_gateway("gateway unhealthy"))

    def test_stale_incomplete_marker_alone_not_a_blocker(self) -> None:
        """Incomplete marker with healthy deployments must not skip Cypress (44358aac regression)."""
        self.assertEqual(
            _gateway_preflight_issues(auth_ready=True, incomplete=True, healthy=True),
            [],
        )

    def test_incomplete_and_unhealthy_blocks(self) -> None:
        issues = _gateway_preflight_issues(auth_ready=True, incomplete=True, healthy=False)
        self.assertIn("gateway deployments not fully ready", issues)
        self.assertIn("Kuadrant/Authorino stack incomplete", issues[1])


if __name__ == "__main__":
    raise SystemExit(unittest.main())
