"""Orchestrate RHOAI gateway install prep (plan §15 P1–P4)."""

from __future__ import annotations

import os
import sys

from install.approve_transitive_installplans import approve_pending_installplans
from install.dsc_install import ensure_dashboard_gateway_prereqs
from install.gateway_config import (
    cluster_source_is_ephc,
    ensure_rhoai_gateway_for_install,
    gateway_config_ready,
    gateway_oidc_configured,
    reconcile_servicemesh_olm_conflicts,
    wait_servicemesh_csv_succeeded,
)


def _components_need_gateway_stack(components_csv: str) -> bool:
    ids = {c.strip() for c in components_csv.split(",") if c.strip()}
    return "dashboard_cypress" in ids


def ensure_transitive_olm_deps_for_gateway(*, wait_servicemesh: bool = True) -> int:
    """P1: reconcile SM OLM drift, approve InstallPlans, optionally wait for Service Mesh CSV."""
    removed = reconcile_servicemesh_olm_conflicts("openshift-operators")
    if removed:
        print(f"✓ Reconciled {removed} orphan Service Mesh CSV(s) for gateway stack", flush=True)
    approved = approve_pending_installplans("openshift-operators")
    if approved and wait_servicemesh:
        timeout = int(os.environ.get("SERVICEMESH_CSV_WAIT_SEC", "900"))
        wait_servicemesh_csv_succeeded(timeout_sec=timeout)
    return approved


def ensure_rhoai_gateway_stack_for_components(component_ids: set[str] | str) -> None:
    """P1–P4 entry for prepare/install when gateway-backed components are selected."""
    if isinstance(component_ids, str):
        csv = component_ids
        ids = {c.strip() for c in csv.split(",") if c.strip()}
    else:
        ids = {c.strip() for c in component_ids if c.strip()}
        csv = ",".join(sorted(ids))
    if not _components_need_gateway_stack(csv):
        return
    if gateway_config_ready():
        if cluster_source_is_ephc() and not gateway_oidc_configured():
            print(
                "EPHC GatewayConfig Ready but OIDC unset — running gateway OIDC patch",
                flush=True,
            )
        else:
            print("✓ GatewayConfig already Ready — skipping RHOAI gateway prep", flush=True)
            return
    product = os.environ.get("PRODUCT", "").strip().lower()
    from suite.constants import is_test_only_product

    if is_test_only_product(product) and "dashboard_cypress" in ids:
        from components.dashboard_cypress.verify_route import dashboard_cypress_accessible_for_smoke

        if dashboard_cypress_accessible_for_smoke():
            print(
                "test-only PRODUCT: skipping RHOAI gateway stack wait "
                "(dashboard ready; Cypress uses gateway URL)",
                flush=True,
            )
            return
    print("RHOAI gateway prep for selected components...", flush=True)
    ensure_dashboard_gateway_prereqs(for_gateway_stack=True)
    try:
        approved = ensure_transitive_olm_deps_for_gateway(wait_servicemesh=False)
        if approved:
            print(f"✓ Approved {approved} transitive InstallPlan(s) for gateway stack", flush=True)
    except Exception as exc:
        print(f"WARN: transitive OLM approve failed ({exc})", file=sys.stderr, flush=True)
    try:
        ensure_rhoai_gateway_for_install(wait_servicemesh_first=True)
    except Exception as exc:
        print(f"WARN: gateway OIDC/ready prep failed ({exc})", file=sys.stderr, flush=True)
