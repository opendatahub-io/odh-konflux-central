"""Tests for Jenkins createIDP parity (install/ldap.py)."""

from __future__ import annotations

import unittest
from unittest import mock

from install.ldap import install_identity_providers

class TestInstallIdentityProviders(unittest.TestCase):
    def test_cluster_is_byoidc_when_byoidc_credentials_secret(self) -> None:
        from install.ldap import _cluster_is_byoidc

        ok = mock.Mock(returncode=0)
        with mock.patch("install.ldap.oc_run", return_value=ok):
            self.assertTrue(_cluster_is_byoidc())

    def test_skips_when_ldap_idp_already_on_oauth(self) -> None:
        with (
            mock.patch("install.ldap._cluster_is_byoidc", return_value=False),
            mock.patch("install.ldap.cluster_has_ldap_identity", return_value=True),
            mock.patch("install.ldap._clone_ods_install") as clone,
        ):
            install_identity_providers()
        clone.assert_not_called()

    def test_skips_when_htpasswd_idp_on_cluster(self) -> None:
        with (
            mock.patch("install.ldap._cluster_is_byoidc", return_value=False),
            mock.patch("install.ldap.cluster_has_htpasswd_identity", return_value=True),
            mock.patch("install.ldap._clone_ods_install") as clone,
        ):
            install_identity_providers()
        clone.assert_not_called()

    def test_skips_ldap_install_on_rosa_hcp(self) -> None:
        with (
            mock.patch("install.ldap._cluster_is_byoidc", return_value=False),
            mock.patch("install.ldap.cluster_has_htpasswd_identity", return_value=False),
            mock.patch("install.ldap._cluster_is_rosa_hcp", return_value=True),
            mock.patch("install.ldap.cluster_has_ldap_identity", return_value=False),
            mock.patch("install.ldap._clone_ods_install") as clone,
        ):
            install_identity_providers()
        clone.assert_not_called()

    def test_skips_createidp_rerun_when_openldap_on_rosa_hcp(self) -> None:
        with (
            mock.patch("install.ldap._cluster_is_byoidc", return_value=False),
            mock.patch("install.ldap.cluster_has_htpasswd_identity", return_value=False),
            mock.patch("install.ldap._cluster_is_rosa_hcp", return_value=True),
            mock.patch("install.ldap.cluster_has_ldap_identity", return_value=False),
            mock.patch("install.ldap._openldap_secret_ready", return_value=True),
            mock.patch("install.ldap._clone_ods_install") as clone,
        ):
            install_identity_providers()
        clone.assert_not_called()

    def test_cluster_is_rosa_hcp_from_aws_platform_rosa_tag(self) -> None:
        from install.ldap import _cluster_is_rosa_hcp

        platform = mock.Mock(returncode=0, stdout="AWS")
        rosa_tag = mock.Mock(returncode=0, stdout="rosa")
        with mock.patch("install.ldap.oc_run", side_effect=[platform, rosa_tag]):
            self.assertTrue(_cluster_is_rosa_hcp())

    def test_skips_duplicate_install_when_already_attempted(self) -> None:
        import tempfile
        from pathlib import Path

        from steps.cluster_prep_state import mark_identity_providers_attempted

        with tempfile.TemporaryDirectory() as tmp:
            payload = Path(tmp) / "results"
            payload.mkdir()
            mark_identity_providers_attempted(payload)
            with (
                mock.patch(
                    "steps.cluster_prep_state.resolve_artifacts_dir",
                    return_value=payload,
                ),
                mock.patch("install.ldap._clone_ods_install") as clone,
            ):
                install_identity_providers()
            clone.assert_not_called()

    def test_reruns_odstest_when_openldap_secret_without_oauth_idp(self) -> None:
        with (
            mock.patch("install.ldap._cluster_is_byoidc", return_value=False),
            mock.patch("install.ldap.cluster_has_htpasswd_identity", return_value=False),
            mock.patch("install.ldap._cluster_is_rosa_hcp", return_value=False),
            mock.patch("install.ldap.cluster_has_ldap_identity", side_effect=[False, False]),
            mock.patch("install.ldap._openldap_secret_ready", return_value=True),
            mock.patch("install.ldap.shutil.which", return_value="/usr/bin/git"),
            mock.patch("install.ldap._clone_ods_install", return_value=mock.Mock()),
            mock.patch("install.ldap.subprocess.run", return_value=mock.Mock(returncode=0)),
            mock.patch("runners.orchestrator.stage_jq_for_prereqs"),
        ):
            install_identity_providers()

    def test_dashboard_cypress_triggers_idp_install(self) -> None:
        from runners.component_prereqs import prepare_component_for_smoke

        with (
            mock.patch("runners.component_prereqs.cluster_prep_already_done", return_value=False),
            mock.patch("runners.component_prereqs._ensure_dsc_managed_for_component"),
            mock.patch("runners.component_prereqs.smoke_enables_models_as_service", return_value=False),
            mock.patch("runners.component_prereqs.ensure_rhoai_gateway_stack_for_components"),
            mock.patch("runners.component_prereqs._wait_model_catalog_for_dashboard"),
            mock.patch("runners.component_prereqs.verify_dashboard_route_for_prepare"),
            mock.patch("runners.component_prereqs.install_identity_providers") as idp,
        ):
            prepare_component_for_smoke("dashboard_cypress")
        idp.assert_called_once()

class EnsureHtpasswdOpenldapSecretTest(unittest.TestCase):
    def test_stages_secret_on_rosa_hcp(self) -> None:
        from install.ldap import ensure_htpasswd_openldap_secret_for_unprivileged_tests

        manifest = mock.Mock(returncode=0, stdout="apiVersion: v1\nkind: Secret\n")
        apply_ok = mock.Mock(returncode=0, stdout="", stderr="")
        with (
            mock.patch("install.ldap._cluster_is_byoidc", return_value=False),
            mock.patch("install.ldap.cluster_has_ldap_identity", return_value=False),
            mock.patch("install.ldap._cluster_is_rosa_hcp", return_value=True),
            mock.patch("install.ldap.cluster_has_htpasswd_identity", return_value=False),
            mock.patch("install.ldap.oc_run", side_effect=[mock.Mock(returncode=0), manifest, apply_ok]) as oc,
        ):
            self.assertTrue(
                ensure_htpasswd_openldap_secret_for_unprivileged_tests(
                    "htpasswd-cluster-admin-user",
                    "admin-pass",
                )
            )
        self.assertEqual(oc.call_count, 3)

    def test_skips_when_ldap_idp_configured(self) -> None:
        from install.ldap import ensure_htpasswd_openldap_secret_for_unprivileged_tests

        with (
            mock.patch("install.ldap._cluster_is_byoidc", return_value=False),
            mock.patch("install.ldap.cluster_has_ldap_identity", return_value=True),
            mock.patch("install.ldap.oc_run") as oc,
        ):
            self.assertFalse(
                ensure_htpasswd_openldap_secret_for_unprivileged_tests("htpasswd-user1", "pw")
            )
        oc.assert_not_called()

