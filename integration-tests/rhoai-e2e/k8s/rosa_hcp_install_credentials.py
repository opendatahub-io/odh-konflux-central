"""ROSA HCP install-data fallback: rosa-admin password from openshift-cli-installer S3 zip."""

from __future__ import annotations

import io
import os
import zipfile
from pathlib import Path

from install.kubeconfig_cluster_label import cluster_name_from_url
from k8s.external_credentials import ExternalClusterCredentials
from k8s.vault_runtime import VAULT_AUTH_MOUNT, load_hcp_install_aws_credentials
from steps.tekton_util import _kubeconfig_api_server

DEFAULT_S3_BUCKET = "hcp-clusters-mdata"
DEFAULT_S3_PREFIX = "openshift-cli-installer/"
ROSA_ADMIN_USER = "rosa-admin"
ZIP_PASSWORD_ENTRY = "auth/rosa-admin-password"
ZIP_KUBECONFIG_CANDIDATES: tuple[str, ...] = (
    "auth/kubeconfig",
    "auth/kubeconfig-admin",
    "auth/kubeconfig-admin-internal",
)


def _env(name: str, default: str = "") -> str:
    return (os.environ.get(name) or default).strip()


def resolve_install_cluster_name(bootstrap_path: Path) -> str:
    """Map bootstrap kubeconfig API URL to openshift-cli-installer zip basename."""
    override = _env("ROSA_HCP_INSTALL_CLUSTER_NAME")
    if override:
        return override
    if not bootstrap_path.is_file():
        return ""
    server = _kubeconfig_api_server(bootstrap_path)
    if not server:
        return ""
    return cluster_name_from_url(server)


def _s3_object_uri(bucket: str, key: str) -> str:
    return f"s3://{bucket.rstrip('/')}/{key.lstrip('/')}"


def _install_zip_s3_key(cluster_name: str) -> str:
    prefix = _env("ROSA_HCP_INSTALL_S3_PREFIX", DEFAULT_S3_PREFIX)
    return f"{prefix.rstrip('/')}/{cluster_name}.zip"


def _extract_zip_member(zip_bytes: bytes, member_path: str) -> str:
    try:
        with zipfile.ZipFile(io.BytesIO(zip_bytes)) as archive:
            raw = archive.read(member_path)
        return raw.decode("utf-8").strip()
    except (KeyError, UnicodeDecodeError, zipfile.BadZipFile):
        return ""


def api_server_from_install_zip(zip_bytes: bytes) -> str:
    """Read API URL from openshift-cli-installer zip (auth/kubeconfig)."""
    if not zip_bytes:
        return ""
    import tempfile

    from steps.tekton_util import _kubeconfig_api_server

    for member in ZIP_KUBECONFIG_CANDIDATES:
        text = _extract_zip_member(zip_bytes, member)
        if not text:
            continue
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", suffix=".kubeconfig", delete=False) as tmp:
            tmp.write(text)
            tmp.flush()
            path = Path(tmp.name)
        try:
            server = _kubeconfig_api_server(path)
            if server:
                return server
        finally:
            path.unlink(missing_ok=True)
    return ""


def download_install_zip_bytes(*, cluster_name: str, auth_dir: Path = VAULT_AUTH_MOUNT) -> bytes:
    """Fetch openshift-cli-installer/{cluster}.zip using Vault openshift AWS credentials."""
    name = (cluster_name or "").strip()
    if not name:
        return b""
    aws_env = load_hcp_install_aws_credentials(auth_dir=auth_dir)
    if not aws_env.get("AWS_ACCESS_KEY_ID") or not aws_env.get("AWS_SECRET_ACCESS_KEY"):
        print("WARN: AWS credentials unavailable for ROSA HCP install-data S3", flush=True)
        return b""
    bucket = _env("ROSA_HCP_INSTALL_S3_BUCKET", DEFAULT_S3_BUCKET)
    key = _install_zip_s3_key(name)
    return _s3_download_bytes(bucket, key, aws_env)


