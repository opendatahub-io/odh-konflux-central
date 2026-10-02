"""Identity provider install (Jenkins createIDP parity)."""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

from _bootstrap import ensure_rhoai_e2e_path

ensure_rhoai_e2e_path()

from suite.constants import (
    DEFAULT_ODS_INSTALL_REPO_REVISION,
    DEFAULT_ODS_INSTALL_REPO_URL,
)
from install.dsc_install import oc_run
from steps.tekton_util import git_clone

_OPENLDAP_NS = "openldap"
_OPENLDAP_SECRET = "openldap"
_ODS_INSTALL_DEST = Path("/tmp/ods-install-components-prereqs")


def _openldap_secret_ready() -> bool:
    r = oc_run(
        ["get", "secret", _OPENLDAP_SECRET, "-n", _OPENLDAP_NS],
        check=False,
        capture_output=True,
        timeout=30,
    )
    return r.returncode == 0


def _clone_ods_install() -> Path:
    existing = os.environ.get("ODS_INSTALL_DIR", "").strip()
    if existing:
        path = Path(existing)
        if (path / "odstest").is_file():
            return path
        raise FileNotFoundError(f"ODS_INSTALL_DIR={existing!r} has no odstest script")

    dest = _ODS_INSTALL_DEST
    if dest.exists():
        shutil.rmtree(dest)
    url = os.environ.get("ODS_INSTALL_REPO_URL", "").strip() or DEFAULT_ODS_INSTALL_REPO_URL
    rev = os.environ.get("ODS_INSTALL_REPO_REVISION", "").strip() or DEFAULT_ODS_INSTALL_REPO_REVISION
    print(f"Cloning ods-install for identity providers ({url} @ {rev})...", flush=True)
    git_clone(url, rev, dest, tls_workaround=True)
    return dest


def _byoidc_credentials_ready() -> bool:
    """EPHC BYOIDC clusters expose test users via ``oidc/byoidc-credentials`` (g7j2k)."""
    r = oc_run(
        ["get", "secret", "byoidc-credentials", "-n", "oidc"],
        check=False,
        capture_output=True,
        timeout=30,
    )
    return r.returncode == 0


def _cluster_is_byoidc() -> bool:
    if _byoidc_credentials_ready():
        return True
    r = oc_run(
        ["get", "authentication", "cluster", "-o", "jsonpath={.spec.oidcProviders[0].name}"],
        check=False,
        capture_output=True,
        timeout=30,
    )
    if r.returncode != 0:
        return False
    name = (r.stdout or "").strip().lower()
    return bool(name) and "oidc" in name


def install_identity_providers() -> None:
    """Run ``odstest --install-identity-providers`` (Jenkins createIDP on self-managed/external)."""
    from steps.cluster_prep_state import (
        identity_providers_already_attempted,
        mark_identity_providers_attempted,
    )

    if identity_providers_already_attempted():
        print(
            "Skipping duplicate identity provider install (already attempted this PipelineRun)",
            flush=True,
        )
        return
    try:
        _install_identity_providers_body()
    finally:
        mark_identity_providers_attempted()


def _install_identity_providers_body() -> None:
    if _cluster_is_byoidc():
        print("✓ BYOIDC cluster — skipping LDAP/htpasswd install (Jenkins Create IDP skipped)", flush=True)
        return
    if cluster_has_htpasswd_identity():
        print(
            "✓ htpasswd identity provider on cluster — skipping LDAP install "
            "(ROSA HCP blocks OAuth/LDAP patches)",
            flush=True,
        )
        return
    if _cluster_is_rosa_hcp() and not cluster_has_ldap_identity():
        print(
            "✓ ROSA HCP — skipping LDAP OAuth install "
            "(HostedCluster policy blocks identityProvider patches; use htpasswd pytest overlay)",
            flush=True,
        )
        return
    if cluster_has_ldap_identity():
        print("✓ LDAP identity provider already configured on cluster login", flush=True)
        return
    if _openldap_secret_ready() and _cluster_is_rosa_hcp():
        print(
            f"✓ {_OPENLDAP_NS}/{_OPENLDAP_SECRET} present on ROSA HCP — skipping createIDP rerun "
            "(OAuth identityProvider patches blocked; use htpasswd pytest overlay)",
            flush=True,
        )
        return
    if _openldap_secret_ready():
        print(
            f"NOTICE: {_OPENLDAP_NS}/{_OPENLDAP_SECRET} present but LDAP not on OAuth — "
            "re-running Jenkins createIDP (odstest --install-identity-providers)",
            flush=True,
        )
    if not shutil.which("git"):
        raise RuntimeError(
            "git not available in this step image; identity providers must be installed "
            "in opendatahub-tests-prepare (konflux-test image) before component pytest"
        )
    from runners.orchestrator import stage_jq_for_prereqs

    stage_jq_for_prereqs()
    ods_dir = _clone_ods_install()
    cmd = ["sh", "odstest", "--install-identity-providers"]
    print(f"Running identity provider install in {ods_dir}...", flush=True)
    proc = subprocess.run(
        cmd,
        cwd=ods_dir,
        env=os.environ.copy(),
        check=False,
    )
    if proc.returncode != 0:
        raise RuntimeError(f"odstest --install-identity-providers failed (exit {proc.returncode})")
    if not _openldap_secret_ready():
        raise RuntimeError(
            f"openldap secret not found in {_OPENLDAP_NS} after identity provider install"
        )
    if cluster_has_ldap_identity():
        print(
            f"✓ Identity providers ready ({_OPENLDAP_NS}/{_OPENLDAP_SECRET} + LDAP on cluster login)",
            flush=True,
        )
        return
    print(
        f"✓ openldap secret ready ({_OPENLDAP_NS}/{_OPENLDAP_SECRET}); "
        "WARN: LDAP not registered on cluster OAuth (common on ROSA HCP) — "
        "Cypress may still use htpasswd HCP skipTags",
        flush=True,
    )


