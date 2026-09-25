"""Shared bulk namespace delete helpers for pooled-cluster E2E cleanup."""

from __future__ import annotations

import json
import sys
from collections.abc import Callable

from install.dsc_install import oc_run
from install.dependency_operators import unblock_terminating_namespace


def list_cluster_namespace_names() -> list[str]:
    listed = oc_run(
        ["get", "namespace", "-o", "json"],
        check=False,
        capture_output=True,
        timeout=60,
    )
    if listed.returncode != 0:
        err = (listed.stderr or listed.stdout or "").strip()
        raise RuntimeError(f"Could not list namespaces for E2E cleanup: {err or 'unknown error'}")
    try:
        doc = json.loads(listed.stdout or "{}")
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"Could not parse namespace list JSON: {exc}") from exc
    names: list[str] = []
    for item in doc.get("items") or []:
        if not isinstance(item, dict):
            continue
        name = str((item.get("metadata") or {}).get("name") or "")
        if name:
            names.append(name)
    return sorted(names)


def list_matching_namespace_names(match: Callable[[str], bool]) -> list[str]:
    return [name for name in list_cluster_namespace_names() if match(name)]


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


def bulk_delete_namespaces(
    names: list[str],
    *,
    log_label: str,
    empty_message: str,
) -> None:
    if not names:
        print(empty_message, flush=True)
        return
    preview = ", ".join(names[:5])
    if len(names) > 5:
        preview = f"{preview}, ..."
    print(
        f"Deleting {len(names)} {log_label} namespace(s) ({preview})",
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
            f"Bulk delete of {log_label} namespaces failed (exit {deleted.returncode}): "
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
    print(f"✓ Initiated delete for {len(names)} {log_label} namespace(s)", flush=True)
