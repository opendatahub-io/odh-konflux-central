"""Konflux tenant Secrets holding durable htpasswd credentials for external clusters (RHOAIENG-57718)."""

from __future__ import annotations

import base64
import json
import os
import shutil
from dataclasses import dataclass
from pathlib import Path

from k8s.oc_util import filter_warning_lines, run_cmd
from suite.errors import AppError

_KUBECONFIG_SECRET_PREFIX = "olminstall-kubeconfig-"
_CREDENTIALS_SECRET_PREFIX = "olminstall-external-"
_CREDENTIALS_SECRET_SUFFIX = "-credentials"


def _konflux_tenant_oc_env() -> dict[str, str]:
    """Oc env for Konflux tenant API calls (ignore external KUBECONFIG from pipeline steps)."""
    env = dict(os.environ)
    env.pop("KUBECONFIG", None)
    return env


@dataclass(frozen=True)
class ExternalClusterCredentials:
    username: str
    password: str
    api_server: str


def external_credentials_secret_name(
    cluster_source: str,
    *,
    override: str = "",
) -> str:
    """Map ``olminstall-kubeconfig-rh-nightly-pm`` → ``olminstall-external-rh-nightly-pm-credentials``."""
    explicit = (override or "").strip()
    if explicit:
        return explicit
    name = (cluster_source or "").strip()
    if not name.startswith(_KUBECONFIG_SECRET_PREFIX):
        return ""
    suffix = name[len(_KUBECONFIG_SECRET_PREFIX) :]
    if not suffix:
        return ""
    return f"{_CREDENTIALS_SECRET_PREFIX}{suffix}{_CREDENTIALS_SECRET_SUFFIX}"


def resolve_external_cluster_credentials(
    *,
    namespace: str,
    cluster_source: str,
    bootstrap_path: Path,
    credentials_secret_override: str = "",
) -> tuple[ExternalClusterCredentials | None, str]:
    """Tenant htpasswd Secret first, then ROSA HCP install-data S3 (rosa-admin)."""
    creds_secret = external_credentials_secret_name(
        cluster_source,
        override=credentials_secret_override,
    )
    if creds_secret:
        creds = load_external_cluster_credentials(namespace=namespace, secret_name=creds_secret)
        if creds:
            return creds, f"tenant Secret {creds_secret!r}"

    from k8s.rosa_hcp_install_credentials import load_rosa_admin_credentials_from_install_zip

    rosa = load_rosa_admin_credentials_from_install_zip(bootstrap_path=bootstrap_path)
    if rosa:
        return rosa, "ROSA HCP install-data S3 (rosa-admin)"
    return None, ""


def load_external_cluster_credentials(
    *,
    namespace: str,
    secret_name: str,
) -> ExternalClusterCredentials | None:
    """Return htpasswd credentials from a tenant Secret, or ``None`` when absent."""
    ns = (namespace or "").strip()
    name = (secret_name or "").strip()
    if not ns or not name:
        return None
    proc = run_cmd(
        [
            "oc",
            "get",
            "secret",
            name,
            "-n",
            ns,
            "-o",
            "json",
        ],
        capture=True,
        check=False,
        env=_konflux_tenant_oc_env(),
    )
    if proc.returncode != 0:
        return None
    try:
        doc = json.loads(proc.stdout or "{}")
    except json.JSONDecodeError:
        return None
    if not isinstance(doc, dict):
        return None
    data = doc.get("data")
    if not isinstance(data, dict):
        return None

    def _decode(key: str) -> str:
        raw = data.get(key)
        if not raw:
            return ""
        try:
            return base64.b64decode(str(raw)).decode("utf-8").strip()
        except (ValueError, UnicodeDecodeError):
            return ""

    username = _decode("HTPASSWD_USER")
    password = _decode("HTPASSWD_PASS")
    api_server = _decode("API_SERVER")
    if not username or not password or not api_server:
        return None
    return ExternalClusterCredentials(
        username=username,
        password=password,
        api_server=api_server,
    )


