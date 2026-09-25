"""Fetch Vault shift-left/openshift secrets at runtime and stage env files for Tekton mounts."""

from __future__ import annotations

import json
import os
import ssl
import urllib.error
import urllib.request
from pathlib import Path
from typing import Callable, Mapping

from suite.errors import AppError

VAULT_APPROLE_SECRET = "vault-approle"
VAULT_AUTH_MOUNT = Path("/vault-approle")
SMOKE_AWS_MOUNT = Path("/smoke-aws-credentials")
COMPONENT_VAULT_MOUNT = Path("/component-vault-credentials")
TENANT_TEST_SECRETS_MOUNT = Path("/tenant-test-secrets")
SECRET_SOURCE_VAULT = "vault"
SECRET_SOURCE_TENANT = "tenant"
SHIFT_LEFT_KV_PATH = "apps/data/rhods-ci/shift-left"
OPENSHIFT_KV_PATH = "apps/data/rhods-ci/openshift"
APPROLE_LOGIN_PATH = "v1/auth/approle/login"

_AWS_KEYS = ("AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY", "AWS_SESSION_TOKEN")
_AWS_KEY_ALIASES: dict[str, tuple[str, ...]] = {
    "AWS_ACCESS_KEY_ID": ("aws_access_key_id", "awsAccessKeyId", "AWS_ACCESS_KEY"),
    "AWS_SECRET_ACCESS_KEY": ("aws_secret_access_key", "awsSecretAccessKey", "AWS_SECRET_KEY"),
    "AWS_SESSION_TOKEN": ("aws_session_token", "awsSessionToken"),
}

# Cloned Konflux Secret names → Vault KV blob keys on apps/rhods-ci/shift-left.
_TENANT_SECRET_TO_BLOB: dict[str, str] = {
    "envfile-mlflow": "envFileMlflow",
    "envfile-ogx": "envFileOGX",
    "envfile-pipelines": "envFilePipelines",
    "envfile-codeflare-sdk": "envFileCodeflareSdk",
    "envfile-dashboard-cypress": "volumeFileTestVariables",
    "shiftleft-envfile-model-serving": "envFileModelServing",
}

UrlOpen = Callable[..., object]


def resolve_secret_source(environ: Mapping[str, str] | None = None) -> str:
    """``vault`` (default) or ``tenant`` cloned Konflux Secrets."""
    env: Mapping[str, str] = os.environ if environ is None else environ
    raw = (env.get("SECRET_SOURCE") or "").strip().lower()
    if not raw:
        artifacts = Path((env.get("ARTIFACTS_DIR") or "").strip())
        cfg = artifacts.parent.parent / "run-config" / "SECRET_SOURCE"
        if artifacts.parts and cfg.is_file():
            raw = cfg.read_text(encoding="utf-8").strip().lower()
    if raw in (SECRET_SOURCE_TENANT, "konflux"):
        return SECRET_SOURCE_TENANT
    return SECRET_SOURCE_VAULT


def copy_tenant_secret_files(src: Path, dest: Path) -> list[str]:
    """Copy cloned tenant Secret files into the writable emptyDir mount."""
    if not src.is_dir():
        return []
    dest.mkdir(parents=True, exist_ok=True)
    written: list[str] = []
    for path in sorted(src.iterdir()):
        if not path.is_file() or path.name.startswith("."):
            continue
        (dest / path.name).write_bytes(path.read_bytes())
        written.append(path.name)
    return written


def parse_env_file_blob(blob: str) -> dict[str, str]:
    """Parse an envFile* blob (KEY=value / export KEY=value)."""
    out: dict[str, str] = {}
    for raw_line in (blob or "").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export ") :].strip()
        if "=" not in line:
            continue
        key, _, val = line.partition("=")
        key = key.strip()
        if not key or key.startswith("#"):
            continue
        out[key] = val.strip().strip("'").strip('"')
    return out


