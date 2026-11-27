"""Resolve quay.io/rhoai pull credentials: Vault by default, tenant mount as fallback."""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from k8s.vault_runtime import (SECRET_SOURCE_TENANT, SECRET_SOURCE_VAULT,
                               VAULT_AUTH_MOUNT,
                               fetch_quay_dockerconfig_from_vault,
                               resolve_secret_source)

DEFAULT_QUAY_MOUNT_PATH = "/var/secret/quay/.dockerconfigjson"
DEFAULT_ROSA_HCP_QUAY_MOUNT = "/var/secret/rosa-hcp-kyverno/.dockerconfigjson"


def resolve_quay_pull_secret_source(environ: dict[str, str] | None = None) -> str:
    """``vault`` (default) or ``tenant``; override with ``QUAY_PULL_SECRET_SOURCE``."""
    env = dict(os.environ) if environ is None else environ
    raw = (env.get("QUAY_PULL_SECRET_SOURCE") or "").strip().lower()
    if raw in (SECRET_SOURCE_VAULT, SECRET_SOURCE_TENANT):
        return raw
    return resolve_secret_source(env)


def _dockerconfig_from_file(path: Path) -> dict[str, Any] | None:
    if not path.is_file():
        return None
    try:
        parsed = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(parsed, dict):
        return None
    if isinstance(parsed.get("auths"), dict):
        return parsed
    if parsed:
        return {"auths": parsed}
    return None


def _allow_stale_tenant_quay(env: dict[str, str]) -> bool:
    return (env.get("QUAY_PULL_ALLOW_TENANT_FALLBACK") or "").strip().lower() in (
        "1",
        "true",
        "yes",
    )


def _kyverno_mount_candidates(env: dict[str, str]) -> list[Path]:
    """Rosa HCP Kyverno dockerconfig mounts (may be synced separately from rhoai-external-quay-secret)."""
    paths: list[str] = []
    for key in ("ROSA_HCP_QUAY_AUTH_PATH", "ROSA_HCP_KYVERNO_POLICY_DIR"):
        val = (env.get(key) or "").strip()
        if not val:
            continue
        if key == "ROSA_HCP_KYVERNO_POLICY_DIR":
            paths.append(str(Path(val) / ".dockerconfigjson"))
        else:
            paths.append(val)
    paths.extend([DEFAULT_ROSA_HCP_QUAY_MOUNT, "/var/secret/rosa-hcp-kyverno/.dockerconfigjson"])
    seen: set[str] = set()
    out: list[Path] = []
    for raw in paths:
        raw = (raw or "").strip()
        if not raw or raw in seen:
            continue
        seen.add(raw)
        out.append(Path(raw))
    return out


def _quay_tenant_mount_candidates(env: dict[str, str]) -> list[Path]:
    paths: list[str] = []
    val = (env.get("QUAY_PULL_SECRET_PATH") or "").strip()
    if val:
        paths.append(val)
    paths.extend([DEFAULT_QUAY_MOUNT_PATH])
    seen: set[str] = set()
    out: list[Path] = []
    for raw in paths:
        raw = (raw or "").strip()
        if not raw or raw in seen:
            continue
        seen.add(raw)
        out.append(Path(raw))
    return out


def _tenant_mount_candidates(env: dict[str, str]) -> list[Path]:
    paths: list[Path] = []
    paths.extend(_quay_tenant_mount_candidates(env))
    paths.extend(_kyverno_mount_candidates(env))
    seen: set[str] = set()
    out: list[Path] = []
    for path in paths:
        key = str(path)
        if key in seen:
            continue
        seen.add(key)
        out.append(path)
    return out


def load_quay_pull_dockerconfig(
    *,
    auth_dir: Path = VAULT_AUTH_MOUNT,
    environ: dict[str, str] | None = None,
) -> dict[str, Any] | None:
    """Vault-first (default) quay dockerconfig for catalog/FBC/OLM pulls."""
    env = dict(os.environ) if environ is None else environ
    source = resolve_quay_pull_secret_source(env)

    def from_vault() -> dict[str, Any] | None:
        return fetch_quay_dockerconfig_from_vault(auth_dir=auth_dir)

    def from_paths(paths: list[Path]) -> dict[str, Any] | None:
        for path in paths:
            doc = _dockerconfig_from_file(path)
            if doc:
                print(f"✓ Quay dockerconfig from mount {path}", flush=True)
                return doc
        return None

    if source == SECRET_SOURCE_TENANT:
        doc = from_paths(_tenant_mount_candidates(env))
        if doc:
            return doc
        return from_vault()

    doc = from_vault()
    if doc:
        return doc
    doc = from_paths(_kyverno_mount_candidates(env))
    if doc:
        print(
            "WARN: using Rosa HCP Kyverno mount for quay auth (Vault unavailable; "
            "refresh vault-approle for rotation-friendly creds)",
            flush=True,
        )
        return doc
    if _allow_stale_tenant_quay(env):
        doc = from_paths(_quay_tenant_mount_candidates(env))
        if doc:
            print(
                "WARN: using tenant-mounted quay auth (QUAY_PULL_ALLOW_TENANT_FALLBACK; "
                "may be stale vs Vault)",
                flush=True,
            )
        return doc
    print(
        "ERROR: Vault quay auth unavailable; tenant quay fallback disabled "
        "(set QUAY_PULL_ALLOW_TENANT_FALLBACK=1 to use rhoai-external-quay-secret)",
        flush=True,
    )
    return None


def stage_registry_auth_file(
    dest: Path | None = None,
    *,
    quay_path: str = "",
    environ: dict[str, str] | None = None,
) -> str:
    """Write resolved dockerconfig for skopeo/opm; returns path or empty when missing.

    The ``quay_path`` / ``QUAY_PULL_SECRET_PATH`` file is only a *tenant* source, so
    it is honoured only when the tenant source was selected explicitly
    (``SECRET_SOURCE=tenant`` / ``QUAY_PULL_SECRET_SOURCE=tenant``) or the operator
    opted into the stale-tenant fallback (``QUAY_PULL_ALLOW_TENANT_FALLBACK=1``).
    Reading it unconditionally bypassed the Vault-first policy and could stage
    stale credentials that ``load_quay_pull_dockerconfig`` had just refused.
    """
    env = dict(os.environ) if environ is None else environ
    doc = load_quay_pull_dockerconfig(environ=env)
    if doc is None:
        tenant_source = resolve_quay_pull_secret_source(env) == SECRET_SOURCE_TENANT
        if tenant_source or _allow_stale_tenant_quay(env):
            fallback = Path(
                (quay_path or env.get("QUAY_PULL_SECRET_PATH") or DEFAULT_QUAY_MOUNT_PATH).strip()
            )
            if fallback.is_file():
                doc = _dockerconfig_from_file(fallback)
                if doc is not None:
                    print(
                        f"WARN: staging tenant quay auth from {fallback} "
                        "(tenant source opted in; may be stale vs Vault)",
                        flush=True,
                    )
        if doc is None:
            print(
                f"WARN: quay dockerconfig missing (Vault + tenant mounts); "
                f"private FBC pulls may fail",
                flush=True,
            )
            return ""
    target = dest or Path("/tmp/rhoai-quay-auth.json")
    target.write_text(json.dumps(doc, separators=(",", ":")), encoding="utf-8")
    os.environ["REGISTRY_AUTH_FILE"] = str(target)
    print(f"REGISTRY_AUTH_FILE={target}", flush=True)
    return str(target)
