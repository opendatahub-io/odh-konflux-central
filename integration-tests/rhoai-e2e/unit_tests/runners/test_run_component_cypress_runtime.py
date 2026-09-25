"""Tests for component runner env file parsing and Cypress runtime helpers."""

from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock
from unittest.mock import patch

from components.dashboard_cypress.config import (
    discover_cypress_results_subdirs,
    prepend_cypress_shell_env,
)
from components.dashboard_cypress.auth_overlay import (
    _byoidc_cypress_poll_settings,
    gateway_cypress_uses_bearer_bypass,
    validate_gateway_cypress_auth,
)
from components.dashboard_cypress.runtime import (
    _apply_gateway_auth_overlay,
    _dashboard_npm_ci_command,
    _hoist_tslib_for_cypress,
    _reset_dashboard_src_if_ref_changed,
    inject_ci_auth_bypass,
    load_component_vault_env,
    patch_gateway_envoyfilter_if_needed,
    patch_runtime_cy_test_config,
    htpasswd_hcp_extra_cypress_skip_tags,
    byoidc_extra_cypress_skip_tags,
    ephc_bearer_extra_cypress_skip_tags,
    cypress_extra_skip_tags,
    resolve_gateway_auth_overlay,
    resolve_cypress_support_dir,
    resolve_test_clusters_overlay,
    sync_cypress_auth_env_from_config,
)
from suite.component_catalog_models import CypressParallelSet, CypressRunnerConfig
from suite.component_runner_env import load_component_runner_env

