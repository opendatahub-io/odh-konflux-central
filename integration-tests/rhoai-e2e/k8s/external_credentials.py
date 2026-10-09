"""External cluster login: Vault + openshift-cli-installer S3 (default), optional Konflux htpasswd."""

from __future__ import annotations

import base64
import json
import os
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping

from k8s.oc_util import filter_warning_lines, run_cmd
from suite.errors import AppError

_KUBECONFIG_SECRET_PREFIX = "rhoai-e2e-kubeconfig-"
_CREDENTIALS_SECRET_PREFIX = "rhoai-e2e-external-"
_CREDENTIALS_SECRET_SUFFIX = "-credentials"
EXTERNAL_LOGIN_SOURCE_VAULT = "vault"
EXTERNAL_LOGIN_SOURCE_TENANT = "tenant"


def resolve_external_login_source(environ: Mapping[str, str] | None = None) -> str:
    """``vault`` (S3 install zip via AppRole, default) or ``tenant`` (legacy Konflux htpasswd first)."""
    env = os.environ if environ is None else environ
    raw = (env.get("EXTERNAL_LOGIN_SOURCE") or "").strip().lower()
    if raw in (EXTERNAL_LOGIN_SOURCE_TENANT, "konflux"):
        return EXTERNAL_LOGIN_SOURCE_TENANT
    return EXTERNAL_LOGIN_SOURCE_VAULT


def _external_credentials_secret_override(credentials_secret_override: str = "") -> str:
    return (credentials_secret_override or os.environ.get("EXTERNAL_CREDENTIALS_SECRET") or "").strip()


def _tenant_htpasswd_login_enabled(credentials_secret_override: str = "") -> bool:
    if resolve_external_login_source() == EXTERNAL_LOGIN_SOURCE_TENANT:
        return True
    return bool(_external_credentials_secret_override(credentials_secret_override))


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
    """Map ``rhoai-e2e-kubeconfig-rh-nightly-pm`` → ``rhoai-e2e-external-rh-nightly-pm-credentials``."""
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


def companion_kubeconfig_secret_for_install_cluster(cluster_name: str) -> str:
    """Tenant Secret ``rhoai-e2e-kubeconfig-{cluster}`` (optional bootstrap for S3 install-data runs)."""
    name = (cluster_name or "").strip()
    if not name:
        return ""
    return f"{_KUBECONFIG_SECRET_PREFIX}{name}"


def _s3_install_htpasswd_secret_candidates(
    cluster_name: str,
    *,
    override: str = "",
) -> tuple[str, ...]:
    explicit = (override or "").strip()
    ordered: list[str] = []
    if explicit:
        ordered.append(explicit)
    cluster = (cluster_name or "").strip()
    if cluster:
        ordered.append(f"{_CREDENTIALS_SECRET_PREFIX}{cluster}{_CREDENTIALS_SECRET_SUFFIX}")
    seen: set[str] = set()
    out: list[str] = []
    for name in ordered:
        if name not in seen:
            seen.add(name)
            out.append(name)
    return tuple(out)


def load_kubeconfig_from_tenant_secret(*, namespace: str, secret_name: str) -> str:
    """Return kubeconfig YAML from a tenant Secret key ``kubeconfig``, or empty when absent."""
    ns = (namespace or "").strip()
    name = (secret_name or "").strip()
    if not ns or not name:
        return ""
    proc = run_cmd(
        [
            "oc",
            "get",
            "secret",
            name,
            "-n",
            ns,
            "-o",
            "jsonpath={.data.kubeconfig}",
        ],
        capture=True,
        check=False,
        env=_konflux_tenant_oc_env(),
    )
    if proc.returncode != 0 or not (proc.stdout or "").strip():
        return ""
    try:
        return base64.b64decode(str(proc.stdout).strip()).decode("utf-8")
    except (ValueError, UnicodeDecodeError):
        return ""


def _writable_companion_bootstrap_path(bootstrap_path: Path) -> Path:
    """Tekton mounts ``/credentials/bootstrap`` read-only; stage companion kubeconfig under ``/credentials``."""
    parent = bootstrap_path.parent
    try:
        if parent.is_dir() and os.access(parent, os.W_OK):
            return bootstrap_path
    except OSError:
        pass
    root = bootstrap_path.parent.parent if parent.name == "bootstrap" else parent
    return root / ".companion-bootstrap" / bootstrap_path.name


