#!/usr/bin/env python3
"""Tests for MaaS billing htpasswd pytest user overrides on ROSA HCP."""

from __future__ import annotations

import os
import unittest
from unittest import mock

from components.maas_billing.oidc_users import (  # noqa: E402
    apply_maas_billing_htpasswd_test_user_overrides,
    maas_billing_aitenant_crd_installed,
    maas_billing_aitenant_pytest_extra_args,
    maas_billing_rosa_hcp_pytest_extra_args,
    maas_billing_rosa_hcp_skip_htpasswd_oauth_idp,
)

class MaasBillingHtpasswdTest(unittest.TestCase):
    _ENV_KEYS = (
        "TEST_USER_USERNAME",
        "TEST_USER_PASSWORD",
        "TEST_USER_AUTH_TYPE",
        "CLUSTER_AUTH",
        "OCP_ADMIN_USER_USERNAME",
        "OCP_ADMIN_USER_PASSWORD",
        "OLMINSTALL_HTPASSWD_KUBECONFIG",
    )

    def setUp(self) -> None:
        self._saved_env = {k: os.environ.pop(k, None) for k in self._ENV_KEYS}

    def tearDown(self) -> None:
        for key in self._ENV_KEYS:
            os.environ.pop(key, None)
        for key, val in self._saved_env.items():
            if val is not None:
                os.environ[key] = val

    def test_maps_ldap_vault_user_to_htpasswd_admin(self) -> None:
        vault = {
            "TEST_USER_USERNAME": "ldap-user1",
            "TEST_USER_PASSWORD": "ldap-pass",
            "OCP_ADMIN_USER_USERNAME": "htpasswd-cluster-admin-user",
            "OCP_ADMIN_USER_PASSWORD": "admin-pass",
        }
        with (
            mock.patch("components.maas_billing.oidc_users._cluster_is_byoidc", return_value=False),
            mock.patch(
                "components.maas_billing.oidc_users._maas_billing_byoidc_overlay",
                return_value={},
            ),
            mock.patch(
                "components.codeflare_sdk.auth.read_pytest_vault_env",
                return_value=vault,
            ),
            mock.patch("install.ldap.cluster_has_htpasswd_identity", return_value=True),
            mock.patch("components.codeflare_sdk.auth.cluster_has_htpasswd_identity", return_value=True),
            mock.patch.dict(
                os.environ,
                {"CLUSTER_SOURCE": "ods-qe-psi-07"},
                clear=False,
            ),
            mock.patch(
                "components.dashboard_cypress.auth_overlay._htpasswd_test_user_from_env",
                return_value={
                    "AUTH_TYPE": "htpasswd-cluster-admin",
                    "USERNAME": "htpasswd-cluster-admin-user",
                    "PASSWORD": "admin-pass",
                },
            ),
            mock.patch(
                "install.ldap.ensure_htpasswd_openldap_secret_for_unprivileged_tests",
            ) as stage_secret,
            mock.patch(
                "steps.tekton_util.materialize_htpasswd_kubeconfig_login",
                return_value=True,
            ) as login_kc,
        ):
            overlay = apply_maas_billing_htpasswd_test_user_overrides()
            self.assertEqual(os.environ["TEST_USER_USERNAME"], "htpasswd-cluster-admin-user")
            self.assertEqual(os.environ["TEST_USER_PASSWORD"], "admin-pass")
            self.assertEqual(os.environ["TEST_USER_AUTH_TYPE"], "htpasswd-cluster-admin")
            self.assertEqual(os.environ["CLUSTER_AUTH"], "htpasswd-cluster-admin")
            self.assertEqual(os.environ["OCP_ADMIN_USER_USERNAME"], "htpasswd-cluster-admin-user")
            self.assertEqual(overlay.get("OLMINSTALL_HTPASSWD_KUBECONFIG"), "1")
        stage_secret.assert_called_once_with("htpasswd-cluster-admin-user", "admin-pass")
        login_kc.assert_called_once_with("htpasswd-cluster-admin-user", "admin-pass")

    def test_htpasswd_login_failure_exits_before_pytest(self) -> None:
        vault = {
            "TEST_USER_USERNAME": "ldap-user1",
            "TEST_USER_PASSWORD": "ldap-pass",
            "OCP_ADMIN_USER_USERNAME": "htpasswd-cluster-admin-user",
            "OCP_ADMIN_USER_PASSWORD": "admin-pass",
        }
        with (
            mock.patch("components.maas_billing.oidc_users._cluster_is_byoidc", return_value=False),
            mock.patch(
                "components.maas_billing.oidc_users._maas_billing_byoidc_overlay",
                return_value={},
            ),
            mock.patch(
                "components.codeflare_sdk.auth.read_pytest_vault_env",
                return_value=vault,
            ),
            mock.patch("install.ldap.cluster_has_htpasswd_identity", return_value=True),
            mock.patch("components.codeflare_sdk.auth.cluster_has_htpasswd_identity", return_value=True),
            mock.patch.dict(
                os.environ,
                {"CLUSTER_SOURCE": "ods-qe-psi-07"},
                clear=False,
            ),
            mock.patch(
                "components.dashboard_cypress.auth_overlay._htpasswd_test_user_from_env",
                return_value={
                    "AUTH_TYPE": "htpasswd-cluster-admin",
                    "USERNAME": "htpasswd-cluster-admin-user",
                    "PASSWORD": "admin-pass",
                },
            ),
            mock.patch(
                "install.ldap.ensure_htpasswd_openldap_secret_for_unprivileged_tests",
            ),
            mock.patch(
                "steps.tekton_util.materialize_htpasswd_kubeconfig_login",
                return_value=False,
            ),
        ):
            with self.assertRaises(SystemExit) as ctx:
                apply_maas_billing_htpasswd_test_user_overrides()
        self.assertIn("htpasswd kubeconfig login failed", str(ctx.exception))

    def test_skips_on_byoidc_cluster_without_credentials(self) -> None:
        with (
            mock.patch("components.maas_billing.oidc_users._cluster_is_byoidc", return_value=True),
            mock.patch(
                "components.maas_billing.oidc_users._maas_billing_byoidc_overlay",
                return_value={},
            ),
            mock.patch("components.codeflare_sdk.auth.read_pytest_vault_env") as read_vault,
        ):
            apply_maas_billing_htpasswd_test_user_overrides()
        read_vault.assert_not_called()

    def test_ephc_skips_vault_ldap_without_htpasswd_idp(self) -> None:
        """HyperShift often blocks OAuth IdP; vault LDAP cannot log in — use admin SA only."""
        vault = {
            "TEST_USER_USERNAME": "ldap-user1",
            "TEST_USER_PASSWORD": "ldap-pass",
            "OCP_ADMIN_USER_USERNAME": "htpasswd-cluster-admin-user",
            "OCP_ADMIN_USER_PASSWORD": "admin-pass",
        }
        with (
            mock.patch("components.maas_billing.oidc_users._cluster_is_byoidc", return_value=False),
            mock.patch(
                "components.maas_billing.oidc_users._maas_billing_byoidc_overlay",
                return_value={},
            ),
            mock.patch(
                "components.codeflare_sdk.auth.read_pytest_vault_env",
                return_value=vault,
            ),
            mock.patch("install.ldap.cluster_has_htpasswd_identity", return_value=False),
            mock.patch("components.codeflare_sdk.auth.cluster_has_htpasswd_identity", return_value=False),
            mock.patch.dict(os.environ, {"CLUSTER_SOURCE": "EPHC"}, clear=False),
        ):
            overlay = apply_maas_billing_htpasswd_test_user_overrides()
            self.assertNotIn("TEST_USER_USERNAME", os.environ)
        self.assertEqual(overlay, {})

    def test_byoidc_overlay_when_credentials_ready(self) -> None:
        with (
            mock.patch("components.maas_billing.oidc_users._cluster_is_byoidc", return_value=True),
            mock.patch(
                "components.maas_billing.oidc_users._maas_billing_byoidc_overlay",
                return_value={
                    "CLUSTER_AUTH": "oidc",
                    "TEST_USER_USERNAME": "odh-user1",
                    "TEST_USER_PASSWORD": "pw",
                    "TEST_USER_AUTH_TYPE": "oidc",
                },
            ),
        ):
            overlay = apply_maas_billing_htpasswd_test_user_overrides()
        self.assertEqual(overlay["TEST_USER_USERNAME"], "odh-user1")
        self.assertEqual(os.environ["CLUSTER_AUTH"], "oidc")

    def test_merges_htpasswd_admin_from_env_when_vault_lacks_ocp_admin(self) -> None:
        vault = {
            "TEST_USER_USERNAME": "ldap-user1",
            "TEST_USER_PASSWORD": "ldap-pass",
        }
        env = {
            "HTPASSWD_CLUSTER_ADMIN_USER": "htpasswd-cluster-admin-user",
            "HTPASSWD_CLUSTER_PASSWORD": "admin-pass",
            "CLUSTER_SOURCE": "ods-qe-psi-07",
        }
        with (
            mock.patch("components.maas_billing.oidc_users._cluster_is_byoidc", return_value=False),
            mock.patch(
                "components.maas_billing.oidc_users._maas_billing_byoidc_overlay",
                return_value={},
            ),
            mock.patch("components.codeflare_sdk.auth.read_pytest_vault_env", return_value=vault),
            mock.patch("install.ldap.cluster_has_htpasswd_identity", return_value=False),
            mock.patch("components.codeflare_sdk.auth.cluster_has_htpasswd_identity", return_value=False),
            mock.patch.dict(os.environ, env, clear=False),
            mock.patch(
                "steps.tekton_util.materialize_htpasswd_kubeconfig_login",
                return_value=True,
            ),
        ):
            apply_maas_billing_htpasswd_test_user_overrides()
            self.assertEqual(os.environ["TEST_USER_USERNAME"], "htpasswd-cluster-admin-user")
            self.assertEqual(os.environ["TEST_USER_PASSWORD"], "admin-pass")

