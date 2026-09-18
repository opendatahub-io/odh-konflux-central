"""Bulk-delete leaked pooled-cluster tenant namespaces before rhoai-e2e cleanup.sh."""

from __future__ import annotations

import json
import os
import re
import sys

from install.dsc_install import oc_run
from install.dependency_operators import unblock_terminating_namespace

# MaaS e2e tenants and Kueue smoke namespaces left on pooled clusters; cleanup.sh
# otherwise deletes their CSVs one-by-one in the safety-net phase.
_LEAKED_TENANT_NS_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"^ai-tenant-e2e-aigw-[0-9a-f]+$"),
    re.compile(r"^test-kueue-managed-[a-z0-9]+$"),
)


def _cleanup_enabled() -> bool:
    raw = os.environ.get("CLEANUP_LEAKED_TENANT_NS", "true").strip().lower()
    return raw not in ("0", "false", "no", "off")


def _matches_leaked_tenant_namespace(name: str) -> bool:
    return any(pattern.match(name) for pattern in _LEAKED_TENANT_NS_PATTERNS)


def _list_leaked_tenant_namespaces() -> list[str]:
    listed = oc_run(
        ["get", "namespace", "-o", "json"],
        check=False,
        capture_output=True,
        timeout=60,
    )
    if listed.returncode != 0:
        err = (listed.stderr or listed.stdout or "").strip()
        raise RuntimeError(f"Could not list namespaces for leaked-tenant cleanup: {err or 'unknown error'}")
    try:
        doc = json.loads(listed.stdout or "{}")
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"Could not parse namespace list JSON: {exc}") from exc
    names: list[str] = []
    for item in doc.get("items") or []:
        if not isinstance(item, dict):
            continue
        name = str((item.get("metadata") or {}).get("name") or "")
        if _matches_leaked_tenant_namespace(name):
            names.append(name)
    return sorted(names)


def _namespace_phase(name: str) -> str | None:
    listed = oc_run(
        ["get", "namespace", name, "-o", "jsonpath={.status.phase}"],
        check=False,
        capture_output=True,
        timeout=30,
    )
    if listed.returncode != 0:
        return None
    phase = (listed.stdout or "").strip()
    return phase or None


def cleanup_leaked_tenant_namespaces() -> None:
    """Delete leaked tenant namespaces before cleanup.sh to avoid per-CSV safety-net sweeps."""
    if not _cleanup_enabled():
        print(
            "NOTE: skipping leaked tenant namespace cleanup (CLEANUP_LEAKED_TENANT_NS=0)",
            flush=True,
        )
        return
    names = _list_leaked_tenant_namespaces()
    if not names:
        print("✓ No leaked tenant namespaces matched pre-cleanup patterns", flush=True)
        return
    preview = ", ".join(names[:5])
    if len(names) > 5:
        preview = f"{preview}, ..."
    print(
        f"Deleting {len(names)} leaked tenant namespace(s) before rhoai-e2e cleanup.sh ({preview})",
        flush=True,
    )
    deleted = oc_run(
        ["delete", "namespace", *names, "--ignore-not-found", "--wait=false"],
        check=False,
        capture_output=True,
        timeout=180,
    )
    if deleted.returncode != 0:
        err = (deleted.stderr or deleted.stdout or "").strip()
        raise RuntimeError(
            f"Bulk delete of leaked tenant namespaces failed (exit {deleted.returncode}): "
            f"{err or 'unknown error'}"
        )
    for name in names:
        if _namespace_phase(name) != "Terminating":
            continue
        try:
            unblock_terminating_namespace(name)
        except Exception as exc:
            print(
                f"WARN: could not unblock Terminating namespace {name}: {exc}",
                file=sys.stderr,
                flush=True,
            )
    print(f"✓ Initiated delete for {len(names)} leaked tenant namespace(s)", flush=True)