def _ensure_bootstrap_from_companion_kubeconfig_secret(
    *,
    namespace: str,
    cluster_name: str,
    bootstrap_path: Path,
) -> Path:
    if bootstrap_path.is_file():
        return bootstrap_path
    secret = companion_kubeconfig_secret_for_install_cluster(cluster_name)
    if not secret:
        return bootstrap_path
    text = load_kubeconfig_from_tenant_secret(namespace=namespace, secret_name=secret)
    if not text.strip():
        return bootstrap_path
    target = _writable_companion_bootstrap_path(bootstrap_path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(text, encoding="utf-8")
    target.chmod(0o600)
    return target


def try_refresh_from_companion_kubeconfig_secret(
    *,
    namespace: str,
    cluster_name: str,
    work_path: Path,
) -> tuple[bool, str]:
    """Use an existing ``rhoai-e2e-kubeconfig-{cluster}`` Secret when the token is still valid."""
    from k8s.external_kubeconfig import verify_external_cluster_login

    secret = companion_kubeconfig_secret_for_install_cluster(cluster_name)
    if not secret:
        return False, ""
    text = load_kubeconfig_from_tenant_secret(namespace=namespace, secret_name=secret)
    if not text.strip():
        return False, ""
    work_path.parent.mkdir(parents=True, exist_ok=True)
    work_path.write_text(text, encoding="utf-8")
    work_path.chmod(0o600)
    try:
        verify_external_cluster_login(work_path)
    except AppError:
        return False, ""
    return True, f"companion tenant kubeconfig Secret {secret!r}"


def _rosa_admin_credentials_from_install_zip(
    *,
    bootstrap_path: Path,
    cluster_name: str = "",
) -> tuple[ExternalClusterCredentials | None, str]:
    from k8s.rosa_hcp_install_credentials import load_rosa_admin_credentials_from_install_zip

    rosa = load_rosa_admin_credentials_from_install_zip(
        bootstrap_path=bootstrap_path if bootstrap_path.is_file() else None,
        cluster_name=cluster_name,
    )
    if rosa:
        return rosa, "ROSA HCP install-data S3 (rosa-admin)"
    return None, ""


def _tenant_htpasswd_credentials(
    *,
    namespace: str,
    secret_names: tuple[str, ...],
) -> tuple[ExternalClusterCredentials | None, str]:
    for creds_secret in secret_names:
        if not creds_secret:
            continue
        creds = load_external_cluster_credentials(namespace=namespace, secret_name=creds_secret)
        if creds:
            return creds, f"tenant Secret {creds_secret!r}"
    return None, ""


def resolve_external_cluster_credentials(
    *,
    namespace: str,
    cluster_source: str,
    bootstrap_path: Path,
    credentials_secret_override: str = "",
) -> tuple[ExternalClusterCredentials | None, str]:
    """Vault/S3 rosa-admin first (default); Konflux htpasswd when EXTERNAL_LOGIN_SOURCE=tenant or override set."""
    from suite.its_trigger_params import is_s3_install_cluster_source, s3_install_cluster_name

    override = _external_credentials_secret_override(credentials_secret_override)
    cluster_name = ""
    bootstrap = bootstrap_path
    if is_s3_install_cluster_source(cluster_source):
        cluster_name = s3_install_cluster_name(cluster_source)
        bootstrap = _ensure_bootstrap_from_companion_kubeconfig_secret(
            namespace=namespace,
            cluster_name=cluster_name,
            bootstrap_path=bootstrap_path,
        )

    def s3_creds() -> tuple[ExternalClusterCredentials | None, str]:
        return _rosa_admin_credentials_from_install_zip(
            bootstrap_path=bootstrap,
            cluster_name=cluster_name,
        )

    def tenant_creds() -> tuple[ExternalClusterCredentials | None, str]:
        if not _tenant_htpasswd_login_enabled(credentials_secret_override):
            return None, ""
        if is_s3_install_cluster_source(cluster_source):
            names = _s3_install_htpasswd_secret_candidates(cluster_name, override=override)
        else:
            mapped = external_credentials_secret_name(cluster_source, override=override)
            names = (mapped,) if mapped else ()
        return _tenant_htpasswd_credentials(namespace=namespace, secret_names=names)

    if resolve_external_login_source() == EXTERNAL_LOGIN_SOURCE_TENANT:
        creds, source = tenant_creds()
        if creds:
            return creds, source
        return s3_creds()

    creds, source = s3_creds()
    if creds:
        return creds, source
    return tenant_creds()


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

    Assumes ``rhoai-e2e-kubeconfig-*`` Secrets contain only the ``kubeconfig`` key today.
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
    """Login via Vault/S3 (default) or Konflux htpasswd when opted in; return (used, source label)."""
    from steps.tekton_util import ensure_kubeconfig_bearer_token, materialize_htpasswd_kubeconfig_login
    from suite.its_trigger_params import is_s3_install_cluster_source, s3_install_cluster_name

    if (
        is_s3_install_cluster_source(cluster_source)
        and resolve_external_login_source() == EXTERNAL_LOGIN_SOURCE_TENANT
    ):
        cluster = s3_install_cluster_name(cluster_source)
        used, source = try_refresh_from_companion_kubeconfig_secret(
            namespace=namespace,
            cluster_name=cluster,
            work_path=work_path,
        )
        if used:
            return True, source

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
