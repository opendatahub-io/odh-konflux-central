"""Unit tests for MaaS database infra cleanup on external pooled clusters."""

from __future__ import annotations

import unittest
from unittest.mock import patch

from components.maas_billing.database import (
    _maas_postgres_location,
    _needs_maas_postgres_reset,
    cleanup_maas_database_infra,
    ensure_maas_database,
)


class MaasDatabaseCleanupTest(unittest.TestCase):
    @patch("components.maas_billing.database._reset_maas_postgres_database")
    @patch("components.maas_billing.database._delete_namespace_if_present")
    @patch("components.maas_billing.database._delete_maas_db_secrets")
    @patch(
        "components.maas_billing.database._maas_postgres_location",
        return_value=("redhat-ai-gateway-infra", "maas-postgres"),
    )
    def test_cleanup_resets_operator_postgres_instead_of_deleting_ns(
        self,
        _location,
        delete_secrets,
        delete_ns,
        reset_db,
    ) -> None:
        from components.maas_billing.database import cleanup_maas_postgres_infra

        cleanup_maas_postgres_infra()
        delete_secrets.assert_called_once_with()
        reset_db.assert_called_once_with("redhat-ai-gateway-infra", "maas-postgres")
        delete_ns.assert_not_called()

    @patch("components.maas_billing.database._read_operator_maas_postgres_credentials", return_value=("maas", "secret", "maas"))
    @patch("components.maas_billing.database._postgres_deploy_ready", return_value=True)
    @patch("components.maas_billing.database.oc_run")
    def test_reset_terminates_backends_before_drop(self, oc_run, _ready, _creds) -> None:
        from components.maas_billing.database import _reset_maas_postgres_database

        oc_run.return_value.returncode = 0
        _reset_maas_postgres_database("redhat-ai-gateway-infra", "maas-postgres")
        executed_sql = [
            call.args[0][-1]
            for call in oc_run.call_args_list
            if call.args and call.args[0][0] == "exec"
        ]
        self.assertIn("pg_terminate_backend", executed_sql[0])
        self.assertIn("DROP DATABASE IF EXISTS maas;", executed_sql[1])
        self.assertIn("CREATE DATABASE maas OWNER maas;", executed_sql[2])

    @patch("components.maas_billing.database._delete_namespace_if_present")
    @patch("components.maas_billing.database._delete_maas_db_secrets")
    @patch(
        "components.maas_billing.database._maas_postgres_location",
        return_value=("odh-ai-gateway-infra", "postgres"),
    )
    def test_cleanup_removes_secrets_and_namespaces(
        self,
        _location,
        delete_secrets,
        delete_ns,
    ) -> None:
        cleanup_maas_database_infra()
        delete_secrets.assert_called_once_with()
        self.assertEqual(delete_ns.call_count, 2)

    @patch(
        "components.maas_billing.database._deployment_exists",
        side_effect=lambda ns, name: ns == "redhat-ai-gateway-infra"
        and name == "maas-postgres",
    )
    def test_maas_postgres_location_prefers_operator_postgres(self, _exists) -> None:
        self.assertEqual(
            _maas_postgres_location(),
            ("redhat-ai-gateway-infra", "maas-postgres"),
        )

    @patch("components.maas_billing.database.maas_api_deployment_exists", return_value=False)
    @patch("components.maas_billing.database._maas_api_deployment_ready", return_value=False)
    @patch("components.maas_billing.database._read_maas_postgres_schema_version", return_value=None)
    @patch("components.maas_billing.database._maas_postgres_has_missing_schema", return_value=True)
    def test_skips_reset_when_schema_missing_before_maas_api_exists(
        self,
        _missing_schema,
        _schema,
        _api_ready,
        _api_exists,
    ) -> None:
        self.assertFalse(_needs_maas_postgres_reset())

    @patch("components.maas_billing.database.maas_api_deployment_exists", return_value=True)
    @patch("components.maas_billing.database._maas_api_deployment_ready", return_value=False)
    @patch("components.maas_billing.database._read_maas_postgres_schema_version", return_value=None)
    @patch("components.maas_billing.database._maas_postgres_has_missing_schema", return_value=True)
    def test_skips_reset_when_schema_table_missing_while_maas_api_starting(
        self,
        _missing_schema,
        _schema,
        _api_ready,
        _api_exists,
    ) -> None:
        self.assertFalse(_needs_maas_postgres_reset())

    @patch("components.maas_billing.database._maas_api_deployment_ready", return_value=False)
    @patch("components.maas_billing.database._read_maas_postgres_schema_version", return_value=6)
    def test_needs_reset_when_operator_postgres_has_stale_schema(
        self,
        _schema,
        _api_ready,
    ) -> None:
        self.assertTrue(_needs_maas_postgres_reset())

    @patch("components.maas_billing.database._maas_api_deployment_ready", return_value=False)
    @patch("components.maas_billing.database._read_maas_postgres_schema_version", return_value=5)
    def test_needs_reset_when_schema_present_and_api_not_ready(
        self,
        _schema,
        _api_ready,
    ) -> None:
        self.assertTrue(_needs_maas_postgres_reset())

    @patch("components.maas_billing.database._maas_api_deployment_ready", return_value=True)
    @patch("components.maas_billing.database._read_maas_postgres_schema_version", return_value=5)
    def test_skips_reset_when_api_ready(
        self,
        _schema,
        _api_ready,
    ) -> None:
        self.assertFalse(_needs_maas_postgres_reset())

    @patch("components.maas_billing.database._operator_maas_postgres_active", return_value=False)
    @patch("components.maas_billing.database._restart_maas_api_after_db_config")
    @patch("components.maas_billing.database.cleanup_maas_database_infra")
    @patch("components.maas_billing.database._needs_maas_postgres_reset", return_value=True)
    @patch("components.maas_billing.database._repair_apps_maas_db_connection_url_if_needed", return_value=False)
    @patch("components.maas_billing.database._secret_exists", return_value=True)
    @patch("components.maas_billing.database._namespace_exists", return_value=True)
    @patch("components.maas_billing.database._apps_namespace_ready_for_secrets", return_value=True)
    @patch("components.maas_billing.database._clone_models_as_a_service")
    @patch("components.maas_billing.database.subprocess.run")
    def test_ensure_resets_stale_schema_and_reruns_setup(
        self,
        subprocess_run,
        clone_repo,
        _apps_ns_ready,
        _ns_exists,
        secret_exists,
        _repair,
        needs_reset,
        cleanup,
        restart_api,
        _operator_active,
    ) -> None:
        from pathlib import Path

        repo = Path("/tmp/fake-models-as-a-service")
        clone_repo.return_value = repo
        subprocess_run.return_value.returncode = 0
        secret_exists.side_effect = [True, False, True]

        with patch.object(Path, "is_file", return_value=True):
            with patch(
                "components.maas_billing.database._promote_maas_db_secret_to_apps_namespace",
                return_value=True,
            ):
                ensure_maas_database()

        cleanup.assert_called_once_with()
        needs_reset.assert_called_once_with()
        restart_api.assert_called_once()


