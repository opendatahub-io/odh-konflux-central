#!/usr/bin/env python3
"""Resolve quay.io/opendatahub/opendatahub-tests image reference for BVT.

Tag rules mirror Jenkins vars/validateHealth.groovy (RHOAI_VERSION -> image tag).
Writes the resolved image reference to RESULT_PATH for Tekton results.

Env: OPERATOR_VERSION, OPENDATAHUB_TESTS_REPO (default quay.io/opendatahub/opendatahub-tests),
     RESULT_PATH (required -- Tekton result file).
"""

from __future__ import annotations

import os

from _bootstrap import ensure_rhoai_e2e_path

ensure_rhoai_e2e_path()

from suite.resolve_versioned_image import resolve_versioned_image
from steps.tekton_util import require_env, write_result


def _skip_csv_cluster_probe() -> bool:
    """Skip CSV probe only for snapshot-only runs (no workload cluster)."""
    if os.environ.get("OLMINSTALL_SKIP_CSV_PROBE", "").strip().lower() in ("1", "true", "yes"):
        return True
    product = os.environ.get("PRODUCT", "").strip().lower()
    cluster_source = os.environ.get("CLUSTER_SOURCE", "").strip()
    from suite.constants import is_test_only_product

    if is_test_only_product(product) and not cluster_source:
        return True
    return False


def _kubeconfig_for_csv_probe() -> str:
    """Return a kubeconfig path for cluster CSV probe, if staged."""
    explicit = os.environ.get("KUBECONFIG", "").strip()
    if explicit and os.path.isfile(explicit):
        return explicit
    shared = os.environ.get("TESTS_SHARED", "").strip()
    if shared:
        staged = os.path.join(shared, "credentials", "kubeconfig")
        if os.path.isfile(staged):
            return staged
    mounted = "/credentials/kubeconfig"
    if os.path.isfile(mounted):
        return mounted
    return ""


def _probe_csv_from_kubeconfig() -> str:
    """When OPERATOR_VERSION is unset, read installed CSV from mounted KUBECONFIG."""
    if _skip_csv_cluster_probe():
        print("Skipping CSV cluster probe (no workload cluster or OLMINSTALL_SKIP_CSV_PROBE)")
        return ""
    kc = _kubeconfig_for_csv_probe()
    op_ns = os.environ.get("OPERATOR_NAMESPACE", "").strip()
    op_name = (os.environ.get("OPERATOR_NAME", "") or "rhods-operator").strip()
    if not kc or not op_ns:
        return ""
    os.environ["KUBECONFIG"] = kc
    from install.install_and_verify import pick_succeeded_csv_version

    try:
        ver = pick_succeeded_csv_version(op_ns, op_name, timeout=20)
    except Exception as exc:
        print(f"Warning: CSV probe failed for {op_ns}/{op_name}: {exc}")
        return ""
    if ver:
        print(f"Probed CSV version {ver} from cluster ({op_ns}/{op_name})")
    return (ver or "").strip()


def resolve_csv_version_for_tests_image() -> str:
    """CSV / override input for versioned component test images."""
    override = os.environ.get("OLMINSTALL_TESTS_VERSION_OVERRIDE", "").strip()
    if override:
        print(f"Using OLMINSTALL_TESTS_VERSION_OVERRIDE={override!r} for test image resolve")
        return override
    product = os.environ.get("PRODUCT", "").strip().lower()
    if product == "odh":
        print("PRODUCT=odh: using :latest (Jenkins odh-stable.yaml)")
        return ""
    csv_version = os.environ.get("OPERATOR_VERSION", "").strip()
    if not csv_version:
        csv_version = _probe_csv_from_kubeconfig()
    return (csv_version or "").strip()


def main() -> int:
    result_path = require_env("RESULT_PATH")
    repo = os.environ.get("OPENDATAHUB_TESTS_REPO", "").strip() or "quay.io/opendatahub/opendatahub-tests"
    csv_version = resolve_csv_version_for_tests_image()

    resolved = resolve_versioned_image(repo, csv_version)
    write_result(result_path, resolved)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
