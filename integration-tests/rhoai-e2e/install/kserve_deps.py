"""OpenShift Serverless operator prep for KServe / TrustyAI smoke (install-dep-operators)."""

from __future__ import annotations

import os
import time

from install.approve_transitive_installplans import approve_pending_installplans
from install.dsc_install import (
    _smoke_components_need_servicemesh,
    smoke_components_use_kserve_raw_deployment,
    oc_run,
)
from install.install_and_verify import (
    _named_csv_phase,
    _named_csv_succeeded_version,
    _operator_csv_phase,
    _subscription_target_csv,
    pick_succeeded_csv_version,
)

_SERVERLESS_OPERATOR = "serverless-operator"
_SERVERLESS_SUB_NS = "openshift-operators"
# Operator CSV is installed in the subscription namespace (openshift-operators), not
# openshift-serverless (Knative workload namespace created later by the operator).
_SERVERLESS_CSV_NAMESPACES = (_SERVERLESS_SUB_NS, "openshift-serverless")
# EPHC fresh clusters often need >30m for Serverless CSV (Installing → Succeeded).
_DEFAULT_TIMEOUT_SEC = 2400
_GRACE_POLL_SEC = 180

_SERVERLESS_SUB_YAML = """\
apiVersion: operators.coreos.com/v1alpha1
kind: Subscription
metadata:
  name: serverless-operator
  namespace: openshift-operators
spec:
  channel: stable
  installPlanApproval: Automatic
  name: serverless-operator
  source: redhat-operators
  sourceNamespace: openshift-marketplace
"""

_SERVERLESS_CSV_PREFIXES = ("serverless",)


def _approve_serverless_installplans() -> int:
    """Approve pending serverless-operator InstallPlans (§15 P1 parity)."""
    return approve_pending_installplans(
        _SERVERLESS_SUB_NS,
        allowed_csv_prefixes=_SERVERLESS_CSV_PREFIXES,
        restrict_to_gateway_stack=False,
    )


def components_csv_requires_kserve_deps(components_csv: str) -> bool:
    return _smoke_components_need_servicemesh(components_csv) and not smoke_components_use_kserve_raw_deployment(
        components_csv
    )


def serverless_operator_ready(*, timeout: float = 30) -> bool:
    for ns in _SERVERLESS_CSV_NAMESPACES:
        if pick_succeeded_csv_version(ns, _SERVERLESS_OPERATOR, timeout=timeout) is not None:
            return True
    return False


def _named_csv_state_in_namespaces(
    csv_name: str,
    namespaces: tuple[str, ...],
) -> tuple[str | None, str | None, str | None]:
    """Return (namespace, label, phase) for *csv_name* in the first namespace where it exists."""
    for ns in namespaces:
        ver = _named_csv_succeeded_version(ns, csv_name)
        if ver:
            return ns, csv_name, "Succeeded"
        _, phase = _named_csv_phase(ns, csv_name)
        if phase is not None:
            return ns, csv_name, phase
    return None, csv_name, None


def _serverless_csv_poll_once() -> tuple[bool, str, str | None, bool]:
    """Poll Serverless CSV once. Returns (ready, label, phase, failed)."""
    _approve_serverless_installplans()
    target_csv = _subscription_target_csv(_SERVERLESS_SUB_NS, _SERVERLESS_OPERATOR)
    if target_csv:
        for ns in _SERVERLESS_CSV_NAMESPACES:
            ver = _named_csv_succeeded_version(ns, target_csv)
            if ver:
                print(
                    f"✓ OpenShift Serverless operator CSV is ready "
                    f"(namespace={ns}, version={ver})",
                    flush=True,
                )
                return True, target_csv, "Succeeded", False
        _, label, phase = _named_csv_state_in_namespaces(target_csv, _SERVERLESS_CSV_NAMESPACES)
        if phase is None:
            phase = "Pending"
        return False, label or target_csv, phase, phase == "Failed"

    for ns in _SERVERLESS_CSV_NAMESPACES:
        ver = pick_succeeded_csv_version(ns, _SERVERLESS_OPERATOR, timeout=30)
        if ver:
            print(
                f"✓ OpenShift Serverless operator CSV is ready (namespace={ns}, version={ver})",
                flush=True,
            )
            return True, _SERVERLESS_OPERATOR, "Succeeded", False

    for ns in _SERVERLESS_CSV_NAMESPACES:
        csv_name, phase = _operator_csv_phase(ns, _SERVERLESS_OPERATOR)
        if phase is not None:
            return False, csv_name or _SERVERLESS_OPERATOR, phase, phase == "Failed"
    return False, _SERVERLESS_OPERATOR, None, False


