"""Wait for Enterprise Contract (conforma) on the same Konflux Snapshot before e2e smoke."""

from __future__ import annotations

import json
import time
from dataclasses import dataclass
from typing import Any, Callable, Protocol

from suite.constants import is_test_only_product

LABEL_SNAPSHOT = "appstudio.openshift.io/snapshot"
LABEL_EC_KIND = "test.appstudio.openshift.io/kind"
EC_KIND_VALUE = "enterprise-contract"

CONFORMA_GATE_PASS = "pass"
CONFORMA_GATE_SKIP = "skip"

_POLL_INTERVAL_SEC = 15


@dataclass(frozen=True)
class ConformaGateDecision:
    gate: str  # pass | skip
    reason: str
    conforma_runs: tuple[str, ...] = ()
    note: str = ""


def snapshot_name_from_snapshot_param(snapshot_raw: str) -> str:
    raw = (snapshot_raw or "").strip()
    if not raw:
        return ""
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return ""
    if not isinstance(data, dict):
        return ""
    meta = data.get("metadata")
    if isinstance(meta, dict):
        name = meta.get("name")
        if isinstance(name, str) and name.strip():
            return name.strip()
    return ""


def snapshot_name_from_env(*, snapshot_name: str = "") -> str:
    """Read Konflux Snapshot name from ``SNAPSHOT_NAME`` (Tekton PipelineRun label)."""
    from steps.tekton_util import resolved_tekton_env_value

    return resolved_tekton_env_value(snapshot_name or "")


class _SnapshotLabelReader(Protocol):
    def __call__(self) -> str: ...


def resolve_snapshot_name(
    snapshot_param: str,
    *,
    snapshot_name_env: str = "",
    pipeline_run_snapshot_label_fn: _SnapshotLabelReader | None = None,
) -> str:
    """Resolve Snapshot name for conforma gate (ITS spec-only SNAPSHOT, label, or API)."""
    snap = snapshot_name_from_snapshot_param(snapshot_param)
    if snap:
        return snap
    snap = snapshot_name_from_env(snapshot_name=snapshot_name_env)
    if snap:
        return snap
    if pipeline_run_snapshot_label_fn is not None:
        return (pipeline_run_snapshot_label_fn() or "").strip()
    return ""


def is_enterprise_contract_pipelinerun(item: dict[str, Any]) -> bool:
    labels = (item.get("metadata") or {}).get("labels") or {}
    if not isinstance(labels, dict):
        return False
    kind = str(labels.get(LABEL_EC_KIND) or "").strip().lower()
    if kind == EC_KIND_VALUE:
        return True
    scenario = str(labels.get("test.appstudio.openshift.io/scenario") or "").strip().lower()
    return "conforma" in scenario or "enterprise-contract" in scenario


def pipelinerun_succeeded_status(item: dict[str, Any]) -> str | None:
    """Return ``True``/``False`` when finished, ``None`` when still running."""
    status = item.get("status")
    if not isinstance(status, dict):
        return None
    for cond in status.get("conditions") or []:
        if not isinstance(cond, dict):
            continue
        if cond.get("type") != "Succeeded":
            continue
        value = str(cond.get("status") or "").strip()
        if value == "True":
            return "True"
        if value == "False":
            return "False"
    return None


def evaluate_conforma_runs(items: list[dict[str, Any]]) -> ConformaGateDecision | None:
    """Return a decision when conforma runs for the snapshot have reached a terminal state."""
    ec_runs = [x for x in items if isinstance(x, dict) and is_enterprise_contract_pipelinerun(x)]
    if not ec_runs:
        return None
    names: list[str] = []
    pending = 0
    failed: list[str] = []
    for item in ec_runs:
        meta = item.get("metadata") or {}
        name = str(meta.get("name") or "").strip() if isinstance(meta, dict) else ""
        if name:
            names.append(name)
        outcome = pipelinerun_succeeded_status(item)
        if outcome is None:
            pending += 1
        elif outcome == "False":
            failed.append(name or "(unknown)")
    if pending:
        return None
    if failed:
        joined = ", ".join(failed[:3])
        extra = f" (+{len(failed) - 3} more)" if len(failed) > 3 else ""
        return ConformaGateDecision(
            gate=CONFORMA_GATE_SKIP,
            reason="conforma_failed",
            conforma_runs=tuple(names),
            note=f"Skipped: conforma failed ({joined}{extra}) — e2e smoke not run",
        )
    return ConformaGateDecision(
        gate=CONFORMA_GATE_PASS,
        reason="conforma_passed",
        conforma_runs=tuple(names),
        note="",
    )


def should_wait_for_conforma(
    *,
    wait_for_conforma: str,
    product: str,
    snapshot_name: str,
) -> bool:
    if (wait_for_conforma or "true").strip().lower() not in ("1", "true", "yes"):
        return False

    if is_test_only_product(product):
        return False
    return bool((snapshot_name or "").strip())


def decide_conforma_gate_without_wait(
    *,
    wait_for_conforma: str,
    product: str,
    snapshot_name: str,
) -> ConformaGateDecision:
    is_explicit_bypass = (
        (wait_for_conforma or "true").strip().lower() not in ("1", "true", "yes")
        or is_test_only_product(product)
    )
    if is_explicit_bypass:
        return ConformaGateDecision(
            gate=CONFORMA_GATE_PASS,
            reason="gate_disabled",
            note="",
        )
    if not (snapshot_name or "").strip():
        return ConformaGateDecision(
            gate=CONFORMA_GATE_SKIP,
            reason="no_snapshot",
            note="Skipped: conforma wait requires a Snapshot name — e2e smoke not run",
        )
    return ConformaGateDecision(
        gate=CONFORMA_GATE_PASS,
        reason="gate_disabled",
        note="",
    )


def poll_conforma_gate(
    *,
    snapshot_name: str,
    list_runs: Callable[[str], list[dict[str, Any]]],
    timeout_sec: int,
    sleep_fn: Callable[[float], None] = time.sleep,
    monotonic_fn: Callable[[], float] = time.monotonic,
    list_errors: list[str] | None = None,
) -> ConformaGateDecision:
    snap = (snapshot_name or "").strip()
    if not snap:
        return ConformaGateDecision(
            gate=CONFORMA_GATE_SKIP,
            reason="no_snapshot",
            note="Skipped: conforma wait requires a Snapshot name — e2e smoke not run",
        )
    deadline = monotonic_fn() + max(int(timeout_sec), 1)
    last_count = -1
    last_list_error = ""
    had_successful_list = False
    while monotonic_fn() < deadline:
        if list_errors is not None:
            list_errors.clear()
        items = list_runs(snap)
        if list_errors:
            errs = [e for e in list_errors if (e or "").strip()]
            if errs:
                last_list_error = errs[-1]
                sleep_fn(_POLL_INTERVAL_SEC)
                continue
        had_successful_list = True
        last_list_error = ""
        decision = evaluate_conforma_runs(items)
        if decision is not None:
            return decision
        ec_count = sum(1 for x in items if is_enterprise_contract_pipelinerun(x))
        if ec_count != last_count:
            last_count = ec_count
        sleep_fn(_POLL_INTERVAL_SEC)
    if last_list_error and not had_successful_list:
        return ConformaGateDecision(
            gate=CONFORMA_GATE_SKIP,
            reason="pipelinerun_list_failed",
            note=f"Skipped: {last_list_error} — e2e smoke not run",
        )
    return ConformaGateDecision(
        gate=CONFORMA_GATE_SKIP,
        reason="conforma_timeout",
        note=f"Skipped: timed out waiting for conforma on snapshot {snap} — e2e smoke not run",
    )
