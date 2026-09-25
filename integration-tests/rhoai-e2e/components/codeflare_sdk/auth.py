"""CodeFlare SDK vault auth overlays for external clusters (Jenkins parity)."""

from __future__ import annotations

import os
from pathlib import Path

from install.ldap import _cluster_is_byoidc, cluster_has_htpasswd_identity
from suite.its_trigger_params import CLUSTER_SOURCE_EPHC

_VAULT_MOUNT = Path("/component-vault-credentials")
_PYTEST_VAULT_MOUNTS: tuple[Path, ...] = (
    Path("/smoke-aws-credentials"),
    _VAULT_MOUNT,
)


def read_flat_vault_env(mount: Path | None = None) -> dict[str, str]:
    """Read Konflux flat secret keys mounted as individual files."""
    root = mount if mount is not None else _VAULT_MOUNT
    if not root.is_dir():
        return {}
    out: dict[str, str] = {}
    for path in root.iterdir():
        if not path.is_file():
            continue
        try:
            out[path.name] = path.read_text(encoding="utf-8").strip()
        except OSError:
            continue
    return out


def read_pytest_vault_env() -> dict[str, str]:
    """Merge flat vault keys from pytest task mounts (shift-left + component vault)."""
    out: dict[str, str] = {}
    for root in _PYTEST_VAULT_MOUNTS:
        for key, val in read_flat_vault_env(root).items():
            if val and key not in out:
                out[key] = val
    return out


def _ldap_style_test_user(username: str) -> bool:
    user = username.strip().lower()
    return user.startswith("ldap-") or user.startswith("ldap_")


def codeflare_byoidc_test_user_overlay() -> dict[str, str]:
    """On EPHC BYOIDC, use Keycloak users from ``oidc/byoidc-credentials`` (not htpasswd vault)."""
    from components.maas_billing.oidc_users import byoidc_cypress_test_user

    user = byoidc_cypress_test_user()
    if not user:
        return {}
    return {
        "CLUSTER_AUTH": "oidc",
        "TEST_USER_USERNAME": user["USERNAME"],
        "TEST_USER_PASSWORD": user["PASSWORD"],
        "TEST_USER_AUTH_TYPE": user.get("AUTH_TYPE", "oidc"),
    }


def codeflare_htpasswd_test_user_overlay(vault: dict[str, str]) -> dict[str, str]:
    """On htpasswd HCP, map LDAP vault TEST_USER to cluster-admin htpasswd creds."""
    test_user = vault.get("TEST_USER_USERNAME", "").strip()
    if not test_user or not _ldap_style_test_user(test_user):
        return {}
    admin_user = vault.get("OCP_ADMIN_USER_USERNAME", "").strip()
    admin_pass = vault.get("OCP_ADMIN_USER_PASSWORD", "").strip()
    if not admin_user or not admin_pass:
        from components.dashboard_cypress.auth_overlay import _htpasswd_test_user_from_env

        ht = _htpasswd_test_user_from_env()
        if not ht:
            return {}
        admin_user = ht["USERNAME"]
        admin_pass = ht["PASSWORD"]
    if not cluster_has_htpasswd_identity() and not admin_user.lower().startswith("htpasswd-"):
        return {}
    idp = "htpasswd-cluster-admin"
    return {
        "TEST_USER_USERNAME": admin_user,
        "TEST_USER_PASSWORD": admin_pass,
        "TEST_USER_AUTH_TYPE": idp,
        "CLUSTER_AUTH": idp,
        "OCP_ADMIN_USER_USERNAME": admin_user,
        "OCP_ADMIN_USER_PASSWORD": admin_pass,
    }


def codeflare_ephc_kubeconfig_overlay() -> dict[str, str]:
    """HyperShift EPHC blocks OAuth IdP patches; use materialized kubeconfig bearer token."""
    if os.environ.get("CLUSTER_SOURCE", "").strip() != CLUSTER_SOURCE_EPHC:
        return {}
    if _cluster_is_byoidc() or cluster_has_htpasswd_identity():
        return {}
    token = os.environ.get("OPENSHIFT_TOKEN") or os.environ.get("OC_TOKEN")
    if not token:
        return {}
    return {
        "CLUSTER_AUTH": "openshift",
        "OPENSHIFT_TOKEN": token,
        "OC_TOKEN": token,
        "OCP_ADMIN_USER_USERNAME": "",
        "OCP_ADMIN_USER_PASSWORD": "",
        "TEST_USER_USERNAME": "",
        "TEST_USER_PASSWORD": "",
    }


def _resolve_dashboard_url_for_codeflare(artifacts_dir: Path | None = None) -> str:
    """Resolve staged or live dashboard URL for codeflare-sdk run-tests.sh."""
    from components.dashboard_cypress.config import resolve_odh_dashboard_base_url
    from steps.tests_payload import resolve_tests_payload_root

    if artifacts_dir is not None:
        payload = resolve_tests_payload_root(artifacts_dir)
        url_file = payload / "odh-dashboard-url.txt"
        if url_file.is_file():
            url = url_file.read_text(encoding="utf-8").strip().rstrip("/")
            if url:
                return url
        staged_cfg = artifacts_dir / "dashboard-cypress-config.yml"
        if staged_cfg.is_file():
            for line in staged_cfg.read_text(encoding="utf-8").splitlines():
                if line.startswith("ODH_DASHBOARD_URL:"):
                    url = line.split(":", 1)[1].strip()
                    if url:
                        return url.rstrip("/")
    return (resolve_odh_dashboard_base_url() or "").strip().rstrip("/")


def codeflare_dashboard_url_overlay(artifacts_dir: Path | None = None) -> dict[str, str]:
    url = _resolve_dashboard_url_for_codeflare(artifacts_dir)
    if not url:
        return {}
    return {
        "ODH_DASHBOARD_URL": url,
        "BASE_URL": url,
        "DASHBOARD_URL": url,
    }


def codeflare_env_overrides_from_vault(
    mount: Path | None = None,
    *,
    artifacts_dir: Path | None = None,
) -> dict[str, str]:
    vault = read_flat_vault_env(mount)
    overlay: dict[str, str] = {}
    if _cluster_is_byoidc():
        overlay = codeflare_byoidc_test_user_overlay()
        if not overlay:
            overlay = codeflare_ephc_kubeconfig_overlay()
        if not overlay:
            overlay = codeflare_htpasswd_test_user_overlay(vault)
    else:
        overlay = codeflare_htpasswd_test_user_overlay(vault)
        if not overlay:
            overlay = codeflare_ephc_kubeconfig_overlay()
    dashboard = codeflare_dashboard_url_overlay(artifacts_dir)
    if dashboard:
        overlay = {**overlay, **dashboard}
    return overlay
