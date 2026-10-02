"""Load Jenkins shift-left env vars from a mounted tenant Secret (Vault key parity)."""

from __future__ import annotations

import os
from pathlib import Path
from typing import MutableMapping

# Jenkins Vault rhods-ci/shift-left → envFileModelServing (see VaultSecrets.SHIFT_LEFT).
SHIFT_LEFT_ENVFILE_MODEL_SERVING_KEYS: tuple[str, ...] = (
    "CI_S3_BUCKET_NAME",
    "CI_S3_BUCKET_REGION",
    "CI_S3_BUCKET_ENDPOINT",
    "MODELS_S3_BUCKET_NAME",
    "MODELS_S3_BUCKET_REGION",
    "MODELS_S3_BUCKET_ENDPOINT",
    "PYTEST_JIRA_TOKEN",
    "PYTEST_JIRA_URL",
    "PYTEST_JIRA_EMAIL",
    "PYTEST_JIRA_USERNAME",
)

# Older rhoai-e2e tenant secrets used AWS_* aliases; still accepted when staging/copying.
LEGACY_SMOKE_AWS_KEYS: tuple[str, ...] = (
    "AWS_ACCESS_KEY_ID",
    "AWS_SECRET_ACCESS_KEY",
    "AWS_DEFAULT_REGION",
    "AWS_S3_BUCKET",
    "AWS_S3_ENDPOINT",
    "AWS_CA_BUNDLE",
)

STAGED_SMOKE_SECRET_KEYS: tuple[str, ...] = SHIFT_LEFT_ENVFILE_MODEL_SERVING_KEYS + LEGACY_SMOKE_AWS_KEYS

_SHIFT_LEFT_PREFIXES = ("CI_S3_", "MODELS_S3_", "PYTEST_JIRA_", "AWS_")


def is_stageable_smoke_secret_key(key: str) -> bool:
    """True if a Secret data key should be copied into the Konflux tenant for smoke."""
    name = (key or "").strip()
    if not name:
        return False
    if name in STAGED_SMOKE_SECRET_KEYS:
        return True
    return any(name.startswith(prefix) for prefix in _SHIFT_LEFT_PREFIXES)


_TRUST_BUNDLE_PATH = Path("/tmp/olminstall-trust-bundle.pem")

_SYSTEM_CA_BUNDLE_CANDIDATES: tuple[Path, ...] = (
    Path("/etc/ssl/certs/ca-certificates.crt"),
    Path("/etc/pki/tls/certs/ca-bundle.crt"),
    Path("/etc/ssl/ca-bundle.pem"),
)


_CLUSTER_ROUTER_CA_PATH = Path("/tmp/olminstall-cluster-router-ca.pem")


def apply_cluster_router_ca_from_kubeconfig(
    environ: MutableMapping[str, str] | None = None,
) -> bool:
    """Prefer ingress router CA from the cluster under test (EPHC) over a stale tenant Secret."""
    from .smoke_trusted_ca import fetch_ingress_router_ca_pem

    env: MutableMapping[str, str] = os.environ if environ is None else environ
    kc = env.get("KUBECONFIG", "").strip()
    if not kc:
        return False
    kc_path = Path(kc)
    if not kc_path.is_file():
        return False
    pem = fetch_ingress_router_ca_pem(kc_path)
    if not pem:
        return False
    _CLUSTER_ROUTER_CA_PATH.write_text(pem + "\n", encoding="utf-8")
    bundle_path = _materialize_trust_bundle(_CLUSTER_ROUTER_CA_PATH)
    env["AWS_CA_BUNDLE"] = bundle_path
    for env_key in ("REQUESTS_CA_BUNDLE", "CURL_CA_BUNDLE", "SSL_CERT_FILE"):
        env[env_key] = bundle_path
    env.pop("SSL_CERT_DIR", None)
    _normalize_ssl_cert_env(env)
    return True


