"""Tests for verify-operator-ready Tekton entry."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest import mock

from runners import verify_operator_ready

class VerifyOperatorReadyTest(unittest.TestCase):
    def test_skips_when_marker_present(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tests_shared = Path(tmp)
            (tests_shared / ".skip-verify-operator-ready").write_text("", encoding="utf-8")
            with mock.patch.dict(
                "os.environ",
                {"KUBECONFIG": "/tmp/kc", "TESTS_SHARED": str(tests_shared)},
                clear=False,
            ):
                self.assertEqual(verify_operator_ready.main(), 0)

    @mock.patch("runners.verify_operator_ready.log_gateway_auth_stack_warnings")
    @mock.patch("runners.verify_operator_ready.verify_dashboard_route_for_prepare", return_value="https://dash.example")
    def test_runs_when_product_existing(self, verify_mock: mock.MagicMock, _auth_warn: mock.MagicMock) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch.dict(
                "os.environ",
                {"KUBECONFIG": "/tmp/kc", "PRODUCT": "", "TESTS_SHARED": tmp},
                clear=False,
            ):
                self.assertEqual(verify_operator_ready.main(), 0)
        verify_mock.assert_called_once()
        _auth_warn.assert_called_once()

    @mock.patch("runners.verify_operator_ready.log_gateway_auth_stack_warnings")
    @mock.patch("runners.verify_operator_ready.verify_dashboard_route_for_prepare", return_value="https://dash.example")
    def test_writes_dashboard_url_file(self, verify_mock: mock.MagicMock, _auth_warn: mock.MagicMock) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tests_shared = Path(tmp)
            with mock.patch.dict(
                "os.environ",
                {
                    "KUBECONFIG": str(tests_shared / "credentials" / "kubeconfig"),
                    "PRODUCT": "rhoai",
                    "TESTS_SHARED": str(tests_shared),
                },
                clear=False,
            ):
                (tests_shared / "credentials").mkdir(parents=True)
                (tests_shared / "credentials" / "kubeconfig").write_text("x", encoding="utf-8")
                self.assertEqual(verify_operator_ready.main(), 0)
            url_file = tests_shared / "tests-payload" / "odh-dashboard-url.txt"
            self.assertTrue(url_file.is_file())
            self.assertEqual(url_file.read_text(encoding="utf-8").strip(), "https://dash.example")
            verify_mock.assert_called_once()

    def test_skips_dashboard_verify_for_install_dependencies_model_server(self) -> None:
        with mock.patch.dict(
            "os.environ",
            {
                "KUBECONFIG": "/tmp/kc",
                "PRODUCT": "",
                "COMPONENTS_CSV": "model_server",
                "RUN_COMPONENT_CLUSTER_PREP_IN_DEP_OPERATORS": "true",
            },
            clear=False,
        ):
            with mock.patch(
                "runners.verify_operator_ready.verify_dashboard_route_for_prepare",
            ) as verify_mock:
                self.assertEqual(verify_operator_ready.main(), 0)
                verify_mock.assert_not_called()

    @mock.patch("runners.verify_operator_ready._dsc_crd_available", return_value=False)
    @mock.patch("runners.verify_operator_ready.verify_dashboard_route_for_prepare", return_value="https://dash.example")
    def test_skips_when_no_dsc_crd_and_not_dashboard_cypress(
        self,
        verify_mock: mock.MagicMock,
        _dsc: mock.MagicMock,
    ) -> None:
        with mock.patch.dict(
            "os.environ",
            {
                "KUBECONFIG": "/tmp/kc",
                "PRODUCT": "",
                "COMPONENTS_CSV": "model_server",
            },
            clear=False,
        ):
            self.assertEqual(verify_operator_ready.main(), 0)
        verify_mock.assert_not_called()

