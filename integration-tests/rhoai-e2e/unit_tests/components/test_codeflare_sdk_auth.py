#!/usr/bin/env python3
"""Tests for CodeFlare SDK htpasswd auth overlay on external clusters."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest import mock

from components.codeflare_sdk.auth import (  # noqa: E402
    codeflare_dashboard_url_overlay,
    codeflare_ephc_kubeconfig_overlay,
    codeflare_env_overrides_from_vault,
    codeflare_htpasswd_test_user_overlay,
    read_flat_vault_env,
    read_pytest_vault_env,
)

class CodeflareSdkAuthTest(unittest.TestCase):
    def test_read_flat_vault_env(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "TEST_USER_USERNAME").write_text("ldap-admin1\n", encoding="utf-8")
            (root / "OCP_ADMIN_USER_PASSWORD").write_text("secret\n", encoding="utf-8")
            env = read_flat_vault_env(root)
            self.assertEqual(env["TEST_USER_USERNAME"], "ldap-admin1")
            self.assertEqual(env["OCP_ADMIN_USER_PASSWORD"], "secret")

    def test_read_pytest_vault_env_merges_smoke_mount(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            smoke = Path(tmp) / "smoke"
            smoke.mkdir()
            (smoke / "OCP_ADMIN_USER_USERNAME").write_text("htpasswd-cluster-admin-user", encoding="utf-8")
            with mock.patch(
                "components.codeflare_sdk.auth._PYTEST_VAULT_MOUNTS",
                (smoke,),
            ):
                env = read_pytest_vault_env()
            self.assertEqual(env["OCP_ADMIN_USER_USERNAME"], "htpasswd-cluster-admin-user")

    def test_htpasswd_overlay_maps_ldap_test_user_to_admin(self) -> None:
        vault = {
            "TEST_USER_USERNAME": "ldap-admin1",
            "TEST_USER_PASSWORD": "ldap-pass",
            "OCP_ADMIN_USER_USERNAME": "htpasswd-cluster-admin-user",
            "OCP_ADMIN_USER_PASSWORD": "admin-pass",
        }
        with mock.patch("components.codeflare_sdk.auth._cluster_is_byoidc", return_value=False):
            with mock.patch("components.codeflare_sdk.auth.cluster_has_htpasswd_identity", return_value=True):
                overlay = codeflare_htpasswd_test_user_overlay(vault)
        self.assertEqual(
            overlay,
            {
                "TEST_USER_USERNAME": "htpasswd-cluster-admin-user",
                "TEST_USER_PASSWORD": "admin-pass",
                "TEST_USER_AUTH_TYPE": "htpasswd-cluster-admin",
                "CLUSTER_AUTH": "htpasswd-cluster-admin",
                "OCP_ADMIN_USER_USERNAME": "htpasswd-cluster-admin-user",
                "OCP_ADMIN_USER_PASSWORD": "admin-pass",
            },
        )

    def test_htpasswd_overlay_when_cluster_has_htpasswd_idp(self) -> None:
        vault = {
            "TEST_USER_USERNAME": "ldap-admin1",
            "OCP_ADMIN_USER_USERNAME": "htpasswd-cluster-admin-user",
            "OCP_ADMIN_USER_PASSWORD": "admin-pass",
        }
        with mock.patch("components.codeflare_sdk.auth._cluster_is_byoidc", return_value=True):
            with mock.patch("components.codeflare_sdk.auth.cluster_has_htpasswd_identity", return_value=True):
                overlay = codeflare_htpasswd_test_user_overlay(vault)
        self.assertEqual(overlay["TEST_USER_USERNAME"], "htpasswd-cluster-admin-user")

    def test_env_overrides_from_mount(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "TEST_USER_USERNAME").write_text("ldap-user1", encoding="utf-8")
            (root / "OCP_ADMIN_USER_USERNAME").write_text("htpasswd-cluster-admin-user", encoding="utf-8")
            (root / "OCP_ADMIN_USER_PASSWORD").write_text("pw", encoding="utf-8")
            with mock.patch("components.codeflare_sdk.auth._cluster_is_byoidc", return_value=False):
                with mock.patch("components.codeflare_sdk.auth.cluster_has_htpasswd_identity", return_value=True):
                    overlay = codeflare_env_overrides_from_vault(root)
            self.assertEqual(overlay["TEST_USER_USERNAME"], "htpasswd-cluster-admin-user")

    def test_skips_htpasswd_overlay_without_htpasswd_idp(self) -> None:
        vault = {
            "TEST_USER_USERNAME": "ldap-admin1",
            "OCP_ADMIN_USER_USERNAME": "ldap-admin1",
            "OCP_ADMIN_USER_PASSWORD": "admin-pass",
        }
        with mock.patch("components.codeflare_sdk.auth._cluster_is_byoidc", return_value=False):
            with mock.patch("components.codeflare_sdk.auth.cluster_has_htpasswd_identity", return_value=False):
                self.assertEqual(codeflare_htpasswd_test_user_overlay(vault), {})

    def test_vault_htpasswd_admin_applies_without_idp_probe(self) -> None:
        vault = {
            "TEST_USER_USERNAME": "ldap-admin1",
            "OCP_ADMIN_USER_PASSWORD": "pw",
            "OCP_ADMIN_USER_USERNAME": "htpasswd-cluster-admin-user",
        }
        with mock.patch("components.codeflare_sdk.auth.cluster_has_htpasswd_identity", return_value=False):
            overlay = codeflare_htpasswd_test_user_overlay(vault)
        self.assertEqual(overlay["TEST_USER_USERNAME"], "htpasswd-cluster-admin-user")

    def test_byoidc_env_overrides(self) -> None:
        with mock.patch("components.codeflare_sdk.auth._cluster_is_byoidc", return_value=True):
            with mock.patch(
                "components.codeflare_sdk.auth.codeflare_byoidc_test_user_overlay",
                return_value={
                    "CLUSTER_AUTH": "oidc",
                    "TEST_USER_USERNAME": "odh-user1",
                    "TEST_USER_PASSWORD": "secret",
                    "TEST_USER_AUTH_TYPE": "oidc",
                },
            ):
                overlay = codeflare_env_overrides_from_vault()
        self.assertEqual(overlay["CLUSTER_AUTH"], "oidc")
        self.assertEqual(overlay["TEST_USER_USERNAME"], "odh-user1")

    def test_byoidc_cluster_falls_back_to_htpasswd_overlay(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "TEST_USER_USERNAME").write_text("ldap-admin1", encoding="utf-8")
            (root / "OCP_ADMIN_USER_USERNAME").write_text("htpasswd-cluster-admin-user", encoding="utf-8")
            (root / "OCP_ADMIN_USER_PASSWORD").write_text("pw", encoding="utf-8")
            with mock.patch("components.codeflare_sdk.auth._cluster_is_byoidc", return_value=True):
                with mock.patch("components.codeflare_sdk.auth.codeflare_byoidc_test_user_overlay", return_value={}):
                    with mock.patch("components.codeflare_sdk.auth.codeflare_ephc_kubeconfig_overlay", return_value={}):
                        with mock.patch(
                            "components.codeflare_sdk.auth.cluster_has_htpasswd_identity",
                            return_value=True,
                        ):
                            overlay = codeflare_env_overrides_from_vault(root)
            self.assertEqual(overlay["TEST_USER_USERNAME"], "htpasswd-cluster-admin-user")

    def test_ephc_kubeconfig_overlay_when_no_idp(self) -> None:
        with mock.patch.dict("os.environ", {"CLUSTER_SOURCE": "EPHC", "OPENSHIFT_TOKEN": "tok123"}, clear=False):
            with mock.patch("components.codeflare_sdk.auth._cluster_is_byoidc", return_value=False):
                with mock.patch("components.codeflare_sdk.auth.cluster_has_htpasswd_identity", return_value=False):
                    overlay = codeflare_ephc_kubeconfig_overlay()
        self.assertEqual(overlay["CLUSTER_AUTH"], "openshift")
        self.assertEqual(overlay["OPENSHIFT_TOKEN"], "tok123")
        self.assertEqual(overlay["OC_TOKEN"], "tok123")
        self.assertEqual(overlay["OCP_ADMIN_USER_USERNAME"], "")

    def test_ephc_env_overrides_fall_back_to_token(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "OCP_ADMIN_USER_USERNAME").write_text("htpasswd-cluster-admin-user", encoding="utf-8")
            with mock.patch.dict("os.environ", {"CLUSTER_SOURCE": "EPHC", "OC_TOKEN": "tok456"}, clear=False):
                with mock.patch("components.codeflare_sdk.auth._cluster_is_byoidc", return_value=False):
                    with mock.patch("components.codeflare_sdk.auth.cluster_has_htpasswd_identity", return_value=False):
                        overlay = codeflare_env_overrides_from_vault(root)
        self.assertEqual(overlay["OC_TOKEN"], "tok456")
        self.assertEqual(overlay["CLUSTER_AUTH"], "openshift")

    def test_dashboard_url_overlay_from_staged_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            payload = Path(tmp) / "tests-payload"
            results = payload / "results"
            results.mkdir(parents=True)
            (payload / "odh-dashboard-url.txt").write_text(
                "https://rh-ai.apps.ods-qe-psi-07.osp.rh-ods.com\n",
                encoding="utf-8",
            )
            overlay = codeflare_dashboard_url_overlay(results)
        self.assertEqual(
            overlay,
            {
                "ODH_DASHBOARD_URL": "https://rh-ai.apps.ods-qe-psi-07.osp.rh-ods.com",
                "BASE_URL": "https://rh-ai.apps.ods-qe-psi-07.osp.rh-ods.com",
                "DASHBOARD_URL": "https://rh-ai.apps.ods-qe-psi-07.osp.rh-ods.com",
            },
        )

    def test_env_overrides_include_dashboard_url(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            payload = Path(tmp) / "tests-payload"
            results = payload / "results"
            results.mkdir(parents=True)
            (payload / "odh-dashboard-url.txt").write_text(
                "https://dash.example.com\n",
                encoding="utf-8",
            )
            with mock.patch("components.codeflare_sdk.auth._cluster_is_byoidc", return_value=False):
                with mock.patch("components.codeflare_sdk.auth.cluster_has_htpasswd_identity", return_value=False):
                    with mock.patch.dict("os.environ", {"CLUSTER_SOURCE": "EPHC", "OC_TOKEN": "tok"}, clear=False):
                        overlay = codeflare_env_overrides_from_vault(artifacts_dir=results)
        self.assertEqual(overlay["ODH_DASHBOARD_URL"], "https://dash.example.com")
        self.assertEqual(overlay["OC_TOKEN"], "tok")

