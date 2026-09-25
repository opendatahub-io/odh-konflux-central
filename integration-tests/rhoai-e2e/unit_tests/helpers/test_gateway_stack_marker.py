"""Unit tests for helpers.gateway_stack_marker."""

from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from helpers import gateway_stack_marker as marker


class GatewayStackMarkerTest(unittest.TestCase):
    def test_write_and_read_under_tests_shared(self) -> None:
        with mock.patch.dict(os.environ, {"TESTS_SHARED": "/workspace/tests-shared"}, clear=False):
            with (
                mock.patch.object(
                    marker,
                    "gateway_stack_marker_paths",
                    return_value=[Path("/workspace/tests-shared/tests-payload/results/.gateway-auth-stack-incomplete")],
                ),
                mock.patch.object(Path, "mkdir"),
                mock.patch.object(Path, "write_text") as write_text,
                mock.patch.object(Path, "is_file", return_value=True),
            ):
                marker.write_gateway_stack_incomplete_marker()
                self.assertTrue(marker.gateway_stack_incomplete())
                write_text.assert_called_once()

    def test_reconcile_clears_marker_when_live_ready(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / ".gateway-auth-stack-incomplete"
            path.write_text("rhcl post-install retry failed\n", encoding="utf-8")
            with (
                mock.patch.object(
                    marker, "gateway_stack_marker_paths", return_value=[path]
                ),
                mock.patch(
                    "components.maas_billing.auth.maas_gateway_auth_stack_live_ready",
                    return_value=True,
                ),
            ):
                self.assertTrue(marker.reconcile_gateway_stack_incomplete_marker())
            self.assertFalse(path.is_file())

    def test_reconcile_keeps_marker_when_live_not_ready(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / ".gateway-auth-stack-incomplete"
            path.write_text("rhcl post-install retry failed\n", encoding="utf-8")
            with (
                mock.patch.object(
                    marker, "gateway_stack_marker_paths", return_value=[path]
                ),
                mock.patch(
                    "components.maas_billing.auth.maas_gateway_auth_stack_live_ready",
                    return_value=False,
                ),
                mock.patch(
                    "components.maas_billing.auth.recover_kuadrant_after_gateway_api_provider",
                    return_value=False,
                ),
            ):
                self.assertFalse(marker.reconcile_gateway_stack_incomplete_marker())
            self.assertTrue(path.is_file())

    def test_reconcile_recovers_kuadrant_then_clears_marker(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / ".gateway-auth-stack-incomplete"
            path.write_text("rhcl post-install retry failed\n", encoding="utf-8")
            with (
                mock.patch.object(
                    marker, "gateway_stack_marker_paths", return_value=[path]
                ),
                mock.patch(
                    "components.maas_billing.auth.maas_gateway_auth_stack_live_ready",
                    side_effect=[False, True],
                ),
                mock.patch(
                    "components.maas_billing.auth.recover_kuadrant_after_gateway_api_provider",
                    return_value=True,
                ) as recover,
            ):
                self.assertTrue(marker.reconcile_gateway_stack_incomplete_marker())
                recover.assert_called_once()
            self.assertFalse(path.is_file())


if __name__ == "__main__":
    unittest.main()