class MaasOperatorPostgresDeferTest(unittest.TestCase):
    @patch("components.maas_billing.database._restart_maas_api_after_db_config")
    @patch("components.maas_billing.database._defer_maas_db_config_until_operator_install")
    @patch("components.maas_billing.database._ensure_operator_maas_db_config_secrets", return_value=False)
    @patch("components.maas_billing.database._operator_maas_postgres_active", return_value=True)
    @patch("components.maas_billing.database._secret_exists", return_value=False)
    @patch("components.maas_billing.database._apps_namespace_ready_for_secrets", return_value=True)
    @patch("components.maas_billing.database._clone_models_as_a_service")
    def test_skips_setup_database_when_operator_postgres_active(
        self,
        clone_repo,
        _apps_ready,
        _secret_exists,
        operator_active,
        ensure_operator,
        defer,
        restart_api,
    ) -> None:
        ensure_maas_database()
        clone_repo.assert_not_called()
        ensure_operator.assert_called_once_with()
        defer.assert_called_once_with()
        restart_api.assert_not_called()

    @patch("components.maas_billing.database._restart_maas_api_after_db_config")
    @patch("components.maas_billing.database._ensure_operator_maas_db_config_secrets", return_value=True)
    @patch("components.maas_billing.database._operator_maas_postgres_active", return_value=True)
    @patch("components.maas_billing.database._secret_exists", return_value=False)
    @patch("components.maas_billing.database._apps_namespace_ready_for_secrets", return_value=True)
    @patch("components.maas_billing.database._clone_models_as_a_service")
    def test_creates_operator_db_config_when_promote_unavailable(
        self,
        clone_repo,
        _apps_ready,
        _secret_exists,
        operator_active,
        ensure_operator,
        restart_api,
    ) -> None:
        ensure_maas_database()
        clone_repo.assert_not_called()
        ensure_operator.assert_called_once_with()
        restart_api.assert_called_once()


if __name__ == "__main__":
    unittest.main()