def shift_left_vault_blob_key(name: str) -> str:
    """Map a catalog/tenant secret name to the Vault shift-left KV blob key."""
    raw = (name or "").strip()
    if not raw:
        return ""
    if raw in _TENANT_SECRET_TO_BLOB:
        return _TENANT_SECRET_TO_BLOB[raw]
    if raw.startswith("envFile") or raw.startswith("volumeFile"):
        return raw
    return _TENANT_SECRET_TO_BLOB.get(raw.lower(), raw)


def merge_model_serving_env(shift_left: Mapping[str, str]) -> dict[str, str]:
    """envFileModelServing buckets + AWS from envFileCommon (else envFile-for-rhelaiteam)."""
    serving = parse_env_file_blob(shift_left.get("envFileModelServing") or "")
    common = parse_env_file_blob(shift_left.get("envFileCommon") or "")
    rhela = parse_env_file_blob(shift_left.get("envFile-for-rhelaiteam") or "")
    merged = dict(serving)
    aws_src = common if (common.get("AWS_ACCESS_KEY_ID") or "").strip() else rhela
    for key in _AWS_KEYS:
        val = (aws_src.get(key) or "").strip()
        if val:
            merged[key] = val
    return merged


def _write_files(dest: Path, values: Mapping[str, str]) -> list[str]:
    dest.mkdir(parents=True, exist_ok=True)
    written: list[str] = []
    for key, val in values.items():
        if not key or val is None:
            continue
        if "/" in key or key in (".", ".."):
            continue
        (dest / key).write_text(str(val).strip() + "\n", encoding="utf-8")
        written.append(key)
    return written


def stage_shift_left_files(
    shift_left: Mapping[str, str],
    dest: Path,
    *,
    blob_key: str = "",
    include_model_serving: bool = True,
) -> list[str]:
    """Write env files under dest. Never logs values."""
    written: list[str] = []
    if include_model_serving:
        written.extend(_write_files(dest, merge_model_serving_env(shift_left)))
    key = shift_left_vault_blob_key(blob_key) if blob_key else ""
    if key == "volumeFileTestVariables":
        yaml_blob = (shift_left.get("volumeFileTestVariables") or "").strip()
        if yaml_blob:
            dest.mkdir(parents=True, exist_ok=True)
            (dest / "test-variables.yml").write_text(yaml_blob + "\n", encoding="utf-8")
            written.append("test-variables.yml")
        return written
    if key and key != "envFileModelServing":
        blob = shift_left.get(key) or ""
        written.extend(_write_files(dest, parse_env_file_blob(blob)))
    return written


def _ssl_context(ca_path: Path) -> ssl.SSLContext:
    if not ca_path.is_file():
        raise AppError(f"Vault CA missing: {ca_path}", 1)
    return ssl.create_default_context(cafile=str(ca_path))


def _vault_login_token(
    *,
    vault_addr: str,
    role_id: str,
    secret_id: str,
    ca_path: Path,
    urlopen: UrlOpen | None = None,
) -> tuple[str, UrlOpen | None, ssl.SSLContext | None]:
    addr = (vault_addr or "").rstrip("/")
    if not addr or not (role_id or "").strip() or not (secret_id or "").strip():
        raise AppError("Vault AppRole address/role_id/secret_id incomplete", 1)
    opener = urlopen or urllib.request.urlopen
    ctx = None if urlopen is not None else _ssl_context(ca_path)
    login_body = json.dumps({"role_id": role_id.strip(), "secret_id": secret_id.strip()}).encode()
    login_req = urllib.request.Request(
        f"{addr}/v1/auth/approle/login",
        data=login_body,
        method="POST",
        headers={"Content-Type": "application/json"},
    )
    try:
        with opener(login_req, context=ctx, timeout=30) as resp:  # type: ignore[arg-type]
            login_raw = resp.read()
    except TypeError:
        with opener(login_req, timeout=30) as resp:  # type: ignore[misc]
            login_raw = resp.read()
    except urllib.error.URLError as exc:
        raise AppError(f"Vault AppRole login failed: {exc.reason}", 1) from exc
    login_doc = json.loads(login_raw.decode("utf-8"))
    token = str(((login_doc.get("auth") or {}).get("client_token")) or "").strip()
    if not token:
        errs = login_doc.get("errors")
        raise AppError(f"Vault AppRole login returned no token ({errs})", 1)
    return token, opener, ctx


