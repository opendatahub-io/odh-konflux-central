"""DSC baseline snapshot and post-component drift detection.

After global prep captures the intended DSC spec.components state as a baseline,
each component task compares, fails-on-drift (when attributable), and restores
before the next task. Hygiene also waits when baseline-Managed Ready conditions
are not True (``Removed`` lag or ``DeploymentsNotReady`` flaps). Hygiene runs
from finalize-component-exit for all runners and before pytest/golang orchestrate.
"""

from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path
from typing import Any

_BASELINE_FILENAME = ".dsc-baseline.json"
_DRIFT_PREFIX = ".dsc-drift-"


def _oc_get_dsc_components_spec() -> dict[str, Any]:
    """Fetch spec.components from default-dsc as a dict."""
    from install.dsc_install import dsc_resource_kind, oc_run

    r = oc_run(
        [
            "get",
            dsc_resource_kind(),
            "default-dsc",
            "-o",
            "jsonpath={.spec.components}",
        ],
        check=False,
        capture_output=True,
        timeout=30,
    )
    if r.returncode != 0 or not (r.stdout or "").strip():
        return {}
    try:
        return json.loads(r.stdout.strip())
    except json.JSONDecodeError:
        return {}


def _baseline_path(artifacts_dir: Path) -> Path:
    return artifacts_dir / _BASELINE_FILENAME


def _extract_management_states(spec: dict[str, Any]) -> dict[str, str]:
    states: dict[str, str] = {}
    for key, value in spec.items():
        if isinstance(value, dict):
            states[key] = value.get("managementState", "")
    return states


