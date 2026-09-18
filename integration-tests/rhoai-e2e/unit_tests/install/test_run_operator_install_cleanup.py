"""Unit tests for external cleanup best-effort MaaS teardown."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from install.run_operator_install_cleanup import run_cleanup_operator
from suite.errors import AppError


class RunOlminstallCleanupTest(unittest.TestCase):
    @patch("install.run_operator_install_cleanup._invoke_cleanup")
    @patch("install.leaked_tenant_namespace_cleanup.cleanup_leaked_tenant_namespaces")
    @patch("components.maas_billing.database.cleanup_maas_tenant_namespace")
    @patch("components.maas_billing.database.cleanup_maas_postgres_infra")
    @patch("components.maas_billing.bbr_pre_processing.cleanup_stale_maas_ingress_workloads")
    def test_maas_failure_still_runs_operator_cleanup(
        self,
        cleanup_ingress,
        cleanup_postgres,
        cleanup_tenant,
        cleanup_leaked,
        invoke_cleanup,
    ) -> None:
        cleanup_postgres.side_effect = RuntimeError("namespace stuck")
        with tempfile.TemporaryDirectory() as tmp:
            olm = Path(tmp)
            (olm / "cleanup.sh").write_text("#!/bin/bash\n", encoding="utf-8")
            with self.assertRaises(AppError) as ctx:
                run_cleanup_operator(
                    operator_install_dir=olm,
                    kubeconfig=Path(tmp) / "kubeconfig",
                )
        invoke_cleanup.assert_called_once()
        cleanup_tenant.assert_called_once()
        cleanup_ingress.assert_called_once()
        cleanup_leaked.assert_called_once()
        self.assertIn("MaaS infra cleanup failed", str(ctx.exception))

    @patch("install.run_operator_install_cleanup._invoke_cleanup")
    @patch("install.leaked_tenant_namespace_cleanup.cleanup_leaked_tenant_namespaces")
    @patch("components.maas_billing.database.cleanup_maas_tenant_namespace")
    @patch("components.maas_billing.database.cleanup_maas_postgres_infra")
    @patch("components.maas_billing.bbr_pre_processing.cleanup_stale_maas_ingress_workloads")
    def test_maas_success_does_not_raise(
        self,
        cleanup_ingress,
        cleanup_postgres,
        cleanup_tenant,
        cleanup_leaked,
        invoke_cleanup,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            olm = Path(tmp)
            (olm / "cleanup.sh").write_text("#!/bin/bash\n", encoding="utf-8")
            run_cleanup_operator(
                operator_install_dir=olm,
                kubeconfig=Path(tmp) / "kubeconfig",
            )
        cleanup_postgres.assert_called_once()
        cleanup_tenant.assert_called_once()
        cleanup_ingress.assert_called_once()
        cleanup_leaked.assert_called_once()
        invoke_cleanup.assert_called_once()

    @patch("install.run_operator_install_cleanup._invoke_cleanup")
    @patch("install.leaked_tenant_namespace_cleanup.cleanup_leaked_tenant_namespaces")
    @patch("components.maas_billing.database.cleanup_maas_tenant_namespace")
    @patch("components.maas_billing.database.cleanup_maas_postgres_infra")
    @patch("components.maas_billing.bbr_pre_processing.cleanup_stale_maas_ingress_workloads")
    def test_operator_cleanup_failure_still_runs_tenant_cleanup(
        self,
        cleanup_ingress,
        cleanup_postgres,
        cleanup_tenant,
        cleanup_leaked,
        invoke_cleanup,
    ) -> None:
        invoke_cleanup.side_effect = AppError("rhoai-e2e cleanup.sh failed (exit 1)", 1)
        with tempfile.TemporaryDirectory() as tmp:
            olm = Path(tmp)
            (olm / "cleanup.sh").write_text("#!/bin/bash\n", encoding="utf-8")
            with self.assertRaises(AppError) as ctx:
                run_cleanup_operator(
                    operator_install_dir=olm,
                    kubeconfig=Path(tmp) / "kubeconfig",
                )
        cleanup_postgres.assert_called_once()
        cleanup_tenant.assert_called_once()
        cleanup_leaked.assert_called_once()
        invoke_cleanup.assert_called_once()
        self.assertIn("cleanup.sh failed", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()