def suppress_ephemeral_jira_env(environ: MutableMapping[str, str] | None = None) -> None:
    """Drop Jenkins Jira-proxy env vars when no sidecar listens on localhost (Konflux/EPHC).

    Shift-left secrets often set ``PYTEST_JIRA_URL=http://localhost:2990/jira``; without the
    proxy, pytest collection blocks ~100s on connection refused (tr274 / 7nxdt). When unset,
    opendatahub-tests defaults to the same localhost URL — force-disable for Tekton runs.
    """
    env: MutableMapping[str, str] = os.environ if environ is None else environ
    url = env.get("PYTEST_JIRA_URL", "").strip().lower()
    disable = (not url) or "localhost" in url or "127.0.0.1" in url
    if not disable:
        return
    for key in (
        "PYTEST_JIRA_URL",
        "PYTEST_JIRA_TOKEN",
        "PYTEST_JIRA_EMAIL",
        "PYTEST_JIRA_USERNAME",
    ):
        env.pop(key, None)
    env["PYTEST_JIRA_URL"] = ""
    env["PYTEST_JIRA_DISABLE"] = "1"
    print("✓ Disabled PYTEST_JIRA for Tekton (no Jenkins Jira proxy)", flush=True)


def load_shift_left_env_from_mount(
    mount_path: Path | str = "/smoke-aws-credentials",
    environ: MutableMapping[str, str] | None = None,
) -> None:
    """Export mounted Secret keys into the process env (Jenkins withEnv parity).

    Each Kubernetes Secret key becomes a file under ``mount_path``; file basename is the
    variable name and file contents are the value (same as Vault envFile* lines).

    Legacy ``AWS_S3_*`` keys are mapped to ``CI_S3_*`` when the latter are unset.
    ``AWS_CA_BUNDLE`` is exposed as the mount file path for OpenSSL/boto (not PEM inline).
    """
    base = Path(mount_path)
    env: MutableMapping[str, str] = os.environ if environ is None else environ
    if not base.is_dir():
        return

    for path in sorted(base.iterdir()):
        if not path.is_file() or path.name.startswith(".") or path.name == "AWS_CA_BUNDLE":
            continue
        key = path.name
        if env.get(key, "").strip():
            continue
        env[key] = path.read_text(encoding="utf-8").strip()

    _apply_ca_bundle_side_effects(env, base)
    _apply_legacy_aws_to_ci_s3_mapping(env)
    _normalize_ssl_cert_env(env)


def _normalize_ssl_cert_env(env: MutableMapping[str, str]) -> None:
    """Tekton may set SSL_CERT_DIR to colon-separated paths; rustls treats that as one path."""
    cert_dir = env.get("SSL_CERT_DIR", "").strip()
    if not cert_dir:
        return
    if ":" not in cert_dir:
        return
    for candidate in cert_dir.split(":"):
        part = candidate.strip()
        if part and Path(part).is_dir():
            env["SSL_CERT_DIR"] = part
            return
    env.pop("SSL_CERT_DIR", None)


def _apply_ca_bundle_side_effects(env: MutableMapping[str, str], base: Path) -> None:
    ca_path = base / "AWS_CA_BUNDLE"
    if not ca_path.is_file():
        return
    bundle_path = _materialize_trust_bundle(ca_path)
    if not env.get("AWS_CA_BUNDLE", "").strip():
        env["AWS_CA_BUNDLE"] = bundle_path
    for env_key in ("REQUESTS_CA_BUNDLE", "CURL_CA_BUNDLE", "SSL_CERT_FILE"):
        if not env.get(env_key, "").strip():
            env[env_key] = bundle_path
    env.pop("SSL_CERT_DIR", None)


