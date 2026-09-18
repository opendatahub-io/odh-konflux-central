#!/usr/bin/env python3
"""Tekton verify-operator-ready entry (Jenkins verifyDashboardRoute after install)."""

from __future__ import annotations

import os
import sys
from pathlib import Path

from _bootstrap import ensure_rhoai_e2e_path

ensure_rhoai_e2e_path()

from components.dashboard_cypress.verify_route import verify_dashboard_route_for_prepare
from components.dashboard_cypress.runtime import log_gateway_auth_stack_warnings
from suite.constants import is_test_only_product
from steps.tests_payload import ensure_tests_payload_layout, resolve_tests_payload_root

_SKIP_MARKER = ".skip-verify-operator-ready"


def _skip_requested(tests_shared: str) -> bool:
    if not tests_shared:
        return False
    return (Path(tests_shared) / _SKIP_MARKER).is_file()


def _truthy_env(name: str) -> bool:
    return os.environ.get(name, "").strip().lower() in ("1", "true", "yes")


def _selected_component_ids() -> set[str]:
    csv = os.environ.get("COMPONENTS_CSV", "").strip()
    if not csv:
        return set()
    return {part.strip() for part in csv.split(",") if part.strip()}


def _dsc_crd_available() -> bool:
    from install.dsc_install import dsc_crd_available

    return dsc_crd_available()


def _skip_dashboard_verify_reason() -> str | None:
    """Skip Jenkins dashboard gate for deps-only test-only cluster smoke (e.g. model_server)."""
    if not is_test_only_product(os.environ.get("PRODUCT", "")):
        return None
    component_ids = _selected_component_ids()
    if "dashboard_cypress" in component_ids:
        return None
    install_dependencies = _truthy_env("INSTALL_DEPENDENCIES") or _truthy_env(
        "RUN_COMPONENT_CLUSTER_PREP_IN_DEP_OPERATORS",
    )
    if install_dependencies and component_ids:
        return (
            "test-only PRODUCT with install-dependencies and no dashboard_cypress "
            f"in COMPONENTS_CSV ({','.join(sorted(component_ids))})"
        )
    if component_ids and not _dsc_crd_available():
        return "no DataScienceCluster CRD on cluster (deps-only component smoke)"
    return None


def _artifacts_dir() -> Path | None:
    tests_shared = os.environ.get("TESTS_SHARED", "").strip()
    if tests_shared:
        ensure_tests_payload_layout(Path(tests_shared))
        return resolve_tests_payload_root(Path(tests_shared)) / "results"
    raw = os.environ.get("ARTIFACTS_DIR", "").strip()
    return Path(raw) if raw else None


def main() -> int:
    tests_shared = os.environ.get("TESTS_SHARED", "").strip()
    if _skip_requested(tests_shared):
        print("Skipping verify-operator-ready (no workload cluster)")
        return 0
    skip_reason = _skip_dashboard_verify_reason()
    if skip_reason:
        print(f"Skipping verify-operator-ready ({skip_reason})")
        return 0
    if not os.environ.get("KUBECONFIG", "").strip():
        print("ERROR: KUBECONFIG is required", file=sys.stderr)
        return 1
    try:
        url = verify_dashboard_route_for_prepare(artifacts_dir=_artifacts_dir())
    except RuntimeError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    log_gateway_auth_stack_warnings()
    if tests_shared and url:
        url_file = resolve_tests_payload_root(Path(tests_shared)) / "odh-dashboard-url.txt"
        url_file.parent.mkdir(parents=True, exist_ok=True)
        url_file.write_text(url.rstrip("/") + "\n", encoding="utf-8")
        print(f"Wrote {url_file}", flush=True)
    print(f"✓ Operator dashboard ready: {url}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
