#!/usr/bin/env python3
"""Unit tests for MaaS OIDC Keycloak user provisioning."""

from __future__ import annotations

import os
import unittest
from unittest import mock

from components.maas_billing import oidc_users as mod  # noqa: E402

class TestEnsureMaasOidcKeycloakUsers(unittest.TestCase):
    def test_skips_non_byoidc_cluster(self) -> None:
        with (
            mock.patch.object(mod, "_cluster_is_byoidc", return_value=False),
            mock.patch.object(mod, "_keycloak_admin_token") as admin_token,
        ):
            mod.ensure_maas_oidc_keycloak_users()
        admin_token.assert_not_called()

    def test_skips_when_users_already_authenticate(self) -> None:
        with (
            mock.patch.object(mod, "_cluster_is_byoidc", return_value=True),
            mock.patch.object(mod, "_realm_admin_base", return_value="https://kc/admin/realms/openshift-ai-maas"),
            mock.patch.object(mod, "_byoidc_issuer_url", return_value="https://kc/realms/openshift"),
            mock.patch.object(mod, "_maas_client_secret", return_value="secret"),
            mock.patch.object(mod, "_byoidc_user_passwords", return_value={"odh-user1": "pw1"}),
            mock.patch.object(mod, "_password_grant_ok", return_value=True),
            mock.patch.object(mod, "_secret_literal", return_value=""),
            mock.patch.object(mod, "_persist_maas_client_secret_on_cluster"),
            mock.patch.object(mod, "_keycloak_admin_token") as admin_token,
        ):
            mod.ensure_maas_oidc_keycloak_users()
        admin_token.assert_not_called()

    def test_raises_without_admin_when_users_missing(self) -> None:
        with (
            mock.patch.object(mod, "_cluster_is_byoidc", return_value=True),
            mock.patch.object(mod, "_realm_admin_base", return_value="https://kc/admin/realms/openshift-ai-maas"),
            mock.patch.object(mod, "_byoidc_issuer_url", return_value="https://kc/realms/openshift"),
            mock.patch.object(mod, "_maas_client_secret", return_value="secret"),
            mock.patch.dict(os.environ, {"MAAS_OIDC_USER1": "odh-user1", "MAAS_OIDC_PASSWORD1": "pw1"}, clear=False),
            mock.patch.object(mod, "_password_grant_ok", return_value=False),
            mock.patch.object(mod, "_keycloak_admin_token", return_value=""),
        ):
            with self.assertRaises(RuntimeError):
                mod.ensure_maas_oidc_keycloak_users()

class TestByoidcCypressTestUser(unittest.TestCase):
    def test_prefers_odh_admin1_for_gateway_smoke(self) -> None:
        with mock.patch.object(
            mod,
            "_byoidc_user_passwords",
            return_value={"odh-admin1": "adminpw", "odh-user1": "userpw"},
        ):
            user = mod.byoidc_cypress_test_user()
        self.assertEqual(user, {"AUTH_TYPE": "oidc", "USERNAME": "odh-admin1", "PASSWORD": "adminpw"})