class DashboardCypressRuntimeTest(unittest.TestCase):
    def test_patch_runtime_cy_test_config(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            src = root / "vault.yml"
            src.write_text("ODH_DASHBOARD_URL: https://old.example\nFOO: bar\n", encoding="utf-8")
            out = patch_runtime_cy_test_config(
                root,
                cy_test_config=str(src),
                odh_dashboard_url="https://127.0.0.1:18443",
            )
            text = Path(out).read_text(encoding="utf-8")
            self.assertIn('ODH_DASHBOARD_URL: "https://127.0.0.1:18443"', text)
            self.assertIn("OPERATOR_NAMESPACE: redhat-ods-operator", text)
            self.assertIn("FOO: bar", text)

    def test_patch_runtime_cy_test_config_in_place(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            cfg = root / "cypress-runtime-config.yml"
            cfg.write_text("ODH_DASHBOARD_URL: https://old.example\n", encoding="utf-8")
            out = patch_runtime_cy_test_config(
                root,
                cy_test_config=str(cfg),
                odh_dashboard_url="https://dash.example",
            )
            self.assertEqual(out, str(cfg))
            self.assertIn("OPERATOR_NAMESPACE: redhat-ods-operator", cfg.read_text(encoding="utf-8"))

    def test_resolve_test_clusters_overlay_psi_pool_fallback(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            vault = Path(tmp) / "test-variables.yml"
            vault.write_text(
                "\n".join(
                    [
                        "CLUSTER_AUTH: oidc",
                        "TEST_CLUSTERS:",
                        "  ods-qe-01:",
                        "    TEST_USER:",
                        "      AUTH_TYPE: ldap-provider-qe",
                        "      USERNAME: ldap-admin1",
                        "      PASSWORD: secret",
                        "    OCP_ADMIN_USER:",
                        "      AUTH_TYPE: htpasswd-cluster-admin",
                        "      USERNAME: htpasswd-cluster-admin-user",
                    ]
                )
                + "\n",
                encoding="utf-8",
            )
            overlay = resolve_test_clusters_overlay(vault, "ods-qe-psi-07")
            self.assertEqual(overlay.get("CLUSTER_AUTH"), "")
            test_user = overlay.get("TEST_USER")
            self.assertIsInstance(test_user, dict)
            assert isinstance(test_user, dict)
            self.assertEqual(test_user.get("USERNAME"), "ldap-admin1")

    def test_patch_runtime_merges_test_clusters(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            vault = root / "vault.yml"
            vault.write_text(
                "\n".join(
                    [
                        "TEST_USER:",
                        "  AUTH_TYPE: oidc",
                        "  USERNAME: odh-user1",
                        "TEST_CLUSTERS:",
                        "  ods-qe-01:",
                        "    TEST_USER:",
                        "      AUTH_TYPE: ldap-provider-qe",
                        "      USERNAME: ldap-admin1",
                    ]
                )
                + "\n",
                encoding="utf-8",
            )
            out = patch_runtime_cy_test_config(
                root,
                cy_test_config=str(vault),
                odh_dashboard_url="https://rh-ai.apps.ods-qe-psi-07.osp.rh-ods.com",
                cluster_label="ods-qe-psi-07",
            )
            text = Path(out).read_text(encoding="utf-8")
            self.assertIn("ldap-admin1", text)
            self.assertIn("rh-ai.apps.ods-qe-psi-07", text)

    def test_resolve_gateway_auth_overlay_htpasswd_for_rosa_hcp(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            vault = Path(tmp) / "vault.yml"
            vault.write_text(
            "\n".join(
                [
                    "CLUSTER_AUTH: oidc",
                    "TEST_USER:",
                    "  AUTH_TYPE: oidc",
                    "  USERNAME: odh-user1",
                    "  PASSWORD: secret",
                    "TEST_CLUSTERS:",
                    "  ods-qe-01:",
                    "    OCP_ADMIN_USER:",
                    "      AUTH_TYPE: htpasswd-cluster-admin",
                    "      USERNAME: htpasswd-cluster-admin-user",
                    "      PASSWORD: htpw",
                ]
            )
            + "\n",
            encoding="utf-8",
            )
            with mock.patch(
                "install.ldap._cluster_is_byoidc",
                return_value=False,
            ), mock.patch(
                "install.ldap.cluster_has_htpasswd_identity",
                return_value=True,
            ):
                overlay = resolve_gateway_auth_overlay(
                    vault,
                    "nmanos-konflux1",
                    odh_dashboard_url="https://rh-ai.apps.rosa.example.com",
                )
            self.assertEqual(overlay.get("CLUSTER_AUTH"), "htpasswd-cluster-admin")
            test_user = overlay.get("TEST_USER")
            self.assertIsInstance(test_user, dict)
            assert isinstance(test_user, dict)
            self.assertEqual(test_user.get("AUTH_TYPE"), "htpasswd-cluster-admin")
            self.assertEqual(test_user.get("USERNAME"), "htpasswd-cluster-admin-user")
            admin_user = overlay.get("OCP_ADMIN_USER")
            self.assertIsInstance(admin_user, dict)
            assert isinstance(admin_user, dict)
            self.assertEqual(admin_user.get("AUTH_TYPE"), "htpasswd-cluster-admin")

    def test_resolve_gateway_auth_overlay_rosa_hcp_ods_qe01_without_cluster_idp(
        self,
    ) -> None:
        """Personal ROSA HCP without oauth htpasswd IdP uses pooled ods-qe-01 admin template."""
        with tempfile.TemporaryDirectory() as tmp:
            vault = Path(tmp) / "vault.yml"
            vault.write_text(
                "\n".join(
                    [
                        "CLUSTER_AUTH: oidc",
                        "TEST_USER:",
                        "  AUTH_TYPE: oidc",
                        "  USERNAME: odh-user1",
                        "TEST_CLUSTERS:",
                        "  ods-qe-01:",
                        "    OCP_ADMIN_USER:",
                        "      AUTH_TYPE: htpasswd-cluster-admin",
                        "      USERNAME: htpasswd-cluster-admin-user",
                        "      PASSWORD: htpw",
                    ]
                )
                + "\n",
                encoding="utf-8",
            )
            with mock.patch(
                "install.ldap._cluster_is_byoidc",
                return_value=False,
            ), mock.patch(
                "install.ldap.cluster_has_htpasswd_identity",
                return_value=False,
            ):
                overlay = resolve_gateway_auth_overlay(
                    vault,
                    "nmanos-konflux1",
                    odh_dashboard_url="https://rh-ai.apps.rosa.nmanos-konflux1.durx.p3.openshiftapps.com",
                )
            self.assertEqual(overlay.get("CLUSTER_AUTH"), "htpasswd-cluster-admin")
            test_user = overlay.get("TEST_USER")
            self.assertIsInstance(test_user, dict)
            assert isinstance(test_user, dict)
            self.assertEqual(test_user.get("USERNAME"), "htpasswd-cluster-admin-user")

    def test_resolve_gateway_auth_overlay_entry_both_htpasswd_without_cluster_idp(
        self,
    ) -> None:
        """Personal ROSA HCP: vault htpasswd users when oauth has no htpasswd IdP."""
        with tempfile.TemporaryDirectory() as tmp:
            vault = Path(tmp) / "vault.yml"
            vault.write_text(
                "\n".join(
                    [
                        "CLUSTER_AUTH: oidc",
                        "TEST_CLUSTERS:",
                        "  nmanos-konflux1:",
                        "    TEST_USER:",
                        "      AUTH_TYPE: htpasswd-cluster-admin",
                        "      USERNAME: htpasswd-cluster-admin-user",
                        "      PASSWORD: secret",
                        "    OCP_ADMIN_USER:",
                        "      AUTH_TYPE: htpasswd-cluster-admin",
                        "      USERNAME: htpasswd-cluster-admin-user",
                        "      PASSWORD: secret",
                    ]
                )
                + "\n",
                encoding="utf-8",
            )
            with mock.patch(
                "install.ldap._cluster_is_byoidc",
                return_value=False,
            ), mock.patch(
                "install.ldap.cluster_has_htpasswd_identity",
                return_value=False,
            ):
                overlay = resolve_gateway_auth_overlay(
                    vault,
                    "nmanos-konflux1",
                    odh_dashboard_url="https://rh-ai.apps.rosa.nmanos-konflux1.example.com",
                )
            self.assertEqual(overlay.get("CLUSTER_AUTH"), "htpasswd-cluster-admin")
            test_user = overlay.get("TEST_USER")
            self.assertIsInstance(test_user, dict)
            assert isinstance(test_user, dict)
            self.assertEqual(test_user.get("USERNAME"), "htpasswd-cluster-admin-user")

    def test_resolve_gateway_auth_overlay_patches_ocp_admin_when_entry_test_user_htpasswd(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            vault = Path(tmp) / "vault.yml"
            vault.write_text(
                "\n".join(
                    [
                        "CLUSTER_AUTH: oidc",
                        "TEST_CLUSTERS:",
                        "  nmanos-konflux1:",
                        "    TEST_USER:",
                        "      AUTH_TYPE: htpasswd-cluster-admin",
                        "      USERNAME: htpasswd-cluster-admin-user",
                        "      PASSWORD: secret",
                        "    OCP_ADMIN_USER:",
                        "      AUTH_TYPE: oidc",
                        "      USERNAME: odh-user1",
                        "      PASSWORD: x",
                    ]
                )
                + "\n",
                encoding="utf-8",
            )
            with mock.patch(
                "install.ldap._cluster_is_byoidc",
                return_value=False,
            ):
                overlay = resolve_gateway_auth_overlay(
                    vault,
                    "nmanos-konflux1",
                    odh_dashboard_url="https://rh-ai.apps.rosa.example.com",
                )
            self.assertEqual(overlay.get("CLUSTER_AUTH"), "htpasswd-cluster-admin")
            admin_user = overlay.get("OCP_ADMIN_USER")
            self.assertIsInstance(admin_user, dict)
            assert isinstance(admin_user, dict)
            self.assertEqual(admin_user.get("AUTH_TYPE"), "htpasswd-cluster-admin")

    def test_resolve_gateway_auth_overlay_patches_test_user_3_and_5(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            vault = Path(tmp) / "vault.yml"
            vault.write_text(
                "\n".join(
                    [
                        "CLUSTER_AUTH: oidc",
                        "TEST_USER_3:",
                        "  AUTH_TYPE: oidc",
                        "  USERNAME: ldap-contributor",
                        "  PASSWORD: x",
                        "TEST_USER_5:",
                        "  AUTH_TYPE: ldap",
                        "  USERNAME: ldap-admin",
                        "  PASSWORD: y",
                        "TEST_CLUSTERS:",
                        "  nmanos-konflux1:",
                        "    TEST_USER:",
                        "      AUTH_TYPE: htpasswd-cluster-admin",
                        "      USERNAME: htpasswd-cluster-admin-user",
                        "      PASSWORD: secret",
                        "    OCP_ADMIN_USER:",
                        "      AUTH_TYPE: oidc",
                        "      USERNAME: odh-user1",
                        "      PASSWORD: z",
                    ]
                )
                + "\n",
                encoding="utf-8",
            )
            with mock.patch(
                "install.ldap._cluster_is_byoidc",
                return_value=False,
            ):
                overlay = resolve_gateway_auth_overlay(
                    vault,
                    "nmanos-konflux1",
                    odh_dashboard_url="https://rh-ai.apps.rosa.example.com",
                )
            for key in ("TEST_USER_3", "TEST_USER_5"):
                user = overlay.get(key)
                self.assertIsInstance(user, dict, key)
                assert isinstance(user, dict)
                self.assertEqual(user.get("AUTH_TYPE"), "htpasswd-cluster-admin")
                self.assertEqual(user.get("USERNAME"), "htpasswd-cluster-admin-user")

    def test_byoidc_cypress_poll_settings_ephc_longer_wait(self) -> None:
        with mock.patch.dict(os.environ, {"CLUSTER_SOURCE": "EPHC"}, clear=False):
            retries, delay = _byoidc_cypress_poll_settings()
        self.assertEqual((retries, delay), (24, 15.0))
        with mock.patch.dict(os.environ, {}, clear=True):
            retries, delay = _byoidc_cypress_poll_settings()
        self.assertEqual((retries, delay), (6, 5.0))

    def test_resolve_gateway_auth_overlay_byoidc_from_cluster_secret(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            vault = Path(tmp) / "vault.yml"
            vault.write_text("CLUSTER_AUTH: oidc\n", encoding="utf-8")
            byoidc_user = {
                "AUTH_TYPE": "oidc",
                "USERNAME": "odh-user1",
                "PASSWORD": "secret",
            }
            with mock.patch(
                "install.ldap._cluster_is_byoidc",
                return_value=True,
            ), mock.patch(
                "components.maas_billing.oidc_users.byoidc_cypress_test_user",
                return_value=byoidc_user,
            ):
                overlay = resolve_gateway_auth_overlay(
                    vault,
                    "cluster-abc",
                    odh_dashboard_url="https://rh-ai.apps.example.com",
                )
            self.assertEqual(overlay.get("CLUSTER_AUTH"), "oidc")
            self.assertEqual(overlay.get("TEST_USER"), byoidc_user)
            self.assertEqual(overlay.get("OCP_ADMIN_USER"), byoidc_user)

    def test_resolve_gateway_auth_overlay_ephc_non_byoidc_without_openldap_returns_empty(
        self,
    ) -> None:
        """Non-BYOIDC EPHC without createIDP openldap must not use vault htpasswd template."""
        with tempfile.TemporaryDirectory() as tmp:
            vault = Path(tmp) / "vault.yml"
            vault.write_text(
                "\n".join(
                    [
                        "CLUSTER_AUTH: oidc",
                        "TEST_CLUSTERS:",
                        "  ods-qe-01:",
                        "    OCP_ADMIN_USER:",
                        "      AUTH_TYPE: htpasswd-cluster-admin",
                        "      USERNAME: htpasswd-cluster-admin-user",
                        "      PASSWORD: htpw",
                    ]
                )
                + "\n",
                encoding="utf-8",
            )
            ephc_url = "https://rh-ai.apps.foo.konflux-ocp-ci.dev"
            with mock.patch(
                "install.ldap._cluster_is_byoidc",
                return_value=False,
            ), mock.patch(
                "install.ldap.cluster_has_htpasswd_identity",
                return_value=False,
            ), mock.patch(
                "install.ldap._openldap_secret_ready",
                return_value=False,
            ), mock.patch.dict(os.environ, {"CLUSTER_SOURCE": "EPHC"}, clear=False):
                overlay = resolve_gateway_auth_overlay(
                    vault,
                    "ephc-cluster",
                    odh_dashboard_url=ephc_url,
                )
            self.assertEqual(overlay, {})

    def test_gateway_cypress_uses_bearer_bypass_konflux_ocp_ci_guest(self) -> None:
        ephc_url = "https://rh-ai.apps.foo.prod.konflux-ocp-ci.dev"
        with mock.patch("install.ldap._cluster_is_byoidc", return_value=False), mock.patch(
            "install.ldap.cluster_has_ldap_identity",
            return_value=False,
        ), mock.patch(
            "install.ldap.cluster_has_htpasswd_identity",
            return_value=False,
        ):
            self.assertTrue(
                gateway_cypress_uses_bearer_bypass(odh_dashboard_url=ephc_url)
            )

    def test_gateway_cypress_uses_bearer_bypass_oci_ephemeral_even_with_htpasswd_idp(self) -> None:
        """OpenShift CI guests use hosted-mgmt2 OAuth; overlay htpasswd login 401s (ctcml)."""
        oci_url = "https://rh-ai.apps.677cb64a4620c92044ef.prod.konflux-ocp-ci.dev"
        with mock.patch("install.ldap._cluster_is_byoidc", return_value=False), mock.patch(
            "install.ldap.cluster_has_ldap_identity",
            return_value=False,
        ), mock.patch(
            "install.ldap.cluster_has_htpasswd_identity",
            return_value=True,
        ):
            self.assertTrue(
                gateway_cypress_uses_bearer_bypass(odh_dashboard_url=oci_url)
            )

    def test_gateway_cypress_keeps_bearer_when_openldap_secret_only(self) -> None:
        """HostedCluster VAP: openldap secret without OAuth IdP still needs bearer."""
        ephc_url = "https://rh-ai.apps.foo.prod.konflux-ocp-ci.dev"
        with mock.patch("install.ldap._cluster_is_byoidc", return_value=False), mock.patch(
            "install.ldap._openldap_secret_ready",
            return_value=True,
        ), mock.patch(
            "install.ldap.cluster_has_ldap_identity",
            return_value=False,
        ), mock.patch(
            "install.ldap.cluster_has_htpasswd_identity",
            return_value=False,
        ):
            self.assertTrue(
                gateway_cypress_uses_bearer_bypass(odh_dashboard_url=ephc_url)
            )

    def test_gateway_cypress_uses_bearer_bypass_ephc_when_oauth_ldap(self) -> None:
        ephc_url = "https://rh-ai.apps.foo.prod.konflux-ocp-ci.dev"
        with mock.patch("install.ldap._cluster_is_byoidc", return_value=False), mock.patch(
            "install.ldap.cluster_has_ldap_identity",
            return_value=True,
        ):
            self.assertTrue(
                gateway_cypress_uses_bearer_bypass(odh_dashboard_url=ephc_url)
            )

    def test_gateway_cypress_uses_bearer_bypass_external_hcp_false(self) -> None:
        external = "https://rh-ai.apps.ods-qe-01.osp.rh-ods.com"
        with mock.patch("install.ldap._cluster_is_byoidc", return_value=False):
            self.assertFalse(
                gateway_cypress_uses_bearer_bypass(odh_dashboard_url=external)
            )

    def test_validate_gateway_cypress_auth_ephc_requires_oc_token(self) -> None:
        ephc_url = "https://rh-ai.apps.foo.prod.konflux-ocp-ci.dev"
        with mock.patch("install.ldap._cluster_is_byoidc", return_value=False), mock.patch(
            "install.ldap.cluster_has_ldap_identity",
            return_value=False,
        ), mock.patch(
            "install.ldap.cluster_has_htpasswd_identity",
            return_value=False,
        ), mock.patch.dict(os.environ, {}, clear=True):
            self.assertEqual(
                validate_gateway_cypress_auth(odh_dashboard_url=ephc_url),
                2,
            )

    def test_validate_gateway_cypress_auth_ephc_with_token_ok(self) -> None:
        ephc_url = "https://rh-ai.apps.foo.prod.konflux-ocp-ci.dev"
        with mock.patch("install.ldap._cluster_is_byoidc", return_value=False), mock.patch(
            "install.ldap.cluster_has_ldap_identity",
            return_value=False,
        ), mock.patch(
            "install.ldap.cluster_has_htpasswd_identity",
            return_value=False,
        ), mock.patch.dict(os.environ, {"OC_TOKEN": "tok"}, clear=True):
            self.assertIsNone(validate_gateway_cypress_auth(odh_dashboard_url=ephc_url))

    def test_resolve_gateway_auth_overlay_ephc_openldap_skips_htpasswd_for_bearer(
        self,
    ) -> None:
        """EPHC openldap secret without OAuth IdP keeps bearer; no vault htpasswd overlay."""
        with tempfile.TemporaryDirectory() as tmp:
            vault = Path(tmp) / "vault.yml"
            vault.write_text(
                "\n".join(
                    [
                        "CLUSTER_AUTH: oidc",
                        "TEST_USER:",
                        "  AUTH_TYPE: oidc",
                        "  USERNAME: odh-user1",
                        "TEST_CLUSTERS:",
                        "  ods-qe-01:",
                        "    OCP_ADMIN_USER:",
                        "      AUTH_TYPE: htpasswd-cluster-admin",
                        "      USERNAME: htpasswd-cluster-admin-user",
                        "      PASSWORD: htpw",
                    ]
                )
                + "\n",
                encoding="utf-8",
            )
            ephc_url = "https://rh-ai.apps.foo.konflux-ocp-ci.dev"
            with mock.patch(
                "install.ldap._cluster_is_byoidc",
                return_value=False,
            ), mock.patch(
                "install.ldap.cluster_has_htpasswd_identity",
                return_value=False,
            ), mock.patch(
                "install.ldap.cluster_has_ldap_identity",
                return_value=False,
            ), mock.patch(
                "install.ldap._openldap_secret_ready",
                return_value=True,
            ), mock.patch.dict(os.environ, {"CLUSTER_SOURCE": "EPHC"}, clear=False):
                overlay = resolve_gateway_auth_overlay(
                    vault,
                    "ephc-cluster",
                    odh_dashboard_url=ephc_url,
                )
            self.assertEqual(overlay, {})

    def test_resolve_gateway_auth_overlay_ephc_konflux_ocp_ci_url_without_cluster_source_env(
        self,
    ) -> None:
        """konflux-ocp-ci.dev guest URL without OAuth IdP still uses bearer bypass (no vault overlay)."""
        with tempfile.TemporaryDirectory() as tmp:
            vault = Path(tmp) / "vault.yml"
            vault.write_text(
                "\n".join(
                    [
                        "CLUSTER_AUTH: oidc",
                        "TEST_CLUSTERS:",
                        "  ods-qe-01:",
                        "    OCP_ADMIN_USER:",
                        "      AUTH_TYPE: htpasswd-cluster-admin",
                        "      USERNAME: htpasswd-cluster-admin-user",
                        "      PASSWORD: htpw",
                    ]
                )
                + "\n",
                encoding="utf-8",
            )
            ephc_url = "https://rh-ai.apps.foo.prod.konflux-ocp-ci.dev"
            with mock.patch(
                "install.ldap._cluster_is_byoidc",
                return_value=False,
            ), mock.patch(
                "install.ldap.cluster_has_htpasswd_identity",
                return_value=False,
            ), mock.patch(
                "install.ldap.cluster_has_ldap_identity",
                return_value=False,
            ), mock.patch(
                "install.ldap._openldap_secret_ready",
                return_value=False,
            ), mock.patch.dict(os.environ, {}, clear=True):
                overlay = resolve_gateway_auth_overlay(
                    vault,
                    "ephc-cluster",
                    odh_dashboard_url=ephc_url,
                )
            self.assertEqual(overlay, {})

    def test_resolve_gateway_auth_overlay_ephc_openldap_without_oauth_skips_ldap_overlay(
        self,
    ) -> None:
        """EPHC openldap secret without OAuth IdP keeps bearer; vault LDAP overlay skipped."""
        with tempfile.TemporaryDirectory() as tmp:
            vault = Path(tmp) / "vault.yml"
            vault.write_text(
                "\n".join(
                    [
                        "TEST_USER:",
                        "  AUTH_TYPE: ldap",
                        "  USERNAME: ldap-user1",
                        "  PASSWORD: ldap-secret",
                    ]
                )
                + "\n",
                encoding="utf-8",
            )
            with mock.patch(
                "install.ldap._cluster_is_byoidc",
                return_value=False,
            ), mock.patch(
                "install.ldap.cluster_has_htpasswd_identity",
                return_value=False,
            ), mock.patch(
                "install.ldap.cluster_has_ldap_identity",
                return_value=False,
            ), mock.patch(
                "install.ldap._openldap_secret_ready",
                return_value=True,
            ), mock.patch.dict(os.environ, {"CLUSTER_SOURCE": "EPHC"}, clear=False):
                overlay = resolve_gateway_auth_overlay(
                    vault,
                    "ephc-cluster",
                    odh_dashboard_url="https://rh-ai.apps.foo.konflux-ocp-ci.dev",
                )
            self.assertEqual(overlay, {})

    def test_resolve_gateway_auth_overlay_byoidc_pooled_psi_uses_keycloak_not_vault_ldap(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            vault = Path(tmp) / "vault.yml"
            vault.write_text(
                "\n".join(
                    [
                        "CLUSTER_AUTH: oidc",
                        "TEST_CLUSTERS:",
                        "  ods-qe-01:",
                        "    TEST_USER:",
                        "      AUTH_TYPE: ldap-provider-qe",
                        "      USERNAME: ldap-admin1",
                        "      PASSWORD: ldap-secret",
                    ]
                )
                + "\n",
                encoding="utf-8",
            )
            byoidc_user = {
                "AUTH_TYPE": "oidc",
                "USERNAME": "odh-user1",
                "PASSWORD": "secret",
            }
            with mock.patch(
                "install.ldap._cluster_is_byoidc",
                return_value=True,
            ), mock.patch(
                "components.maas_billing.oidc_users.byoidc_cypress_test_user",
                return_value=byoidc_user,
            ):
                overlay = resolve_gateway_auth_overlay(
                    vault,
                    "ods-qe-psi-23",
                    odh_dashboard_url="https://rh-ai.apps.ods-qe-psi-23.osp.rh-ods.com",
                )
            self.assertEqual(overlay.get("CLUSTER_AUTH"), "oidc")
            self.assertEqual(overlay.get("TEST_USER"), byoidc_user)

    def test_apply_gateway_auth_overlay_byoidc_replaces_merged_ldap(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            vault = root / "vault.yml"
            runtime = root / "runtime.yml"
            vault.write_text("CLUSTER_AUTH: oidc\n", encoding="utf-8")
            runtime.write_text(
                "\n".join(
                    [
                        "CLUSTER_AUTH: ''",
                        "TEST_USER:",
                        "  AUTH_TYPE: ldap-provider-qe",
                        "  USERNAME: ldap-admin1",
                        "  PASSWORD: ldap-secret",
                    ]
                )
                + "\n",
                encoding="utf-8",
            )
            byoidc_user = {
                "AUTH_TYPE": "oidc",
                "USERNAME": "odh-user1",
                "PASSWORD": "secret",
            }
            with mock.patch(
                "install.ldap._cluster_is_byoidc",
                return_value=True,
            ), mock.patch(
                "components.maas_billing.oidc_users.byoidc_cypress_test_user",
                return_value=byoidc_user,
            ):
                _apply_gateway_auth_overlay(
                    runtime,
                    vault,
                    cluster_label="ods-qe-psi-23",
                    odh_dashboard_url="https://rh-ai.apps.ods-qe-psi-23.osp.rh-ods.com",
                )
            import yaml

            merged = yaml.safe_load(runtime.read_text(encoding="utf-8"))
            self.assertEqual(merged.get("CLUSTER_AUTH"), "oidc")
            self.assertEqual(merged.get("TEST_USER"), byoidc_user)

    def test_resolve_gateway_auth_overlay_ldap_test_user_htpasswd_admin_uses_htpasswd(self) -> None:
        """ENV-2: Jenkins parity - Cypress uses OCP_ADMIN_USER (htpasswd) on LDAP clusters."""
        with tempfile.TemporaryDirectory() as tmp:
            vault = Path(tmp) / "vault.yml"
            vault.write_text(
                "\n".join(
                    [
                        "CLUSTER_AUTH: oidc",
                        "TEST_CLUSTERS:",
                        "  ods-qe-psi-07:",
                        "    TEST_USER:",
                        "      AUTH_TYPE: ldap-provider-qe",
                        "      USERNAME: ldap-admin1",
                        "      PASSWORD: ldap-secret",
                        "    OCP_ADMIN_USER:",
                        "      AUTH_TYPE: htpasswd-cluster-admin",
                        "      USERNAME: htpasswd-cluster-admin-user",
                        "      PASSWORD: htpw",
                    ]
                )
                + "\n",
                encoding="utf-8",
            )
            with mock.patch(
                "install.ldap._cluster_is_byoidc",
                return_value=False,
            ):
                overlay = resolve_gateway_auth_overlay(
                    vault,
                    "ods-qe-psi-07",
                    odh_dashboard_url="https://rh-ai.apps.ods-qe-psi-07.osp.rh-ods.com",
                )
            self.assertEqual(overlay.get("CLUSTER_AUTH"), "htpasswd-cluster-admin")
            test_user = overlay.get("TEST_USER")
            self.assertIsInstance(test_user, dict)
            assert isinstance(test_user, dict)
            self.assertEqual(test_user.get("USERNAME"), "htpasswd-cluster-admin-user")
            self.assertEqual(test_user.get("AUTH_TYPE"), "htpasswd-cluster-admin")

    def test_resolve_gateway_auth_overlay_ldap_test_user_ldap_admin_applies_overlay(self) -> None:
        """Both TEST_USER and OCP_ADMIN_USER are LDAP - apply vault ldap gateway overlay."""
        with tempfile.TemporaryDirectory() as tmp:
            vault = Path(tmp) / "vault.yml"
            vault.write_text(
                "\n".join(
                    [
                        "CLUSTER_AUTH: oidc",
                        "TEST_CLUSTERS:",
                        "  cluster-x:",
                        "    TEST_USER:",
                        "      AUTH_TYPE: ldap-provider-qe",
                        "      USERNAME: ldap-admin1",
                        "      PASSWORD: ldap-secret",
                        "    OCP_ADMIN_USER:",
                        "      AUTH_TYPE: ldap-provider-qe",
                        "      USERNAME: ldap-admin2",
                        "      PASSWORD: ldap-secret",
                    ]
                )
                + "\n",
                encoding="utf-8",
            )
            with mock.patch(
                "install.ldap._cluster_is_byoidc",
                return_value=False,
            ):
                overlay = resolve_gateway_auth_overlay(
                    vault,
                    "cluster-x",
                    odh_dashboard_url="https://rh-ai.apps.cluster-x.example.com",
                )
            # Non-OIDC vault TEST_USER (ldap) on non-BYOIDC cluster → gateway overlay.
            self.assertEqual(overlay.get("CLUSTER_AUTH"), "ldap-provider-qe")
            self.assertEqual(
                overlay.get("TEST_USER"),
                {
                    "AUTH_TYPE": "ldap-provider-qe",
                    "USERNAME": "ldap-admin1",
                    "PASSWORD": "ldap-secret",
                },
            )

    def test_resolve_gateway_auth_overlay_pooled_psi_non_byoidc_prefers_vault_ldap(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            vault = Path(tmp) / "vault.yml"
            vault.write_text(
                "\n".join(
                    [
                        "CLUSTER_AUTH: oidc",
                        "TEST_CLUSTERS:",
                        "  ods-qe-psi-23:",
                        "    ODH_DASHBOARD_URL: https://rh-ai.apps.ods-qe-psi-23.osp.rh-ods.com",
                        "  ods-qe-01:",
                        "    TEST_USER:",
                        "      AUTH_TYPE: ldap-provider-qe",
                        "      USERNAME: ldap-admin1",
                        "      PASSWORD: ldap-secret",
                    ]
                )
                + "\n",
                encoding="utf-8",
            )
            with mock.patch(
                "install.ldap._cluster_is_byoidc",
                return_value=False,
            ):
                overlay = resolve_gateway_auth_overlay(
                    vault,
                    "ods-qe-psi-23",
                    odh_dashboard_url="https://rh-ai.apps.ods-qe-psi-23.osp.rh-ods.com",
                )
            test_user = overlay.get("TEST_USER")
            self.assertIsInstance(test_user, dict)
            assert isinstance(test_user, dict)
            self.assertEqual(test_user.get("USERNAME"), "ldap-admin1")
            self.assertEqual(overlay.get("CLUSTER_AUTH"), "ldap-provider-qe")

    def test_resolve_gateway_auth_overlay_byoidc_psi_entry_uses_keycloak(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            vault = Path(tmp) / "vault.yml"
            vault.write_text(
                "\n".join(
                    [
                        "CLUSTER_AUTH: oidc",
                        "TEST_CLUSTERS:",
                        "  ods-qe-psi-23:",
                        "    ODH_DASHBOARD_URL: https://rh-ai.apps.ods-qe-psi-23.osp.rh-ods.com",
                        "  ods-qe-01:",
                        "    TEST_USER:",
                        "      AUTH_TYPE: ldap-provider-qe",
                        "      USERNAME: ldap-admin1",
                        "      PASSWORD: ldap-secret",
                    ]
                )
                + "\n",
                encoding="utf-8",
            )
            byoidc_user = {
                "AUTH_TYPE": "oidc",
                "USERNAME": "odh-user1",
                "PASSWORD": "secret",
            }
            with mock.patch(
                "install.ldap._cluster_is_byoidc",
                return_value=True,
            ), mock.patch(
                "components.maas_billing.oidc_users.byoidc_cypress_test_user",
                return_value=byoidc_user,
            ):
                overlay = resolve_gateway_auth_overlay(
                    vault,
                    "ods-qe-psi-23",
                    odh_dashboard_url="https://rh-ai.apps.ods-qe-psi-23.osp.rh-ods.com",
                )
            self.assertEqual(overlay.get("CLUSTER_AUTH"), "oidc")
            self.assertEqual(overlay.get("TEST_USER"), byoidc_user)

    def test_resolve_gateway_auth_overlay_byoidc_empty_without_secret(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            vault = Path(tmp) / "vault.yml"
            vault.write_text("CLUSTER_AUTH: oidc\n", encoding="utf-8")
            with (
                mock.patch("install.ldap._cluster_is_byoidc", return_value=True),
                mock.patch(
                    "components.maas_billing.oidc_users.byoidc_cypress_test_user",
                    return_value=None,
                ),
                mock.patch("components.dashboard_cypress.auth_overlay.time.sleep"),
            ):
                overlay = resolve_gateway_auth_overlay(
                    vault,
                    "cluster-abc",
                    odh_dashboard_url="https://rh-ai.apps.example.com",
                )
            self.assertEqual(overlay, {})

    def test_htpasswd_hcp_extra_cypress_skip_tags_without_ldap(self) -> None:
        with mock.patch(
            "install.ldap._cluster_is_byoidc",
            return_value=False,
        ), mock.patch(
            "install.ldap.cluster_has_ldap_identity",
            return_value=False,
        ):
            extra = htpasswd_hcp_extra_cypress_skip_tags(
                odh_dashboard_url="https://rh-ai.apps.rosa.example.com",
            )
        self.assertIn("@ModelServingCI", extra)
        self.assertIn("@ProjectsCI", extra)
        self.assertIn("@FeatureStore", extra)
        self.assertIn("@SettingsCI", extra)
        self.assertIn("@NotebookAdministration", extra)
        self.assertIn("@MaaS", extra)
        self.assertIn("@AutoML", extra)

    def test_htpasswd_hcp_extra_cypress_skip_tags_skips_when_ldap_present(self) -> None:
        with mock.patch(
            "install.ldap._cluster_is_byoidc",
            return_value=False,
        ), mock.patch(
            "install.ldap.cluster_has_ldap_identity",
            return_value=True,
        ):
            extra = htpasswd_hcp_extra_cypress_skip_tags(
                odh_dashboard_url="https://rh-ai.apps.rosa.example.com",
            )
        self.assertEqual(extra, "")

    def test_byoidc_extra_cypress_skip_tags(self) -> None:
        with mock.patch(
            "install.ldap._cluster_is_byoidc",
            return_value=True,
        ):
            extra = byoidc_extra_cypress_skip_tags(
                odh_dashboard_url="https://rh-ai.apps.ods-qe-psi-23.osp.rh-ods.com",
            )
        self.assertIn("@FeatureStore", extra)
        self.assertIn("@SettingsCI", extra)
        self.assertIn("@MaaSCI", extra)
        self.assertIn("@ModelServingCI", extra)
        self.assertIn("@AutoMLCI", extra)

    def test_ephc_bearer_extra_cypress_skip_tags(self) -> None:
        ephc_url = "https://rh-ai.apps.abc123.prod.konflux-ocp-ci.dev"
        with mock.patch(
            "install.ldap._cluster_is_byoidc",
            return_value=False,
        ), mock.patch(
            "install.ldap.cluster_has_ldap_identity",
            return_value=False,
        ), mock.patch(
            "install.ldap.cluster_has_htpasswd_identity",
            return_value=False,
        ), mock.patch(
            "components.dashboard_cypress.runtime._is_ephc_cluster_source",
            return_value=True,
        ):
            extra = ephc_bearer_extra_cypress_skip_tags(odh_dashboard_url=ephc_url)
        self.assertIn("@FeatureStore", extra)
        self.assertIn("@NotebookAdministration", extra)
        self.assertIn("@ModelServingCI", extra)
        self.assertEqual(
            ephc_bearer_extra_cypress_skip_tags(
                odh_dashboard_url="https://rh-ai.apps.rosa.example.com",
            ),
            "",
        )

    def test_htpasswd_skips_applied_on_ephc_bearer(self) -> None:
        ephc_url = "https://rh-ai.apps.abc123.prod.konflux-ocp-ci.dev"
        with mock.patch(
            "install.ldap._cluster_is_byoidc",
            return_value=False,
        ), mock.patch(
            "install.ldap.cluster_has_ldap_identity",
            return_value=False,
        ), mock.patch(
            "install.ldap.cluster_has_htpasswd_identity",
            return_value=False,
        ), mock.patch(
            "components.dashboard_cypress.runtime._is_ephc_cluster_source",
            return_value=True,
        ):
            extra = cypress_extra_skip_tags(odh_dashboard_url=ephc_url)
        self.assertIn("@ModelServingCI", extra)
        self.assertIn("@ProjectsCI", extra)
        self.assertIn("@FeatureStore", extra)

    def test_cypress_extra_skip_tags_merges_byoidc_and_konflux(self) -> None:
        with mock.patch(
            "install.ldap._cluster_is_byoidc",
            return_value=True,
        ), mock.patch.dict(
            os.environ,
            {"ARTIFACTS": "/workspace/artifacts"},
            clear=False,
        ):
            extra = cypress_extra_skip_tags(
                odh_dashboard_url="https://rh-ai.apps.ods-qe-psi-23.osp.rh-ods.com",
            )
        self.assertIn("@FeatureStore", extra)
        self.assertIn("@ODS-327", extra)

    def test_automl_s3_skip_tags_when_no_s3(self) -> None:
        """ENV-3: @AutoML skipped when CY_TEST_CONFIG has no AWS_PIPELINES."""
        from components.dashboard_cypress.runtime import _automl_s3_skip_tags

        with tempfile.TemporaryDirectory() as tmp:
            cfg = Path(tmp) / "runtime.yml"
            cfg.write_text("ODH_DASHBOARD_URL: https://dash.example\n", encoding="utf-8")
            with mock.patch.dict(os.environ, {"CY_TEST_CONFIG": str(cfg)}, clear=False):
                tags = _automl_s3_skip_tags()
        self.assertIn("@AutoML", tags)
        self.assertIn("@AutoMLCI", tags)

    def test_automl_s3_skip_tags_when_s3_present(self) -> None:
        """ENV-3: No AutoML skip when AWS_PIPELINES exists in config."""
        from components.dashboard_cypress.runtime import _automl_s3_skip_tags

        with tempfile.TemporaryDirectory() as tmp:
            cfg = Path(tmp) / "runtime.yml"
            cfg.write_text(
                "AWS_PIPELINES:\n  AWS_ACCESS_KEY_ID: ak\n  BUCKET_2:\n    NAME: b\n",
                encoding="utf-8",
            )
            with mock.patch.dict(os.environ, {"CY_TEST_CONFIG": str(cfg)}, clear=False):
                tags = _automl_s3_skip_tags()
        self.assertEqual(tags, "")

    def test_patch_runtime_applies_htpasswd_overlay(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            vault = root / "vault.yml"
            vault.write_text(
                "\n".join(
                    [
                        "CLUSTER_AUTH: oidc",
                        "TEST_USER:",
                        "  AUTH_TYPE: oidc",
                        "  USERNAME: odh-user1",
                        "TEST_CLUSTERS:",
                        "  ods-qe-01:",
                        "    OCP_ADMIN_USER:",
                        "      AUTH_TYPE: htpasswd-cluster-admin",
                        "      USERNAME: htpasswd-cluster-admin-user",
                        "      PASSWORD: htpw",
                    ]
                )
                + "\n",
                encoding="utf-8",
            )
            with mock.patch(
                "install.ldap._cluster_is_byoidc",
                return_value=False,
            ), mock.patch(
                "install.ldap.cluster_has_htpasswd_identity",
                return_value=True,
            ):
                out = patch_runtime_cy_test_config(
                    root,
                    cy_test_config=str(vault),
                    odh_dashboard_url="https://rh-ai.apps.rosa.example.com",
                    cluster_label="nmanos-konflux1",
                )
            text = Path(out).read_text(encoding="utf-8")
            self.assertNotIn("CLUSTER_AUTH: oidc", text)
            self.assertIn("CLUSTER_AUTH: htpasswd-cluster-admin", text)
            self.assertIn("htpasswd-cluster-admin-user", text)

    def test_sync_cypress_auth_env_sets_htpasswd_idp(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cfg = Path(tmp) / "runtime.yml"
            cfg.write_text(
                "\n".join(
                    [
                        "TEST_USER:",
                        "  AUTH_TYPE: htpasswd-cluster-admin",
                        "  USERNAME: htpasswd-cluster-admin-user",
                        "  PASSWORD: secret",
                    ]
                )
                + "\n",
                encoding="utf-8",
            )
            with mock.patch(
                "install.ldap._cluster_is_byoidc",
                return_value=False,
            ):
                with mock.patch.dict(os.environ, {}, clear=True):
                    sync_cypress_auth_env_from_config(cfg)
                    self.assertEqual(os.environ.get("CLUSTER_AUTH"), "htpasswd-cluster-admin")
                    self.assertEqual(
                        os.environ.get("TEST_USER_USERNAME"), "htpasswd-cluster-admin-user"
                    )

    def test_inject_ci_auth_bypass(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            support = root / "packages" / "cypress" / "cypress" / "support"
            support.mkdir(parents=True)
            (support / "e2e.ts").write_text("import './commands';\n", encoding="utf-8")
            inject_ci_auth_bypass(root / "frontend")
            self.assertTrue((support / "ci-auth-bypass.ts").is_file())
            self.assertIn("ci-auth-bypass", (support / "e2e.ts").read_text(encoding="utf-8"))

    def test_merge_cypress_s3_overlay_from_env(self) -> None:
        from components.dashboard_cypress.auth_overlay import _merge_cypress_s3_overlay

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            runtime = root / "runtime.yml"
            vault = root / "vault.yml"
            runtime.write_text("ODH_DASHBOARD_URL: https://dash.example\n", encoding="utf-8")
            vault.write_text("CLUSTER_AUTH: htpasswd\n", encoding="utf-8")
            with mock.patch.dict(
                os.environ,
                {
                    "AWS_ACCESS_KEY_ID": "ak",
                    "AWS_SECRET_ACCESS_KEY": "sk",
                    "CI_S3_BUCKET_NAME": "ods-ci-s3",
                    "CI_S3_BUCKET_REGION": "us-east-1",
                    "CI_S3_BUCKET_ENDPOINT": "https://s3.us-east-1.amazonaws.com",
                },
                clear=False,
            ):
                _merge_cypress_s3_overlay(runtime, vault)
            text = runtime.read_text(encoding="utf-8")
            self.assertIn("AWS_PIPELINES:", text)
            self.assertIn("BUCKET_2:", text)
            self.assertIn("ods-ci-s3", text)

    def test_merge_cypress_s3_overlay_from_vault(self) -> None:
        from components.dashboard_cypress.auth_overlay import _merge_cypress_s3_overlay

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            runtime = root / "runtime.yml"
            vault = root / "vault.yml"
            runtime.write_text("ODH_DASHBOARD_URL: https://dash.example\n", encoding="utf-8")
            vault.write_text(
                "\n".join(
                    [
                        "AWS_PIPELINES:",
                        "  AWS_ACCESS_KEY_ID: vault-ak",
                        "  AWS_SECRET_ACCESS_KEY: vault-sk",
                        "  BUCKET_2:",
                        "    NAME: vault-bucket",
                        "    REGION: us-west-2",
                        "    ENDPOINT: https://s3.us-west-2.amazonaws.com",
                    ]
                )
                + "\n",
                encoding="utf-8",
            )
            _merge_cypress_s3_overlay(runtime, vault)
            text = runtime.read_text(encoding="utf-8")
            self.assertIn("vault-ak", text)
            self.assertIn("vault-bucket", text)

    def test_merge_cypress_s3_overlay_skips_when_present(self) -> None:
        from components.dashboard_cypress.auth_overlay import _merge_cypress_s3_overlay

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            runtime = root / "runtime.yml"
            vault = root / "vault.yml"
            runtime.write_text(
                "AWS_PIPELINES:\n  BUCKET_2:\n    NAME: existing\n",
                encoding="utf-8",
            )
            vault.write_text("AWS_PIPELINES:\n  BUCKET_2:\n    NAME: vault-only\n", encoding="utf-8")
            _merge_cypress_s3_overlay(runtime, vault)
            text = runtime.read_text(encoding="utf-8")
            self.assertIn("existing", text)
            self.assertNotIn("vault-only", text)

    def test_resolve_cypress_support_dir(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            support = root / "packages" / "cypress" / "cypress" / "support"
            support.mkdir(parents=True)
            resolved = resolve_cypress_support_dir(root / "frontend")
            self.assertEqual(resolved, support)

    @patch("components.dashboard_cypress.runtime._VAULT_MOUNT")
    def test_load_component_vault_env(self, mount_mock) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            vault = Path(tmp)
            mount_mock.is_dir.return_value = True
            mount_mock.iterdir.return_value = [
                vault / "CY_USERNAME",
                vault / "CY_TEST_CONFIG",
                vault / "AWS_CA_BUNDLE",
            ]
            (vault / "CY_USERNAME").write_text("admin\n", encoding="utf-8")
            (vault / "CY_TEST_CONFIG").write_text("ODH_DASHBOARD_URL: x\n", encoding="utf-8")
            (vault / "AWS_CA_BUNDLE").write_text("pem", encoding="utf-8")
            with patch("components.dashboard_cypress.runtime._VAULT_MOUNT", vault):
                env = load_component_vault_env()
            self.assertEqual(env["CY_USERNAME"], "admin")
            self.assertEqual(env["CY_TEST_CONFIG"], str(vault / "CY_TEST_CONFIG"))
            self.assertNotIn("AWS_CA_BUNDLE", env)

    @patch("components.dashboard_cypress.runtime.oc_run")
    def test_patch_gateway_envoyfilter_finds_namespace_without_name_across_ns(self, oc_mock) -> None:
        def fake_oc(args, **kwargs):
            cmd = args if isinstance(args, list) else []
            if cmd[:3] == ["get", "service", "kube-auth-proxy"]:
                return type("R", (), {"returncode": 1, "stdout": "", "stderr": ""})()
            if len(cmd) >= 4 and cmd[0] == "get" and "envoyfilter" in cmd[1] and cmd[2] == "-n":
                ns = cmd[3]
                if ns == "openshift-ingress":
                    return type("R", (), {"returncode": 0, "stdout": "", "stderr": ""})()
                return type("R", (), {"returncode": 1, "stdout": "", "stderr": ""})()
            if cmd[:2] == ["get", "envoyfilter"] and "-A" in cmd:
                return type("R", (), {"returncode": 0, "stdout": "", "stderr": ""})()
            if "failure_mode_allow" in " ".join(cmd):
                return type("R", (), {"returncode": 0, "stdout": "false", "stderr": ""})()
            if cmd[0] == "patch":
                return type("R", (), {"returncode": 0, "stdout": "", "stderr": ""})()
            return type("R", (), {"returncode": 1, "stdout": "", "stderr": ""})()

        oc_mock.side_effect = fake_oc
        patch_gateway_envoyfilter_if_needed()
        patch_calls = [c for c in oc_mock.call_args_list if c.args[0][0] == "patch"]
        self.assertTrue(patch_calls)
        self.assertEqual(patch_calls[0].args[0][3], "openshift-ingress")