def _materialize_trust_bundle(router_ca_path: Path) -> str:
    """Merge system CA bundle (when present) with ingress/router PEM for rustls/openssl-probe."""
    parts: list[str] = []
    for candidate in _SYSTEM_CA_BUNDLE_CANDIDATES:
        if candidate.is_file():
            text = candidate.read_text(encoding="utf-8").strip()
            if text:
                parts.append(text)
            break
    router_pem = router_ca_path.read_text(encoding="utf-8").strip()
    if router_pem:
        parts.append(router_pem)
    if not parts:
        return str(router_ca_path)
    merged = "\n".join(parts) + "\n"
    _TRUST_BUNDLE_PATH.write_text(merged, encoding="utf-8")
    return str(_TRUST_BUNDLE_PATH)


def _apply_legacy_aws_to_ci_s3_mapping(env: MutableMapping[str, str]) -> None:
    """Map pre-shift-left AWS_* secret keys into CI_S3_* names opendatahub-tests expect."""
    if not env.get("CI_S3_BUCKET_NAME", "").strip():
        bucket = env.get("AWS_S3_BUCKET", "").strip()
        if bucket:
            env["CI_S3_BUCKET_NAME"] = bucket
    if not env.get("CI_S3_BUCKET_ENDPOINT", "").strip():
        endpoint = env.get("AWS_S3_ENDPOINT", "").strip()
        if endpoint:
            env["CI_S3_BUCKET_ENDPOINT"] = endpoint
    if not env.get("CI_S3_BUCKET_REGION", "").strip():
        region = env.get("AWS_DEFAULT_REGION", "").strip()
        if region:
            env["CI_S3_BUCKET_REGION"] = region


def resolve_ci_s3_smoke_fields(
    environ: MutableMapping[str, str] | None = None,
) -> dict[str, str] | None:
    """Return normalized S3 smoke creds for Cypress AWS_PIPELINES overlays."""
    source = dict(os.environ if environ is None else environ)
    _apply_legacy_aws_to_ci_s3_mapping(source)
    access_key = source.get("AWS_ACCESS_KEY_ID", "").strip()
    secret_key = source.get("AWS_SECRET_ACCESS_KEY", "").strip()
    bucket_name = (
        source.get("CI_S3_BUCKET_NAME", "").strip()
        or source.get("MODELS_S3_BUCKET_NAME", "").strip()
    )
    region = (
        source.get("CI_S3_BUCKET_REGION", "").strip()
        or source.get("MODELS_S3_BUCKET_REGION", "").strip()
    )
    endpoint = (
        source.get("CI_S3_BUCKET_ENDPOINT", "").strip()
        or source.get("MODELS_S3_BUCKET_ENDPOINT", "").strip()
    )
    if not all((access_key, secret_key, bucket_name, region, endpoint)):
        return None
    return {
        "AWS_ACCESS_KEY_ID": access_key,
        "AWS_SECRET_ACCESS_KEY": secret_key,
        "NAME": bucket_name,
        "REGION": region,
        "ENDPOINT": endpoint,
    }


_AWS_ENV_ALIASES: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("AWS_ACCESS_KEY_ID", ("aws-access-key-id", "AWS_ACCESS_KEY", "S3_ACCESS_KEY_ID")),
    ("AWS_SECRET_ACCESS_KEY", ("aws-secret-access-key", "AWS_SECRET_KEY", "S3_SECRET_ACCESS_KEY")),
)


def promote_shift_left_aws_env(
    mount_path: Path | str = "/smoke-aws-credentials",
    environ: MutableMapping[str, str] | None = None,
) -> None:
    """Ensure boto/pytest see AWS_* after shift-left mount (file keys or env aliases)."""
    env: MutableMapping[str, str] = os.environ if environ is None else environ
    base = Path(mount_path)
    load_shift_left_env_from_mount(base, env)
    for canonical, aliases in _AWS_ENV_ALIASES:
        if env.get(canonical, "").strip():
            continue
        for alias in aliases:
            val = env.get(alias, "").strip()
            if not val and base.is_dir():
                path = base / alias
                if path.is_file():
                    val = path.read_text(encoding="utf-8").strip()
            if val:
                env[canonical] = val
                break