def _pip_tools_target() -> Path:
    override = _env("ROSA_HCP_PIP_TARGET")
    if override:
        return Path(override)
    tests_shared = _env("TESTS_SHARED")
    if tests_shared:
        return Path(tests_shared) / ".pip-tools"
    return Path("/credentials/.pip-tools")


def _ensure_boto3() -> None:
    try:
        import boto3  # noqa: F401
        return
    except ImportError:
        pass
    from helpers.pip_bootstrap import pip_install_to_target, prepend_pythonpath

    target = _pip_tools_target()
    print(f"Installing boto3 to {target} (ROSA HCP install-data S3)...", flush=True)
    pip_install_to_target("boto3", target)
    prepend_pythonpath(str(target))


def _s3_download_bytes(bucket: str, key: str, aws_env: dict[str, str]) -> bytes:
    access_key = aws_env.get("AWS_ACCESS_KEY_ID", "").strip()
    secret_key = aws_env.get("AWS_SECRET_ACCESS_KEY", "").strip()
    if not access_key or not secret_key:
        return b""
    region = (
        aws_env.get("AWS_DEFAULT_REGION", "").strip()
        or _env("AWS_DEFAULT_REGION", "us-east-1")
    )
    try:
        _ensure_boto3()
        import boto3

        client_kwargs: dict[str, str] = {
            "region_name": region,
            "aws_access_key_id": access_key,
            "aws_secret_access_key": secret_key,
        }
        session_token = aws_env.get("AWS_SESSION_TOKEN", "").strip()
        if session_token:
            client_kwargs["aws_session_token"] = session_token
        client = boto3.client("s3", **client_kwargs)
        response = client.get_object(Bucket=bucket, Key=key)
        body = response.get("Body")
        return body.read() if body is not None else b""
    except Exception as exc:
        print(f"WARN: boto3 S3 download failed for {_s3_object_uri(bucket, key)}: {exc}", flush=True)
        return b""


def load_rosa_admin_credentials_from_install_zip(
    *,
    bootstrap_path: Path | None = None,
    cluster_name: str = "",
    auth_dir: Path = VAULT_AUTH_MOUNT,
) -> ExternalClusterCredentials | None:
    """Return rosa-admin credentials from openshift-cli-installer S3 zip when available."""
    name = (cluster_name or "").strip()
    if not name and bootstrap_path is not None and bootstrap_path.is_file():
        name = resolve_install_cluster_name(bootstrap_path)
    if not name:
        return None

    api_server = ""
    if bootstrap_path is not None and bootstrap_path.is_file():
        api_server = _kubeconfig_api_server(bootstrap_path)

    zip_bytes = download_install_zip_bytes(cluster_name=name, auth_dir=auth_dir)
    if not zip_bytes:
        return None

    if not api_server:
        api_server = api_server_from_install_zip(zip_bytes)
    if not api_server:
        print(
            f"WARN: could not resolve API server from install zip for cluster {name!r}",
            flush=True,
        )
        return None

    password = _extract_zip_member(zip_bytes, ZIP_PASSWORD_ENTRY)
    if not password:
        bucket = _env("ROSA_HCP_INSTALL_S3_BUCKET", DEFAULT_S3_BUCKET)
        key = _install_zip_s3_key(name)
        print(
            f"WARN: {ZIP_PASSWORD_ENTRY!r} missing in {_s3_object_uri(bucket, key)}",
            flush=True,
        )
        return None

    bucket = _env("ROSA_HCP_INSTALL_S3_BUCKET", DEFAULT_S3_BUCKET)
    key = _install_zip_s3_key(name)
    print(
        f"Loaded rosa-admin password from {_s3_object_uri(bucket, key)} "
        f"(cluster={name})",
        flush=True,
    )
    return ExternalClusterCredentials(
        username=ROSA_ADMIN_USER,
        password=password,
        api_server=api_server,
    )
