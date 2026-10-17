"""Bulk-delete dependency operator CSV copies before rhoai-e2e cleanup.sh."""

from __future__ import annotations

import json
import os
import sys

from install.dsc_install import oc_run

# Namespaces that may hold dependency-operator subscriptions cleanup.sh removes.
_SUBSCRIPTION_NAMESPACES: tuple[str, ...] = (
    "openshift-operators",
    "kuadrant-system",
    "rh-connectivity-link",
    "opendatahub-ogx-system",
)

_CSV_DELETE_BATCH_SIZE = 40


def _cleanup_enabled() -> bool:
    raw = os.environ.get("CLEANUP_DEPENDENCY_OPERATOR_CSVS", "true").strip().lower()
    return raw not in ("0", "false", "no", "off")


def _dependency_csv_prefixes() -> tuple[str, ...]:
    from install.approve_transitive_installplans import _DEFAULT_TRANSITIVE_CSV_PREFIXES
    from install.kserve_deps import _SERVERLESS_CSV_PREFIXES
    from install.rhcl_deps import _KUADRANT_STACK_CSV_PREFIXES

    extra = ("loki-operator", "custom-metrics-autoscaler")
    seen: set[str] = set()
    ordered: list[str] = []
    for prefix in (
        *_DEFAULT_TRANSITIVE_CSV_PREFIXES,
        *_KUADRANT_STACK_CSV_PREFIXES,
        *_SERVERLESS_CSV_PREFIXES,
        *extra,
    ):
        key = prefix.lower()
        if key in seen:
            continue
        seen.add(key)
        ordered.append(key)
    return tuple(ordered)


def _csv_package_base(name: str) -> str:
    return name.split(".", 1)[0].lower()


def _matches_dependency_csv_prefix(name: str, prefixes: tuple[str, ...]) -> bool:
    base = _csv_package_base(name)
    return any(base == prefix or base.startswith(prefix) for prefix in prefixes)


def _list_dependency_subscriptions(prefixes: tuple[str, ...]) -> list[tuple[str, str]]:
    matches: list[tuple[str, str]] = []
    for namespace in _SUBSCRIPTION_NAMESPACES:
        listed = oc_run(
            ["get", "subscription", "-n", namespace, "-o", "json"],
            check=False,
            capture_output=True,
            timeout=60,
        )
        if listed.returncode != 0:
            continue
        try:
            doc = json.loads(listed.stdout or "{}")
        except json.JSONDecodeError:
            continue
        for item in doc.get("items") or []:
            if not isinstance(item, dict):
                continue
            meta = item.get("metadata") or {}
            spec = item.get("spec") or {}
            sub_name = str(meta.get("name") or "").strip()
            package_name = str(spec.get("name") or sub_name).strip()
            if not sub_name:
                continue
            if _matches_dependency_csv_prefix(package_name, prefixes):
                matches.append((namespace, sub_name))
    return sorted(set(matches))


def _delete_dependency_subscriptions(prefixes: tuple[str, ...]) -> int:
    targets = _list_dependency_subscriptions(prefixes)
    if not targets:
        return 0
    preview = ", ".join(f"{ns}/{name}" for ns, name in targets[:5])
    if len(targets) > 5:
        preview = f"{preview}, ..."
    print(
        f"Deleting {len(targets)} dependency operator subscription(s) before rhoai-e2e cleanup.sh ({preview})",
        flush=True,
    )
    removed = 0
    for namespace, name in targets:
        deleted = oc_run(
            ["delete", "subscription", name, "-n", namespace, "--ignore-not-found", "--wait=false"],
            check=False,
            capture_output=True,
            timeout=60,
        )
        if deleted.returncode == 0:
            removed += 1
            print(f"✓ Deleted subscription {namespace}/{name}", flush=True)
        else:
            err = (deleted.stderr or deleted.stdout or "").strip()
            raise RuntimeError(
                f"Could not delete subscription {namespace}/{name}: {err or 'unknown error'}"
            )
    return removed


def _list_dependency_csvs_by_namespace(prefixes: tuple[str, ...]) -> dict[str, list[str]]:
    listed = oc_run(
        ["get", "csv", "-A", "-o", "json"],
        check=False,
        capture_output=True,
        timeout=180,
    )
    if listed.returncode != 0:
        err = (listed.stderr or listed.stdout or "").strip()
        raise RuntimeError(f"Could not list CSVs for dependency cleanup: {err or 'unknown error'}")
    try:
        doc = json.loads(listed.stdout or "{}")
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"Could not parse CSV list JSON: {exc}") from exc

    by_namespace: dict[str, list[str]] = {}
    for item in doc.get("items") or []:
        if not isinstance(item, dict):
            continue
        meta = item.get("metadata") or {}
        namespace = str(meta.get("namespace") or "").strip()
        name = str(meta.get("name") or "").strip()
        if not namespace or not name:
            continue
        if not _matches_dependency_csv_prefix(name, prefixes):
            continue
        by_namespace.setdefault(namespace, []).append(name)
    return {ns: sorted(set(names)) for ns, names in sorted(by_namespace.items())}


def _bulk_delete_csvs(by_namespace: dict[str, list[str]]) -> int:
    removed = 0
    for namespace, names in by_namespace.items():
        for start in range(0, len(names), _CSV_DELETE_BATCH_SIZE):
            batch = names[start : start + _CSV_DELETE_BATCH_SIZE]
            deleted = oc_run(
                [
                    "delete",
                    "csv",
                    *batch,
                    "-n",
                    namespace,
                    "--ignore-not-found",
                    "--wait=false",
                ],
                check=False,
                capture_output=True,
                timeout=180,
            )
            if deleted.returncode != 0:
                err = (deleted.stderr or deleted.stdout or "").strip()
                raise RuntimeError(
                    f"Bulk delete of dependency CSVs in {namespace} failed "
                    f"(exit {deleted.returncode}): {err or 'unknown error'}"
                )
            removed += len(batch)
    return removed


def cleanup_dependency_operator_csvs() -> None:
    """Delete dependency operator subscriptions and CSV copies before cleanup.sh safety-net."""
    if not _cleanup_enabled():
        print(
            "NOTE: skipping dependency operator CSV pre-cleanup (CLEANUP_DEPENDENCY_OPERATOR_CSVS=0)",
            flush=True,
        )
        return

    prefixes = _dependency_csv_prefixes()
    subs_removed = _delete_dependency_subscriptions(prefixes)
    by_namespace = _list_dependency_csvs_by_namespace(prefixes)
    csv_count = sum(len(names) for names in by_namespace.values())
    if subs_removed == 0 and csv_count == 0:
        print("✓ No dependency operator subscriptions or CSV copies matched pre-cleanup prefixes", flush=True)
        return

    if csv_count:
        preview_ns = ", ".join(
            f"{ns}({len(names)})" for ns, names in list(by_namespace.items())[:5]
        )
        if len(by_namespace) > 5:
            preview_ns = f"{preview_ns}, ..."
        print(
            f"Deleting {csv_count} dependency operator CSV copy/copies across "
            f"{len(by_namespace)} namespace(s) before rhoai-e2e cleanup.sh ({preview_ns})",
            flush=True,
        )
        removed = _bulk_delete_csvs(by_namespace)
        print(f"✓ Initiated delete for {removed} dependency operator CSV copy/copies", flush=True)
