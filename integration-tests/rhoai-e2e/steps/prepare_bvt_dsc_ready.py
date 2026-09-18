"""Reconcile MaaS ingress and wait for DSC+dashboard before operator_health BVT."""

from __future__ import annotations

import os
import sys


def prepare_bvt_dsc_ready() -> int:
    """Repair stale MaaS ingress workloads and wait for DSC Ready before BVT."""
    product = os.environ.get("PRODUCT", "").strip().lower()
    if product not in ("rhoai", "odh"):
        return 0

    from install.dsc_install import dsc_crd_available

    if not dsc_crd_available():
        print("NOTE: skipping BVT DSC ready wait (DataScienceCluster CRD absent)", flush=True)
        return 0

    from components.maas_billing.bbr_pre_processing import (
        cleanup_stale_maas_ingress_workloads,
        repair_payload_pre_processing_selector_conflict,
    )
    from components.maas_billing.timeouts import bvt_dsc_ready_timeout_sec
    from components.maas_billing.wait import require_dsc_ready_for_bvt

    cleanup_stale_maas_ingress_workloads()
    repair_payload_pre_processing_selector_conflict()
    try:
        require_dsc_ready_for_bvt(timeout_sec=bvt_dsc_ready_timeout_sec())
        from steps.prepare_bvt_apps_namespace import wait_dashboard_pods_ready_for_bvt

        wait_dashboard_pods_ready_for_bvt()
    except RuntimeError as exc:
        print(f"ERROR: {exc}", file=sys.stderr, flush=True)
        return 1
    return 0


def main() -> int:
    return prepare_bvt_dsc_ready()


if __name__ == "__main__":
    raise SystemExit(main())
