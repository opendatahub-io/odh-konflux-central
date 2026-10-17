#!/usr/bin/env python3
"""DSC chart validation network skip during component pytest."""

from __future__ import annotations

import io
import unittest
from contextlib import redirect_stdout
from unittest.mock import patch

from install.dsc_install_policy import _maybe_validate_managed_keys_against_chart
from suite.errors import AppError


class DscChartValidateNetworkSkipTest(unittest.TestCase):
    @patch("install.dsc_install_policy._active_policy")
    @patch("install.rhoai_dsc_chart_validate.chart_validation_enabled", return_value=True)
    @patch("install.rhoai_dsc_chart_validate.validate_smoke_managed_keys_for_operator_version")
    def test_skips_on_http_fetch_app_error(
        self,
        validate_mock,
        _enabled,
        policy_mock,
    ) -> None:
        policy_mock.return_value.promotion_gate_components_override = None
        validate_mock.side_effect = AppError("HTTP GET https://example failed: timed out", 2)
        buf = io.StringIO()
        with redirect_stdout(buf):
            _maybe_validate_managed_keys_against_chart({"trainer"}, "3.6.0-ea.2")
        self.assertIn("DSC chart validation skipped (fetch)", buf.getvalue())
        validate_mock.assert_called_once()

    @patch("install.dsc_install_policy._active_policy")
    @patch("install.rhoai_dsc_chart_validate.chart_validation_enabled", return_value=True)
    @patch("install.rhoai_dsc_chart_validate.validate_smoke_managed_keys_for_operator_version")
    def test_reraises_non_network_app_error(self, validate_mock, _enabled, policy_mock) -> None:
        policy_mock.return_value.promotion_gate_components_override = None
        validate_mock.side_effect = AppError("DSC key foo not in sample", 2)
        with self.assertRaises(AppError):
            _maybe_validate_managed_keys_against_chart({"trainer"}, "3.6.0-ea.2")

    @patch("install.dsc_install_policy._install_removed_for_version")
    @patch("install.dsc_install_policy._active_policy")
    @patch("install.rhoai_dsc_chart_validate.chart_validation_enabled", return_value=True)
    @patch("install.rhoai_dsc_chart_validate.validate_smoke_managed_keys_for_operator_version")
    def test_omits_install_deferred_keys_from_validation(
        self,
        validate_mock,
        _enabled,
        policy_mock,
        deferred_mock,
    ) -> None:
        policy_mock.return_value.promotion_gate_components_override = None
        deferred_mock.return_value = frozenset({"codeflare", "sparkoperator"})
        validate_mock.return_value = []
        _maybe_validate_managed_keys_against_chart(
            {"codeflare", "sparkoperator", "ray"},
            "3.6.0-ea.2",
        )
        validate_mock.assert_called_once()
        self.assertEqual(validate_mock.call_args[0][0], {"ray"})


if __name__ == "__main__":
    raise SystemExit(unittest.main())