def vault_login_and_read_kv_data(
    *,
    vault_addr: str,
    role_id: str,
    secret_id: str,
    ca_path: Path,
    kv_path: str,
    urlopen: UrlOpen | None = None,
) -> dict[str, str]:
    """AppRole login then KV v2 get. Does not log token or secret_id."""
    token, opener, ctx = _vault_login_token(
        vault_addr=vault_addr,
        role_id=role_id,
        secret_id=secret_id,
        ca_path=ca_path,
        urlopen=urlopen,
    )
    kv_req = urllib.request.Request(
        f"{vault_addr.rstrip('/')}/v1/{kv_path.lstrip('/')}",
        method="GET",
        headers={"X-Vault-Token": token},
    )
    try:
        with opener(kv_req, context=ctx, timeout=30) as resp:  # type: ignore[arg-type]
            kv_raw = resp.read()
    except TypeError:
        with opener(kv_req, timeout=30) as resp:  # type: ignore[misc]
            kv_raw = resp.read()
    except urllib.error.URLError as exc:
        raise AppError(f"Vault KV get failed for {kv_path}: {exc.reason}", 1) from exc
    kv_doc = json.loads(kv_raw.decode("utf-8"))
    data = (kv_doc.get("data") or {}).get("data")
    if not isinstance(data, dict):
        raise AppError(f"Vault KV payload missing data.data for {kv_path}", 1)
    out: dict[str, str] = {}
    for key, val in data.items():
        if isinstance(key, str) and isinstance(val, str):
            out[key] = val
    return out


def vault_login_and_read_shift_left(
    *,
    vault_addr: str,
    role_id: str,
    secret_id: str,
    ca_path: Path,
    urlopen: UrlOpen | None = None,
) -> dict[str, str]:
    """AppRole login then KV v2 get of shift-left. Does not log token or secret_id."""
    return vault_login_and_read_kv_data(
        vault_addr=vault_addr,
        role_id=role_id,
        secret_id=secret_id,
        ca_path=ca_path,
        kv_path=SHIFT_LEFT_KV_PATH,
        urlopen=urlopen,
    )


def _aws_credentials_from_mapping(values: Mapping[str, str]) -> dict[str, str]:
    flat = dict(values)
    for blob_key in ("envFileCommon", "envFile-for-rhelaiteam", "envFile"):
        blob = (flat.get(blob_key) or "").strip()
        if blob:
            flat.update(parse_env_file_blob(blob))
    out: dict[str, str] = {}
    for canonical, aliases in _AWS_KEY_ALIASES.items():
        val = (flat.get(canonical) or "").strip()
        if not val:
            for alias in aliases:
                val = (flat.get(alias) or "").strip()
                if val:
                    break
        if val:
            out[canonical] = val
    return out


def load_hcp_install_aws_credentials(
    *,
    auth_dir: Path = VAULT_AUTH_MOUNT,
    environ: Mapping[str, str] | None = None,
    urlopen: UrlOpen | None = None,
) -> dict[str, str]:
    """AWS keys for openshift-cli-installer S3 (apps/rhods-ci/openshift, then shift-left)."""
    env: Mapping[str, str] = os.environ if environ is None else environ
    existing = _aws_credentials_from_mapping(env)
    if existing.get("AWS_ACCESS_KEY_ID") and existing.get("AWS_SECRET_ACCESS_KEY"):
        return existing
    if not auth_dir.is_dir():
        return existing
    addr = _read_auth_file(auth_dir, "VAULT_ADDR")
    role_id = _read_auth_file(auth_dir, "role_id")
    secret_id = _read_auth_file(auth_dir, "secret_id")
    ca_path = auth_dir / "ca.crt"
    try:
        openshift = vault_login_and_read_kv_data(
            vault_addr=addr,
            role_id=role_id,
            secret_id=secret_id,
            ca_path=ca_path,
            kv_path=OPENSHIFT_KV_PATH,
            urlopen=urlopen,
        )
        creds = _aws_credentials_from_mapping(openshift)
        if creds.get("AWS_ACCESS_KEY_ID") and creds.get("AWS_SECRET_ACCESS_KEY"):
            return creds
    except AppError as exc:
        print(f"WARN: could not load openshift Vault AWS credentials: {exc}", flush=True)
    try:
        shift_left = vault_login_and_read_shift_left(
            vault_addr=addr,
            role_id=role_id,
            secret_id=secret_id,
            ca_path=ca_path,
            urlopen=urlopen,
        )
        creds = _aws_credentials_from_mapping(merge_model_serving_env(shift_left))
        if creds.get("AWS_ACCESS_KEY_ID") and creds.get("AWS_SECRET_ACCESS_KEY"):
            return creds
    except AppError as exc:
        print(f"WARN: could not load shift-left Vault AWS credentials: {exc}", flush=True)
    return existing


