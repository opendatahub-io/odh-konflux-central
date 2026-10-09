"""Approve pending OLM InstallPlans (Jenkins: Approve All Pending Installplans)."""

from __future__ import annotations

import json
import sys

from install.dsc_install import oc_run

_DEFAULT_NAMESPACE = "openshift-operators"
# Gateway stack transitive deps in openshift-operators (Service Mesh, etc.).
_DEFAULT_TRANSITIVE_CSV_PREFIXES = (
    "servicemeshoperator",
    "servicemesh",
    "kiali",
    "jaeger",
    "tempo",
    "opentelemetry",
)


def _installplan_csv_allowed(
    csv_names: list[object],
    allowed_csv_prefixes: tuple[str, ...] | None,
) -> bool:
    if not allowed_csv_prefixes:
        return True
    for raw in csv_names:
        csv = str(raw or "").strip().lower()
        if not csv:
            continue
        base = csv.split(".", 1)[0]
        if any(base.startswith(prefix) for prefix in allowed_csv_prefixes):
            return True
    return False


def approve_pending_installplans(
    namespace: str = _DEFAULT_NAMESPACE,
    *,
    allowed_csv_prefixes: tuple[str, ...] | None = None,
    restrict_to_gateway_stack: bool | None = None,
) -> int:
    """Approve pending InstallPlans in *namespace* whose CSVs match the allowlist when restricted."""
    if restrict_to_gateway_stack is None:
        restrict = namespace == _DEFAULT_NAMESPACE
    else:
        restrict = restrict_to_gateway_stack
    prefixes = allowed_csv_prefixes if allowed_csv_prefixes is not None else (
        _DEFAULT_TRANSITIVE_CSV_PREFIXES if restrict else None
    )
    r = oc_run(
        ["get", "installplan", "-n", namespace, "-o", "json"],
        check=False,
        capture_output=True,
        timeout=120,
    )
    if r.returncode != 0:
        print(
            f"WARN: could not list InstallPlans in {namespace}: {(r.stderr or r.stdout or '').strip()}",
            file=sys.stderr,
        )
        return 0
    try:
        doc = json.loads(r.stdout or "{}")
    except json.JSONDecodeError:
        print(f"WARN: invalid InstallPlan list JSON in {namespace}", file=sys.stderr)
        return 0

    approved = 0
    for item in doc.get("items") or []:
        if not isinstance(item, dict):
            continue
        spec = item.get("spec") or {}
        if spec.get("approved") is True:
            continue
        csvs = spec.get("clusterServiceVersionNames") or []
        if not _installplan_csv_allowed(csvs, prefixes):
            name = (item.get("metadata") or {}).get("name") or ""
            print(
                f"Skipping InstallPlan/{name} in {namespace} (CSVs {csvs} not in allowlist)",
                flush=True,
            )
            continue
        name = (item.get("metadata") or {}).get("name") or ""
        if not name:
            continue
        patch = json.dumps({"spec": {"approved": True}})
        pr = oc_run(
            ["patch", "installplan", name, "-n", namespace, "--type=merge", "-p", patch],
            check=False,
            capture_output=True,
            timeout=60,
        )
        if pr.returncode != 0:
            err = (pr.stderr or pr.stdout or "").strip()
            print(f"WARN: could not approve InstallPlan/{name}: {err}", file=sys.stderr)
            continue
        print(f"✓ Approved InstallPlan/{name} in {namespace} (CSVs: {csvs})", flush=True)
        approved += 1
    return approved
