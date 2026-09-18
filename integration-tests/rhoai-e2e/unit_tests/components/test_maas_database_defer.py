"""Unit tests for MaaS database setup deferral before install-rhoai."""

from __future__ import annotations

import os
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from components.maas_billing.database import (
    _promote_maas_db_secret_to_apps_namespace,
    _repair_apps_maas_db_connection_url_if_needed,
    _rewrite_db_connection_url_for_apps_namespace,
    ensure_maas_database,
)


class MaasDatabaseDeferTest(unittest.TestCase):
    @patch("components.maas_billing.database._apps_namespace_ready_for_secrets", return_value=False)
    @patch("components.maas_billing.database._secret_exists", return_value=False)
    @patch.dict(os.environ, {"PRODUCT": "rhoai"}, clear=False)
    def test_defers_on_product_install_when_apps_namespace_missing(
        self,
        _secret_exists,
        _apps_ready,
    ) -> None:
        ensure_maas_database()
        _secret_exists.assert_not_called()

    @patch("components.maas_billing.database._apps_namespace_ready_for_secrets", return_value=False)
    @patch("components.maas_billing.database._secret_exists", return_value=False)
    @patch.dict(os.environ, {"PRODUCT": ""}, clear=False)
    def test_raises_on_existing_when_apps_namespace_missing(
        self,
        _secret_exists,
        _apps_ready,
    ) -> None:
        with self.assertRaisesRegex(RuntimeError, "redhat-ods-applications"):
            ensure_maas_database()

    @patch("components.maas_billing.database._wait_namespace_deleted")
    @patch(
        "components.maas_billing.database._namespace_phase",
        side_effect=["Terminating", None],
    )
    @patch.dict(os.environ, {"PRODUCT": "rhoai", "MAAS_APPS_NS_DELETE_TIMEOUT_SEC": "60"}, clear=False)
    def test_defers_after_waiting_out_terminating_apps_namespace(
        self,
        _phase,
        wait_deleted,
    ) -> None:
        from components.maas_billing.database import _apps_namespace_ready_for_secrets

        self.assertFalse(_apps_namespace_ready_for_secrets())
        wait_deleted.assert_called_once()
        with patch(
            "components.maas_billing.database._apps_namespace_ready_for_secrets",
            return_value=False,
        ):
            ensure_maas_database()