def ensure_htpasswd_openldap_secret_for_unprivileged_tests(
    username: str,
    password: str,
) -> bool:
    """Stage ``openldap/openldap`` for odh-tests ``unprivileged_client`` on htpasswd HCP.

    opendatahub-tests reads users/passwords from this secret (not TEST_USER_* env).
    """
    user = (username or "").strip()
    pwd = (password or "").strip()
    if not user or not pwd or "user" not in user.lower():
        return False
    if _cluster_is_byoidc() or cluster_has_ldap_identity():
        return False
    if not (_cluster_is_rosa_hcp() or cluster_has_htpasswd_identity()):
        return False

    oc_run(["create", "namespace", _OPENLDAP_NS], check=False, capture_output=True, timeout=30)
    manifest = oc_run(
        [
            "create",
            "secret",
            "generic",
            _OPENLDAP_SECRET,
            "-n",
            _OPENLDAP_NS,
            f"--from-literal=users={user}",
            f"--from-literal=passwords={pwd}",
            "--dry-run=client",
            "-o",
            "yaml",
        ],
        check=False,
        capture_output=True,
        timeout=30,
    )
    if manifest.returncode != 0:
        print(
            f"WARN: could not render {_OPENLDAP_NS}/{_OPENLDAP_SECRET} for htpasswd pytest: "
            f"{(manifest.stderr or manifest.stdout or '').strip()}",
            flush=True,
        )
        return False
    apply = oc_run(
        ["apply", "-f", "-"],
        stdin_text=manifest.stdout,
        check=False,
        capture_output=True,
        timeout=30,
    )
    if apply.returncode != 0:
        print(
            f"WARN: could not apply {_OPENLDAP_NS}/{_OPENLDAP_SECRET} for htpasswd pytest: "
            f"{(apply.stderr or apply.stdout or '').strip()}",
            flush=True,
        )
        return False
    print(
        f"✓ Staged {_OPENLDAP_NS}/{_OPENLDAP_SECRET} for htpasswd unprivileged pytest ({user})",
        flush=True,
    )
    return True


def cluster_has_ldap_identity() -> bool:
    """True when LDAP is a configured cluster login IdP (not only the openldap install secret)."""
    if not _openldap_secret_ready():
        return False
    names: list[str] = []
    for resource in ("oauth", "authentication"):
        r = oc_run(
            [
                "get",
                resource,
                "cluster",
                "-o",
                "jsonpath={.spec.identityProviders[*].name}",
            ],
            check=False,
            capture_output=True,
            timeout=30,
        )
        if r.returncode == 0:
            names.extend((r.stdout or "").split())
    idp_blob = " ".join(names).lower()
    return "ldap" in idp_blob or "openldap" in idp_blob


def _cluster_is_rosa_hcp() -> bool:
    """True on ROSA hosted control plane clusters (OAuth patches blocked by VAP)."""
    r = oc_run(
        [
            "get",
            "infrastructure",
            "cluster",
            "-o",
            "jsonpath={.status.platformStatus.type}",
        ],
        check=False,
        capture_output=True,
        timeout=30,
    )
    if r.returncode == 0 and "rosa" in (r.stdout or "").strip().lower():
        return True
    for tag_key in ("red-hat-clustertype", "api.openshift.com/name"):
        r = oc_run(
            [
                "get",
                "infrastructure",
                "cluster",
                "-o",
                f"jsonpath={{.status.platformStatus.aws.resourceTags[?(@.key=='{tag_key}')].value}}",
            ],
            check=False,
            capture_output=True,
            timeout=30,
        )
        if r.returncode == 0 and "rosa" in (r.stdout or "").strip().lower():
            return True
    return False


def cluster_has_htpasswd_identity() -> bool:
    """True when htpasswd is a configured cluster login IdP."""
    names: list[str] = []
    for resource in ("oauth", "authentication"):
        r = oc_run(
            [
                "get",
                resource,
                "cluster",
                "-o",
                "jsonpath={.spec.identityProviders[*].name}",
            ],
            check=False,
            capture_output=True,
            timeout=30,
        )
        if r.returncode == 0:
            names.extend((r.stdout or "").split())
    idp_blob = " ".join(names).lower()
    return "htpasswd" in idp_blob