def write_minimal_kubeconfig(
    *,
    path: Path,
    api_server: str,
    ca_data_b64: str = "",
) -> None:
    import yaml

    cluster: dict[str, object] = {"server": api_server.strip()}
    if ca_data_b64.strip():
        cluster["certificate-authority-data"] = ca_data_b64.strip()
    else:
        cluster["insecure-skip-tls-verify"] = True
    doc = {
        "apiVersion": "v1",
        "kind": "Config",
        "clusters": [{"name": "external", "cluster": cluster}],
        "contexts": [{"name": "external", "context": {"cluster": "external", "user": "external"}}],
        "current-context": "external",
        "users": [{"name": "external", "user": {}}],
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump(doc, default_flow_style=False), encoding="utf-8")
    path.chmod(0o600)


def seed_working_kubeconfig(
    *,
    work_path: Path,
    bootstrap_path: Path,
    api_server: str,
) -> None:
    from steps.tekton_util import _kubeconfig_api_server, _kubeconfig_cluster_ca_data

    work_path.parent.mkdir(parents=True, exist_ok=True)
    if bootstrap_path.is_file():
        shutil.copy2(bootstrap_path, work_path)
        work_path.chmod(0o600)
        if api_server.strip() and _kubeconfig_api_server(work_path) != api_server.strip():
            write_minimal_kubeconfig(
                path=work_path,
                api_server=api_server,
                ca_data_b64=_kubeconfig_cluster_ca_data(bootstrap_path),
            )
        return
    write_minimal_kubeconfig(path=work_path, api_server=api_server)


def update_external_kubeconfig_secret(
    *,
    namespace: str,
    secret_name: str,
    kubeconfig_path: str,
) -> None:
    """Replace ``data.kubeconfig`` on an existing tenant Secret (in-pipeline refresh write-back).

    Assumes ``olminstall-kubeconfig-*`` Secrets contain only the ``kubeconfig`` key today.
    ``oc apply`` replaces the Secret ``data`` map; add a patch-based merge if extra keys are introduced.
    """
    ns = (namespace or "").strip()
    name = (secret_name or "").strip()
    path = (kubeconfig_path or "").strip()
    if not ns or not name or not path:
        raise AppError("namespace, secret name, and kubeconfig path are required to update Secret", 1)
    proc = run_cmd(
        [
            "oc",
            "create",
            "secret",
            "generic",
            name,
            f"--from-file=kubeconfig={path}",
            "-n",
            ns,
            "--dry-run=client",
            "-o",
            "yaml",
        ],
        capture=True,
        check=True,
        env=_konflux_tenant_oc_env(),
    )
    apply = run_cmd(
        ["oc", "apply", "-n", ns, "-f", "-"],
        capture=True,
        check=False,
        input_text=proc.stdout,
        env=_konflux_tenant_oc_env(),
    )
    filtered = filter_warning_lines(f"{apply.stdout}\n{apply.stderr}")
    if filtered.strip():
        print(filtered)
    if apply.returncode != 0:
        raise AppError(f"Failed to update external kubeconfig Secret {name!r} in {ns}", 1)
    print(f"Updated external kubeconfig Secret {name!r} in {ns}")


def refresh_working_kubeconfig_from_credentials(
    *,
    namespace: str,
    cluster_source: str,
    bootstrap_path: Path,
    work_path: Path,
    credentials_secret_override: str = "",
) -> tuple[bool, str]:
    """Login with tenant or ROSA HCP S3 credentials; return (used, source label)."""
    from steps.tekton_util import ensure_kubeconfig_bearer_token, materialize_htpasswd_kubeconfig_login

    creds, source = resolve_external_cluster_credentials(
        namespace=namespace,
        cluster_source=cluster_source,
        bootstrap_path=bootstrap_path,
        credentials_secret_override=credentials_secret_override,
    )
    if not creds:
        return False, ""

    seed_working_kubeconfig(
        work_path=work_path,
        bootstrap_path=bootstrap_path,
        api_server=creds.api_server,
    )
    env = {**os.environ, "KUBECONFIG": str(work_path), "CLUSTER_SOURCE": cluster_source}
    if not materialize_htpasswd_kubeconfig_login(creds.username, creds.password, environ=env):
        raise AppError(f"oc login failed using {source or 'external cluster credentials'}", 1)
    active = Path(env.get("KUBECONFIG", str(work_path)))
    ensure_kubeconfig_bearer_token(env)
    active = Path(env.get("KUBECONFIG", str(active)))
    if active != work_path and active.is_file():
        shutil.copy2(active, work_path)
        work_path.chmod(0o600)
    return True, source