class MaasDatabasePromoteTest(unittest.TestCase):
    def test_rewrite_short_postgres_host_to_infra_fqdn(self) -> None:
        url = _rewrite_db_connection_url_for_apps_namespace(
            "postgresql://maas:secret@postgres:5432/maas",
            infra_ns="odh-ai-gateway-infra",
        )
        self.assertIn("postgres.odh-ai-gateway-infra.svc.cluster.local:5432", url)

    def test_rewrite_operator_postgres_host_to_infra_fqdn(self) -> None:
        url = _rewrite_db_connection_url_for_apps_namespace(
            "postgresql://maas:secret@maas-postgres:5432/maas",
            infra_ns="redhat-ai-gateway-infra",
            postgres_service="maas-postgres",
        )
        self.assertIn(
            "maas-postgres.redhat-ai-gateway-infra.svc.cluster.local:5432",
            url,
        )

    def test_rewrite_leaves_external_host_unchanged(self) -> None:
        url = "postgresql://maas@db.example.com:5432/maas"
        self.assertEqual(
            _rewrite_db_connection_url_for_apps_namespace(url, infra_ns="odh-ai-gateway-infra"),
            url,
        )

    @patch("components.maas_billing.database._create_maas_db_config_secret")
    @patch("components.maas_billing.database._read_secret_data_key")
    def test_repair_updates_existing_apps_secret(
        self,
        read_url,
        create_secret,
    ) -> None:
        read_url.return_value = "postgresql://maas@postgres:5432/maas"
        self.assertTrue(_repair_apps_maas_db_connection_url_if_needed())
        create_secret.assert_called_once()
        args = create_secret.call_args[0]
        self.assertEqual(args[0], "redhat-ods-applications")
        self.assertIn("postgres.odh-ai-gateway-infra.svc.cluster.local", args[1])

    @patch("components.maas_billing.database._create_maas_db_config_secret")
    @patch("components.maas_billing.database._read_secret_data_key")
    def test_repair_skips_when_fqdn_already_set(
        self,
        read_url,
        create_secret,
    ) -> None:
        read_url.return_value = (
            "postgresql://maas@postgres.odh-ai-gateway-infra.svc.cluster.local:5432/maas"
        )
        self.assertFalse(_repair_apps_maas_db_connection_url_if_needed())
        create_secret.assert_not_called()

    @patch("components.maas_billing.database._create_maas_db_config_secret")
    @patch(
        "components.maas_billing.database._read_secret_data_key",
        return_value="postgresql://maas@postgres:5432/maas",
    )
    @patch("components.maas_billing.database._secret_exists")
    @patch("components.maas_billing.database._namespace_exists", return_value=True)
    @patch(
        "components.maas_billing.database._maas_postgres_location",
        return_value=("odh-ai-gateway-infra", "postgres"),
    )
    def test_promote_copies_secret_from_infra_namespace(
        self,
        _postgres_location,
        _ns_exists,
        secret_exists,
        _read_url,
        create_secret,
    ) -> None:
        def exists(ns: str, name: str) -> bool:
            if ns == "redhat-ods-applications" and name == "maas-db-config":
                return create_secret.called
            if ns == "odh-ai-gateway-infra" and name == "maas-db-config":
                return True
            return False

        secret_exists.side_effect = exists
        self.assertTrue(_promote_maas_db_secret_to_apps_namespace())
        create_secret.assert_called_once_with(
            "redhat-ods-applications",
            "postgresql://maas@postgres.odh-ai-gateway-infra.svc.cluster.local:5432/maas",
        )

    @patch("components.maas_billing.database._restart_maas_api_after_db_config")
    @patch("components.maas_billing.database._repair_apps_maas_db_connection_url_if_needed", return_value=False)
    @patch("components.maas_billing.database._secret_exists", return_value=True)
    @patch("components.maas_billing.database._apps_namespace_ready_for_secrets", return_value=True)
    def test_existing_secret_skips_restart_when_unchanged(
        self,
        _apps_ready,
        _secret_exists,
        _repair,
        restart_api,
    ) -> None:
        ensure_maas_database()
        _repair.assert_called_once()
        restart_api.assert_not_called()

    @patch("components.maas_billing.database._restart_maas_api_after_db_config")
    @patch("components.maas_billing.database._repair_apps_maas_db_connection_url_if_needed", return_value=True)
    @patch("components.maas_billing.database._secret_exists", return_value=True)
    @patch("components.maas_billing.database._apps_namespace_ready_for_secrets", return_value=True)
    def test_existing_secret_restarts_maas_api_after_repair(
        self,
        _apps_ready,
        _secret_exists,
        _repair,
        restart_api,
    ) -> None:
        ensure_maas_database()
        _repair.assert_called_once()
        restart_api.assert_called_once()

    @patch("components.maas_billing.database._operator_maas_postgres_active", return_value=False)
    @patch("components.maas_billing.database._clone_models_as_a_service")
    @patch("components.maas_billing.database.subprocess.run")
    @patch("components.maas_billing.database._promote_maas_db_secret_to_apps_namespace")
    @patch("components.maas_billing.database._restart_maas_api_after_db_config")
    @patch("components.maas_billing.database._secret_exists")
    @patch("components.maas_billing.database._apps_namespace_ready_for_secrets", return_value=True)
    def test_setup_database_promotes_when_secret_only_in_infra(
        self,
        _apps_ready,
        secret_exists,
        _restart_api,
        promote,
        subprocess_run,
        clone_repo,
        _operator_active,
    ) -> None:
        secret_exists.return_value = False
        promote.return_value = True
        repo = Path("/tmp/fake-maas-repo")
        clone_repo.return_value = repo
        subprocess_run.return_value = MagicMock(returncode=0)

        with patch.object(Path, "is_file", return_value=True):
            ensure_maas_database()

        promote.assert_called_once()
        subprocess_run.assert_called_once()
        _restart_api.assert_called_once()


if __name__ == "__main__":
    unittest.main()