def _resolve_serverless_wait_sec(timeout_sec: int) -> int:
    override = os.environ.get("SERVERLESS_CSV_WAIT_SEC", "").strip()
    if override:
        try:
            return int(override)
        except ValueError:
            print(
                f"WARNING: invalid SERVERLESS_CSV_WAIT_SEC={override!r}; using default {timeout_sec}s",
                flush=True,
            )
    return timeout_sec


def _wait_for_serverless_csv(*, timeout_sec: int, poll_sec: float = 15) -> bool:
    """Poll Serverless CSV in subscription namespace (and openshift-serverless fallback)."""
    deadline = time.monotonic() + timeout_sec
    last_phase: str | None = None
    ns_list = ", ".join(_SERVERLESS_CSV_NAMESPACES)
    print(
        f"Waiting up to {timeout_sec}s for OpenShift Serverless operator CSV "
        f"(subscription {_SERVERLESS_SUB_NS}, CSV namespaces: {ns_list})...",
        flush=True,
    )
    while time.monotonic() < deadline:
        ready, label, phase, failed = _serverless_csv_poll_once()
        if ready:
            return True
        if failed:
            print(f"ERROR: OpenShift Serverless CSV {label} is Failed", flush=True)
            return False
        if phase and phase != last_phase:
            print(f"  OpenShift Serverless CSV {label} phase={phase}", flush=True)
            last_phase = phase
        time.sleep(poll_sec)
    if _GRACE_POLL_SEC > 0:
        print(
            f"OpenShift Serverless CSV not Succeeded after {timeout_sec}s; "
            f"grace poll {_GRACE_POLL_SEC}s...",
            flush=True,
        )
        grace_deadline = time.monotonic() + _GRACE_POLL_SEC
        while time.monotonic() < grace_deadline:
            ready, label, phase, failed = _serverless_csv_poll_once()
            if ready:
                return True
            if failed:
                print(f"ERROR: OpenShift Serverless CSV {label} is Failed", flush=True)
                return False
            time.sleep(poll_sec)
    return False


def ensure_serverless_operator(*, timeout_sec: int = _DEFAULT_TIMEOUT_SEC) -> None:
    """Install Serverless operator only (no KnativeServing CR) for automated KServe."""
    if serverless_operator_ready():
        print("✓ OpenShift Serverless operator CSV is ready", flush=True)
        return

    r = oc_run(
        ["get", "subscription", _SERVERLESS_OPERATOR, "-n", _SERVERLESS_SUB_NS],
        check=False,
        capture_output=True,
        timeout=30,
    )
    if r.returncode != 0:
        print("Installing OpenShift Serverless operator subscription...", flush=True)
        apply = oc_run(
            ["apply", "-f", "-"],
            stdin_text=_SERVERLESS_SUB_YAML,
            check=False,
            capture_output=True,
            timeout=60,
        )
        if apply.returncode != 0:
            err = (apply.stderr or apply.stdout or "").strip()
            raise RuntimeError(f"Could not create serverless-operator subscription: {err or 'unknown error'}")
        approved = _approve_serverless_installplans()
        if approved:
            print(f"Approved {approved} pending serverless InstallPlan(s)", flush=True)

    wait_sec = _resolve_serverless_wait_sec(timeout_sec)
    if not _wait_for_serverless_csv(timeout_sec=wait_sec):
        raise RuntimeError(
            f"OpenShift Serverless operator CSV not Succeeded after {wait_sec}s "
            f"(namespaces: {', '.join(_SERVERLESS_CSV_NAMESPACES)})"
        )