class MaasBillingRosaHcpPytestSkipTest(unittest.TestCase):
    def test_skip_when_rosa_hcp(self) -> None:
        with (
            mock.patch("components.maas_billing.oidc_users._cluster_is_byoidc", return_value=False),
            mock.patch("install.ldap._cluster_is_rosa_hcp", return_value=True),
            mock.patch("install.ldap.cluster_has_htpasswd_identity", return_value=False),
        ):
            self.assertTrue(maas_billing_rosa_hcp_skip_htpasswd_oauth_idp())
            extra = maas_billing_rosa_hcp_pytest_extra_args()
            self.assertIn("TestAPIKeyCRUD", extra)
            self.assertIn("TestBBRPreAuthInference", extra)
            self.assertIn("-k", extra)

    def test_no_skip_on_byoidc(self) -> None:
        with mock.patch("components.maas_billing.oidc_users._cluster_is_byoidc", return_value=True):
            self.assertFalse(maas_billing_rosa_hcp_skip_htpasswd_oauth_idp())
            self.assertEqual(maas_billing_rosa_hcp_pytest_extra_args(), "")

    def test_skip_aitenant_when_crd_missing(self) -> None:
        with mock.patch(
            "components.maas_billing.oidc_users.maas_billing_aitenant_crd_installed",
            return_value=False,
        ):
            extra = maas_billing_aitenant_pytest_extra_args()
            self.assertIn("aitenant", extra)
            self.assertIn("-k", extra)

    def test_no_skip_aitenant_when_crd_present(self) -> None:
        with mock.patch(
            "components.maas_billing.oidc_users.maas_billing_aitenant_crd_installed",
            return_value=True,
        ):
            self.assertEqual(maas_billing_aitenant_pytest_extra_args(), "")

    def test_skip_aitenant_bootstrap_on_external_cluster(self) -> None:
        from components.maas_billing.oidc_users import maas_billing_aitenant_bootstrap_pytest_extra_args

        with mock.patch.dict(
            os.environ,
            {"CLUSTER_SOURCE": "rhoai-e2e-kubeconfig-nmanos-konflux1-nmanos"},
            clear=False,
        ):
            extra = maas_billing_aitenant_bootstrap_pytest_extra_args()
            self.assertIn("test_aitenant_bootstrap_creates_tenant_environment", extra)

    def test_skip_aitenant_bootstrap_on_ephc(self) -> None:
        from components.maas_billing.oidc_users import maas_billing_aitenant_bootstrap_pytest_extra_args

        with mock.patch.dict(os.environ, {"CLUSTER_SOURCE": "EPHC"}, clear=False):
            extra = maas_billing_aitenant_bootstrap_pytest_extra_args()
            self.assertIn("test_aitenant_bootstrap_creates_tenant_environment", extra)

    def test_ephc_hypershift_skips_oauth_idp_tests(self) -> None:
        with (
            mock.patch("components.maas_billing.oidc_users._cluster_is_byoidc", return_value=False),
            mock.patch("install.ldap._cluster_is_rosa_hcp", return_value=False),
            mock.patch("install.ldap.cluster_has_htpasswd_identity", return_value=False),
            mock.patch(
                "install.rosa_hcp_pull_setup.is_hypershift_managed_cluster",
                return_value=True,
            ),
            mock.patch.dict(os.environ, {"CLUSTER_SOURCE": "EPHC"}, clear=False),
        ):
            self.assertTrue(maas_billing_rosa_hcp_skip_htpasswd_oauth_idp())
            self.assertIn("TestAPIKeyCRUD", maas_billing_rosa_hcp_pytest_extra_args())

