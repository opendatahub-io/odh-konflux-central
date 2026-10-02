"""Bulk-delete leaked component pytest/ginkgo namespaces before rhoai-e2e cleanup.sh."""

from __future__ import annotations

import os

from install.e2e_leaked_namespace_patterns import matches_leaked_component_test_namespace
from install.e2e_namespace_bulk_delete import bulk_delete_namespaces, list_matching_namespace_names


def _cleanup_enabled() -> bool:
    raw = os.environ.get("CLEANUP_LEAKED_COMPONENT_NS", "true").strip().lower()
    return raw not in ("0", "false", "no", "off")


def cleanup_leaked_component_test_namespaces() -> None:
    """Delete opendatahub-tests / harness namespaces left on pooled external clusters."""
    if not _cleanup_enabled():
        print(
            "NOTE: skipping leaked component test namespace cleanup (CLEANUP_LEAKED_COMPONENT_NS=0)",
            flush=True,
        )
        return
    names = list_matching_namespace_names(matches_leaked_component_test_namespace)
    bulk_delete_namespaces(
        names,
        log_label="leaked component test",
        empty_message="✓ No leaked component test namespaces matched pre-cleanup patterns",
    )