def _read_auth_file(auth_dir: Path, name: str) -> str:
    path = auth_dir / name
    if not path.is_file():
        return ""
    return path.read_text(encoding="utf-8").strip()


def _blob_key_for_component(component_id: str) -> str:
    cid = (component_id or "").strip()
    if not cid:
        return "envFileModelServing"
    try:
        from suite.component_catalog import (
            default_components_smoke_config_path,
            load_components_smoke_catalog,
        )

        catalog = load_components_smoke_catalog(default_components_smoke_config_path())
        comp = catalog.components.get(cid)
    except Exception:
        return "envFileModelServing"
    if comp is None:
        return "envFileModelServing"
    runner = comp.runner
    if runner is not None and (runner.vault_secret_key or "").strip():
        return shift_left_vault_blob_key(runner.vault_secret_key)
    if (comp.shift_left_env_secret or "").strip():
        return shift_left_vault_blob_key(comp.shift_left_env_secret)
    return "envFileModelServing"


def ensure_runtime_vault_env(
    *,
    auth_dir: Path = VAULT_AUTH_MOUNT,
    dest: Path | None = None,
    extra_dest: Path | None = None,
    component_id: str = "",
    urlopen: UrlOpen | None = None,
) -> bool:
    """Stage shift-left files when /vault-approle is present. Returns False if auth mount missing."""
    if not auth_dir.is_dir() and resolve_secret_source() != SECRET_SOURCE_TENANT:
        return False
    cid = component_id.strip() or os.environ.get("COMPONENT_TEST_COMPONENT_ID", "").strip()
    blob_key = _blob_key_for_component(cid)
    if dest is None and extra_dest is None:
        targets = [p for p in (SMOKE_AWS_MOUNT, COMPONENT_VAULT_MOUNT) if p.is_dir()]
        if not targets:
            targets = [SMOKE_AWS_MOUNT]
    else:
        targets = [p for p in (dest, extra_dest) if p is not None]
    if resolve_secret_source() == SECRET_SOURCE_TENANT:
        src = TENANT_TEST_SECRETS_MOUNT
        written: set[str] = set()
        for target in targets:
            try:
                written.update(copy_tenant_secret_files(src, target))
            except OSError as exc:
                print(f"WARN: cannot copy tenant secrets into {target}: {exc}", flush=True)
        print(f"✓ Tenant test secrets staged {len(written)} keys (SECRET_SOURCE=tenant)", flush=True)
        return bool(written)
    if not auth_dir.is_dir():
        return False
    addr = _read_auth_file(auth_dir, "VAULT_ADDR")
    role_id = _read_auth_file(auth_dir, "role_id")
    secret_id = _read_auth_file(auth_dir, "secret_id")
    ca_path = auth_dir / "ca.crt"
    data = vault_login_and_read_shift_left(
        vault_addr=addr,
        role_id=role_id,
        secret_id=secret_id,
        ca_path=ca_path,
        urlopen=urlopen,
    )
    seen: set[str] = set()
    for target in targets:
        try:
            written = stage_shift_left_files(
                data,
                target,
                blob_key=blob_key,
                include_model_serving=True,
            )
        except OSError as exc:
            print(f"WARN: cannot stage Vault shift-left into {target}: {exc}", flush=True)
            continue
        seen.update(written)
    print(f"✓ Vault shift-left staged {len(seen)} keys (blob={blob_key})", flush=True)
    return True
