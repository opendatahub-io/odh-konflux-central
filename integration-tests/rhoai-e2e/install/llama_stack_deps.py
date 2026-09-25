"""Llama Stack operator prep for install-dep-operators (DSC component, not setup-dependencies.sh)."""

from __future__ import annotations

import sys
import time

from install.dsc_install import _cr_exists, ensure_dsc_component_managed, oc_run
from components.maas_billing.common import _dsc_condition_types
from components.maas_billing.wait import _wait_for_dsc_component_ready

_LLAMA_STACK_CRD = "llamastackdistributions.llamastack.io"
_DEFAULT_TIMEOUT_SEC = 900


def components_need_llama_stack_deps(component_ids: set[str]) -> bool:
    return "llama_stack" in component_ids


def _llama_stack_enabled_for_current_version() -> bool:
    from suite.component_catalog import default_components_smoke_config_path, load_components_smoke_catalog
    from suite.component_version_gate import version_skip_reason_for_component

    catalog = load_components_smoke_catalog(default_components_smoke_config_path())
    comp = catalog.components.get("llama_stack")
    if comp is None:
        return False
    return not version_skip_reason_for_component(comp)


def components_csv_requires_llama_stack(components_csv: str) -> bool:
    ids = {c.strip() for c in (components_csv or "").split(",") if c.strip()}
    if not components_need_llama_stack_deps(ids):
        return False
    return _llama_stack_enabled_for_current_version()


def llama_stack_crd_present() -> bool:
    r = oc_run(
        ["get", "crd", _LLAMA_STACK_CRD],
        check=False,
        capture_output=True,
        timeout=30,
    )
    return r.returncode == 0


def _wait_for_llama_stack_crd(*, timeout_sec: int) -> None:
    deadline = time.time() + timeout_sec
    while time.time() < deadline:
        if llama_stack_crd_present():
            print(f"✓ {_LLAMA_STACK_CRD} CRD present", flush=True)
            return
        if int(time.time()) % 60 < 12:
            print(f"Waiting for {_LLAMA_STACK_CRD} CRD...", flush=True)
        time.sleep(12)
    raise RuntimeError(f"{_LLAMA_STACK_CRD} CRD not found after {timeout_sec}s")


def try_prepare_llama_stack_operator(*, timeout_sec: int = _DEFAULT_TIMEOUT_SEC) -> bool:
    """Best-effort Llama Stack operator prep; returns False without failing the pipeline."""
    if not _llama_stack_enabled_for_current_version():
        print(
            "Skipping Llama Stack operator prep (llama_stack version-gated for installed RHOAI)",
            flush=True,
        )
        return False
    print("=== Llama Stack operator dependencies (install-dep-operators) ===", flush=True)
    if not _cr_exists("datasciencecluster", "default-dsc"):
        print(
            "NOTE: DataScienceCluster/default-dsc not present yet (normal before RHOAI install); "
            "llama_stack DSC sync runs during install-operator / component prep",
            flush=True,
        )
        return False
    try:
        ensure_dsc_component_managed("llamastackoperator")
    except Exception as exc:
        print(
            f"WARN: could not patch llamastackoperator Managed ({exc}); "
            "llama_stack smoke will verify readiness in the component task",
            file=sys.stderr,
            flush=True,
        )
        return False
    try:
        if "LlamaStackOperatorReady" in _dsc_condition_types():
            _wait_for_dsc_component_ready(
                condition_type="LlamaStackOperatorReady",
                timeout_sec=timeout_sec,
            )
        else:
            print(
                "NOTE: DSC has no LlamaStackOperatorReady condition; "
                f"waiting for {_LLAMA_STACK_CRD} CRD",
                flush=True,
            )
            _wait_for_llama_stack_crd(timeout_sec=timeout_sec)
        print("✓ Llama Stack operator dependencies ready", flush=True)
        return True
    except Exception as exc:
        print(
            f"WARN: Llama Stack operator not ready after install-dep-operators ({exc}); "
            "llama_stack smoke will record a failure and other component tests will continue",
            file=sys.stderr,
            flush=True,
        )
        return False