def capture_dsc_baseline(artifacts_dir: Path) -> dict[str, Any]:
    """Snapshot DSC spec.components after global prep; returns the captured state."""
    spec = _oc_get_dsc_components_spec()
    if not spec:
        print(
            "WARN: could not capture DSC baseline (empty spec.components)",
            file=sys.stderr,
            flush=True,
        )
        return {}
    path = _baseline_path(artifacts_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(spec, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    mgmt = _extract_management_states(spec)
    managed = sorted(k for k, v in mgmt.items() if v == "Managed")
    print(
        f"✓ DSC baseline captured ({len(mgmt)} components, {len(managed)} Managed)",
        flush=True,
    )
    return spec


def load_dsc_baseline(artifacts_dir: Path) -> dict[str, Any] | None:
    """Load saved baseline; None if not captured."""
    path = _baseline_path(artifacts_dir)
    if not path.is_file():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def check_dsc_drift(artifacts_dir: Path) -> list[str]:
    """Compare current DSC spec.components managementState against baseline.

    Returns list of human-readable drift descriptions (empty = no drift or no baseline).
    """
    baseline = load_dsc_baseline(artifacts_dir)
    if baseline is None:
        return []
    current = _oc_get_dsc_components_spec()
    if not current:
        return []
    baseline_states = _extract_management_states(baseline)
    current_states = _extract_management_states(current)
    drifts: list[str] = []
    for key in sorted(set(baseline_states) | set(current_states)):
        orig = baseline_states.get(key, "")
        now = current_states.get(key, "")
        if orig and now and orig != now:
            drifts.append(f"{key}: {orig}\u2192{now}")
    return drifts


def _drift_dsc_key(drift: str) -> str:
    return drift.split(":", 1)[0].strip()


def _managed_dsc_keys_for_component(component_id: str) -> set[str]:
    from install.dsc_install import _dsc_smoke_managed_components, _resolve_operator_version_for_dsc

    return set(
        _dsc_smoke_managed_components(
            component_id,
            operator_version=_resolve_operator_version_for_dsc(),
        )
    )


def filter_drifts_for_component(component_id: str, drifts: list[str]) -> list[str]:
    """Return drift lines for DSC keys this smoke component owns (fail attribution)."""
    if not drifts:
        return []
    managed = _managed_dsc_keys_for_component(component_id)
    if not managed:
        return []
    return [line for line in drifts if _drift_dsc_key(line) in managed]


def _drift_marker_path(artifacts_dir: Path, component_id: str) -> Path:
    return artifacts_dir / f"{_DRIFT_PREFIX}{component_id}.json"


def write_dsc_drift_marker(
    artifacts_dir: Path, component_id: str, drifts: list[str]
) -> None:
    """Persist drift details for write-task-message and finalize to consume."""
    path = _drift_marker_path(artifacts_dir, component_id)
    path.write_text(
        json.dumps({"component_id": component_id, "drifts": drifts}) + "\n",
        encoding="utf-8",
    )


def read_dsc_drift_marker(artifacts_dir: Path, component_id: str) -> list[str]:
    """Read drift details written after a component test; empty = no drift marker."""
    path = _drift_marker_path(artifacts_dir, component_id)
    if not path.is_file():
        return []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    return data.get("drifts", [])


def restore_dsc_from_baseline(artifacts_dir: Path) -> bool:
    """Patch DSC spec.components back to baseline state. Returns True on success."""
    baseline = load_dsc_baseline(artifacts_dir)
    if baseline is None:
        return False
    from install.dsc_install import dsc_resource_kind, oc_run, reset_dsc_resource_kind_cache

    patch_doc = json.dumps({"spec": {"components": baseline}})
    kind = dsc_resource_kind()
    r = oc_run(
        [
            "patch",
            kind,
            "default-dsc",
            "--type=merge",
            "-p",
            patch_doc,
        ],
        check=False,
        capture_output=True,
        timeout=60,
    )
    if r.returncode != 0:
        reset_dsc_resource_kind_cache()
        retry_kind = dsc_resource_kind(force_refresh=True)
        if retry_kind != kind:
            r = oc_run(
                [
                    "patch",
                    retry_kind,
                    "default-dsc",
                    "--type=merge",
                    "-p",
                    patch_doc,
                ],
                check=False,
                capture_output=True,
                timeout=60,
            )
        if r.returncode != 0:
            err = (r.stderr or r.stdout or "").strip()
            print(f"WARN: DSC restore from baseline failed: {err}", file=sys.stderr, flush=True)
            return False
    print("\u2713 DSC restored to baseline", flush=True)
    return True


def wait_for_baseline_spec(
    artifacts_dir: Path,
    *,
    timeout_sec: int | None = None,
) -> bool:
    """Poll until spec.components managementState matches baseline (post-restore)."""
    if load_dsc_baseline(artifacts_dir) is None:
        return True
    timeout = timeout_sec
    if timeout is None:
        timeout = int(os.environ.get("OLMINSTALL_DSC_RESTORE_WAIT_SEC", "120"))
    deadline = time.time() + timeout
    while time.time() < deadline:
        if not check_dsc_drift(artifacts_dir):
            print("\u2713 DSC spec matches baseline after restore", flush=True)
            return True
        time.sleep(5)
    print(
        f"WARN: DSC spec still drifted from baseline after {timeout}s restore wait",
        file=sys.stderr,
        flush=True,
    )
    return False


def check_baseline_managed_ready_stale(artifacts_dir: Path) -> set[str]:
    """Baseline-Managed DSC keys whose Ready condition is not True.

    Covers post-enable lag (``reason=Removed``) and runtime flaps such as
    ``DeploymentsNotReady`` that leave ``phase: Not Ready`` without
    ``spec.components`` managementState drift.
    """
    baseline = load_dsc_baseline(artifacts_dir)
    if baseline is None:
        return set()
    from suite.component_dsc_gate import _DSC_KEY_READY_CONDITION, _dsc_condition

    stale: set[str] = set()
    for key, state in _extract_management_states(baseline).items():
        if state != "Managed":
            continue
        ready_type = _DSC_KEY_READY_CONDITION.get(key)
        if not ready_type:
            continue
        status, _reason, _message = _dsc_condition(ready_type)
        if status != "True":
            stale.add(key)
    return stale


def _dsc_reconcile_wait_timeout_sec() -> int:
    """Default 600s; cap lower on EPHC so dead guests do not burn 10m per component."""
    raw = os.environ.get("OLMINSTALL_DSC_RECONCILE_WAIT_SEC", "600").strip()
    try:
        timeout = int(raw)
    except ValueError:
        timeout = 600
    from suite.its_trigger_params import is_ephemeral_hosted_cluster_source

    source = os.environ.get("CLUSTER_SOURCE", "").strip()
    if not is_ephemeral_hosted_cluster_source(source):
        return timeout
    cap_raw = os.environ.get("OLMINSTALL_EPHC_DSC_RECONCILE_WAIT_SEC", "120").strip()
    try:
        cap = int(cap_raw)
    except ValueError:
        cap = 120
    if cap <= 0:
        return timeout
    return min(timeout, cap)


def wait_for_ready_reconcile(
    dsc_keys: set[str],
    *,
    timeout_sec: int | None = None,
) -> bool:
    """Poll until DSC Ready conditions for *dsc_keys* reach status=True."""
    from suite.component_dsc_gate import _DSC_KEY_READY_CONDITION, _dsc_condition

    ready_types: list[str] = []
    for key in sorted(dsc_keys):
        ready_type = _DSC_KEY_READY_CONDITION.get(key)
        if ready_type and ready_type not in ready_types:
            ready_types.append(ready_type)
    if not ready_types:
        return True
    timeout = timeout_sec if timeout_sec is not None else _dsc_reconcile_wait_timeout_sec()
    deadline = time.time() + timeout
    pending = set(ready_types)
    while time.time() < deadline:
        from suite.cluster_api_health import cluster_api_unreachable_reason

        api_dead = cluster_api_unreachable_reason()
        if api_dead:
            from steps.cluster_prep_state import mark_cluster_api_unreachable

            mark_cluster_api_unreachable(api_dead)
            print(
                f"WARN: DSC Ready wait aborted ({api_dead})",
                file=sys.stderr,
                flush=True,
            )
            return False
        pending = {rt for rt in pending if _dsc_condition(rt)[0] != "True"}
        if not pending:
            print(
                f"\u2713 DSC Ready reconciled after restore: {', '.join(ready_types)}",
                flush=True,
            )
            return True
        time.sleep(12)
    print(
        f"WARN: DSC Ready still pending after {timeout}s: {', '.join(sorted(pending))}",
        file=sys.stderr,
        flush=True,
    )
    return False


def _restore_and_reconcile_dsc(
    artifacts_dir: Path,
    *,
    all_drifts: list[str],
    stale_keys: set[str],
    skip_reconcile_if_restore_failed: bool = False,
) -> None:
    """Restore baseline spec and poll Ready for drifted or stale-Managed keys."""
    reconcile_keys = {_drift_dsc_key(line) for line in all_drifts} | stale_keys
    restore_ok = True
    if all_drifts:
        restore_ok = restore_dsc_from_baseline(artifacts_dir)
        if restore_ok:
            wait_for_baseline_spec(artifacts_dir)
    elif stale_keys:
        restore_ok = restore_dsc_from_baseline(artifacts_dir)
    if not reconcile_keys:
        return
    if skip_reconcile_if_restore_failed and not restore_ok:
        print(
            "NOTE: skip DSC Ready reconcile wait after finalize "
            "(baseline restore failed; reconcile-before-pytest unchanged)",
            flush=True,
        )
        return
    wait_for_ready_reconcile(reconcile_keys)


def reconcile_baseline_dsc_before_component(
    component_id: str,
    artifacts_dir: Path,
) -> None:
    """Before orchestrate prereq gate: fix baseline spec drift and stale Ready conditions."""
    from suite.cluster_api_health import cluster_smoke_infra_blocked_reason

    if cluster_smoke_infra_blocked_reason():
        return
    try:
        all_drifts = check_dsc_drift(artifacts_dir)
        stale_keys = check_baseline_managed_ready_stale(artifacts_dir)
    except Exception as exc:
        print(
            f"WARN: pre-orchestrate DSC reconcile for {component_id} failed ({exc})",
            file=sys.stderr,
            flush=True,
        )
        return
    if not all_drifts and not stale_keys:
        return
    if all_drifts:
        print(
            f"NOTE: DSC spec drift before {component_id}: {'; '.join(all_drifts)}",
            flush=True,
        )
    elif stale_keys:
        print(
            f"NOTE: DSC Ready stale before {component_id} (spec Managed): "
            f"{', '.join(sorted(stale_keys))}",
            flush=True,
        )
    try:
        _restore_and_reconcile_dsc(
            artifacts_dir,
            all_drifts=all_drifts,
            stale_keys=stale_keys,
        )
    except Exception as exc:
        print(
            f"WARN: DSC restore before {component_id} failed ({exc})",
            file=sys.stderr,
            flush=True,
        )


def finalize_component_dsc_hygiene(
    component_id: str,
    artifacts_dir: Path,
) -> list[str]:
    """Post-component DSC check for all runners: restore always; fail only attributable drift."""
    try:
        all_drifts = check_dsc_drift(artifacts_dir)
        stale_keys = check_baseline_managed_ready_stale(artifacts_dir)
    except Exception as exc:
        print(
            f"WARN: DSC drift check for {component_id} failed ({exc})",
            file=sys.stderr,
            flush=True,
        )
        return []
    if not all_drifts and not stale_keys:
        return []

    if all_drifts:
        print(
            f"\u26a0 DSC drift after {component_id}: {'; '.join(all_drifts)}",
            flush=True,
        )
    elif stale_keys:
        print(
            f"NOTE: DSC Ready stale after {component_id} (spec Managed): "
            f"{', '.join(sorted(stale_keys))}",
            flush=True,
        )
    attributable = filter_drifts_for_component(component_id, all_drifts)
    if all_drifts and not attributable:
        print(
            f"NOTE: DSC drift not attributed to {component_id} managed keys; restoring only",
            flush=True,
        )
    try:
        _restore_and_reconcile_dsc(
            artifacts_dir,
            all_drifts=all_drifts,
            stale_keys=stale_keys,
            skip_reconcile_if_restore_failed=True,
        )
    except Exception as exc:
        print(
            f"WARN: DSC restore after {component_id} failed ({exc})",
            file=sys.stderr,
            flush=True,
        )
    if attributable:
        write_dsc_drift_marker(artifacts_dir, component_id, attributable)
    return attributable
