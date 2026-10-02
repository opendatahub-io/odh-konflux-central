"""Ensure Kueue operator CRDs are available before codeflare_sdk smoke."""

from __future__ import annotations

import json
import os
import time

from install.dsc_install import ensure_dsc_component_management_state, oc_run
from suite.component_dsc_gate import _dsc_condition
from suite.component_version_gate import (
    _compare_version_strings,
    normalize_version_for_enablement,
    probe_operator_version_from_cluster,
)

_KUEUE_API_GROUP = "kueue.x-k8s.io"
_OPENSHIFT_KUEUE_CLUSTER = "cluster"
_KUEUE_WEBHOOK_NS = "openshift-kueue-operator"
_KUEUE_WEBHOOK_SVC = "kueue-webhook-service"
_DEFAULT_TIMEOUT_SEC = 600
_POLL_SEC = 15


def _kueue_dsc_management_state(operator_version: str = "") -> str:
    """RHOAI 3.5+ DSC webhook rejects kueue=Managed; use Unmanaged for smoke prep."""
    ver = (operator_version or probe_operator_version_from_cluster() or "").strip()
    compare_ver, is_numeric = normalize_version_for_enablement(ver)
    if not is_numeric:
        # Version probe often fails in early Tekton steps; 3.5+ only allows Unmanaged.
        return "Unmanaged"
    if _compare_version_strings(compare_ver, "3.5") >= 0:
        return "Unmanaged"
    return "Managed"


def _kueue_api_available() -> bool:
    proc = oc_run(
        ["api-resources", f"--api-group={_KUEUE_API_GROUP}", "-o", "name"],
        check=False,
        capture_output=True,
        timeout=30,
    )
    if proc.returncode != 0:
        return False
    return "resourceflavor" in (proc.stdout or "").lower()


def _kueue_webhook_ready() -> bool:
    """Conversion webhook Service must exist with endpoints (ClusterQueue create fails otherwise)."""
    svc = oc_run(
        ["get", "svc", _KUEUE_WEBHOOK_SVC, "-n", _KUEUE_WEBHOOK_NS, "-o", "name"],
        check=False,
        capture_output=True,
        timeout=30,
    )
    if svc.returncode != 0:
        return False
    eps = oc_run(
        ["get", "endpoints", _KUEUE_WEBHOOK_SVC, "-n", _KUEUE_WEBHOOK_NS, "-o", "json"],
        check=False,
        capture_output=True,
        timeout=30,
    )
    if eps.returncode != 0:
        return False
    try:
        doc = json.loads(eps.stdout or "{}")
    except json.JSONDecodeError:
        return False
    for subset in doc.get("subsets") or []:
        if not isinstance(subset, dict):
            continue
        if subset.get("addresses"):
            return True
    return False


def _kueue_smoke_ready() -> bool:
    return _kueue_api_available() and _kueue_webhook_ready()


def _clear_stuck_openshift_kueue_cluster() -> None:
    """Remove finalizers when kueue.openshift.io/cluster is stuck terminating."""
    proc = oc_run(
        [
            "get",
            f"kueue.kueue.openshift.io/{_OPENSHIFT_KUEUE_CLUSTER}",
            "-o",
            "json",
        ],
        check=False,
        capture_output=True,
        timeout=30,
    )
    if proc.returncode != 0:
        return
    try:
        doc = json.loads(proc.stdout or "{}")
    except json.JSONDecodeError:
        return
    if not doc.get("metadata", {}).get("deletionTimestamp"):
        return
    name = doc.get("metadata", {}).get("name", _OPENSHIFT_KUEUE_CLUSTER)
    patch = oc_run(
        [
            "patch",
            f"kueue.kueue.openshift.io/{name}",
            "--type=merge",
            "-p",
            '{"metadata":{"finalizers":[]}}',
        ],
        check=False,
        capture_output=True,
        timeout=30,
    )
    if patch.returncode == 0:
        print(f"✓ Cleared finalizers on stuck Kueue/{name}", flush=True)


def ensure_codeflare_kueue_ready(timeout_sec: int | None = None) -> None:
    """Enable DSC Kueue and wait until RayJob smoke can use kueue.x-k8s.io + conversion webhook."""
    if _kueue_smoke_ready():
        print(
            "✓ Kueue API + webhook "
            f"({_KUEUE_WEBHOOK_NS}/{_KUEUE_WEBHOOK_SVC}) already ready",
            flush=True,
        )
        return

    timeout = timeout_sec or int(
        os.environ.get("CODEFLARE_KUEUE_READY_TIMEOUT_SEC", str(_DEFAULT_TIMEOUT_SEC))
    )
    state = _kueue_dsc_management_state()
    ensure_dsc_component_management_state("kueue", state)
    _clear_stuck_openshift_kueue_cluster()
    deadline = time.time() + timeout
    while time.time() < deadline:
        if _kueue_smoke_ready():
            print(
                "✓ Kueue API + webhook "
                f"({_KUEUE_WEBHOOK_NS}/{_KUEUE_WEBHOOK_SVC}) ready",
                flush=True,
            )
            return
        api_ok = _kueue_api_available()
        wh_ok = _kueue_webhook_ready()
        status, reason, message = _dsc_condition("KueueReady")
        detail = (
            f"api={'ok' if api_ok else 'missing'} "
            f"webhook={'ok' if wh_ok else 'missing'} "
            f"KueueReady={status or '?'} reason={reason or '?'}"
        )
        if message:
            detail = f"{detail}: {message[:120]}"
        print(f"Waiting for Kueue ({detail})...", flush=True)
        time.sleep(_POLL_SEC)
    raise RuntimeError(
        f"Kueue not ready after {timeout}s "
        f"(need {_KUEUE_API_GROUP} CRDs and {_KUEUE_WEBHOOK_NS}/{_KUEUE_WEBHOOK_SVC} endpoints)"
    )
