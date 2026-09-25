"""Bulk-delete leaked pooled-cluster tenant namespaces before rhoai-e2e cleanup.sh."""

from __future__ import annotations

import os

from install.e2e_leaked_namespace_patterns import matches_leaked_tenant_namespace
from install.e2e_namespace_bulk_delete import bulk_delete_namespaces, list_matching_namespace_names


def _cleanup_enabled() -> bool:
    raw = os.environ.get("CLEANUP_LEAKED_TENANT_NS", "true").strip().lower()
    return raw not in ("0", "false", "no", "off")


def cleanup_leaked_tenant_namespaces() -> None:
    """Delete leaked tenant namespaces before cleanup.sh to avoid per-CSV safety-net sweeps."""
    if not _cleanup_enabled():
        print(
            "NOTE: skipping leaked tenant namespace cleanup (CLEANUP_LEAKED_TENANT_NS=0)",
            flush=True,
        )
        return
    names = list_matching_namespace_names(matches_leaked_tenant_namespace)
    bulk_delete_namespaces(
        names,
        log_label="leaked tenant",
        empty_message="✓ No leaked tenant namespaces matched pre-cleanup patterns",
    )


def _matches_leaked_tenant_namespace(name: str) -> bool:
    """Backward-compatible alias for unit tests."""
    return matches_leaked_tenant_namespace(name)
