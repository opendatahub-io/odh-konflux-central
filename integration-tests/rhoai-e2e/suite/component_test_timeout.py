"""Resolve per-component test timeouts for one or more selected gates (smoke, tier1, …)."""

from __future__ import annotations

import math
import os

from suite.its_trigger_params import CLUSTER_SOURCE_EPHC, is_ephemeral_hosted_cluster_source

# Subprocess timeout for pytest runs (component smoke and BVT health checks), in seconds.
COMPONENT_TEST_TIMEOUT_SECS_ENV = "COMPONENT_TEST_TIMEOUT_SECS"

# EPHC pipeline budget is 4h; cap long runners so tail tasks fail fast when cluster dies.
_EHC_COMPONENT_TIMEOUT_CAP_BY_ID: dict[str, str] = {
    # stable-3.5 platform smoke (aigateway group_4) needs >30m on EPHC; catalog is 45m.
    "platform": "45m",
    "mlflow": "15m",
    "ogx": "20m",
}


def _cluster_source_is_ephc() -> bool:
    source = os.environ.get("CLUSTER_SOURCE", "").strip()
    return is_ephemeral_hosted_cluster_source(source)


def apply_cluster_source_timeout_cap(*, component_id: str, timeout_raw: str) -> str:
    """Apply shorter per-component caps on EPHC without changing external-cluster catalog defaults."""
    if not _cluster_source_is_ephc():
        return timeout_raw
    cap_raw = _EHC_COMPONENT_TIMEOUT_CAP_BY_ID.get(component_id.strip())
    if not cap_raw:
        return timeout_raw
    base = (timeout_raw or "").strip()
    if not base:
        return cap_raw
    try:
        base_secs = parse_component_timeout_seconds(base)
        cap_secs = parse_component_timeout_seconds(cap_raw)
    except ValueError:
        return timeout_raw
    if base_secs is None:
        return cap_raw
    if cap_secs is None:
        return base
    return cap_raw if cap_secs < base_secs else base


def parse_component_timeout_seconds(raw: str) -> float | None:
    """Parse duration text like 10m, 90s, 1h30m, 1.5h; empty means no timeout."""
    s = (raw or "").strip()
    if not s:
        return None
    compact = s.replace(" ", "")
    if compact.replace(".", "", 1).isdigit():
        secs = float(compact)
        if secs <= 0:
            raise ValueError("component test timeout must be greater than zero.")
        return secs
    i = 0
    total = 0.0
    unit_mult = {"s": 1.0, "m": 60.0, "h": 3600.0, "d": 86400.0}
    while i < len(compact):
        j = i
        dot_seen = False
        while j < len(compact) and (compact[j].isdigit() or (compact[j] == "." and not dot_seen)):
            dot_seen = dot_seen or compact[j] == "."
            j += 1
        if j == i or j >= len(compact):
            raise ValueError("component test timeout must use duration tokens like 10m, 90s, 1h30m.")
        num = float(compact[i:j])
        unit = compact[j].lower()
        if num <= 0 or unit not in unit_mult:
            raise ValueError("component test timeout must use positive duration tokens like 10m or 90s.")
        total += num * unit_mult[unit]
        i = j + 1
    return total if total > 0 else None


def resolve_component_test_timeout_raw(
    *,
    phases: tuple[str, ...],
    component_default: str = "",
    component_by_gate: dict[str, str] | None = None,
    catalog_gate_defaults: dict[str, str] | None = None,
    cli_override: str = "",
) -> str:
    """Pick the longest timeout covering all active gates (single combined pytest/golang run).

    Precedence per gate: ``component_by_gate[gate]`` → ``catalog_gate_defaults[gate]``
    → ``component_default``. When multiple gates run together, the maximum duration wins.
    ``cli_override`` (ITS ``COMPONENT_TEST_TIMEOUT`` / ``--test-timeout``) sets a global floor;
    per-gate catalog/component values that are longer still win (maximum when multiple gates run).
    """
    cli = (cli_override or "").strip()
    by_gate = component_by_gate or {}
    defaults = catalog_gate_defaults or {}
    best_raw = ""
    best_secs: float | None = None

    for phase in phases:
        raw = (
            (by_gate.get(phase) or "").strip()
            or (defaults.get(phase) or "").strip()
            or (component_default or "").strip()
        )
        if not raw:
            continue
        try:
            secs = parse_component_timeout_seconds(raw)
        except ValueError:
            continue
        if secs is None:
            continue
        if best_secs is None or secs > best_secs:
            best_secs = secs
            best_raw = raw

    if not best_raw:
        return cli

    if not cli:
        return best_raw

    cli_secs = parse_component_timeout_seconds(cli)
    if cli_secs is None:
        return best_raw
    if best_secs is None or cli_secs > best_secs:
        return cli
    return best_raw


# Orchestrate/prep headroom before pytest/golang starts inside the Tekton task pod.
_PIPELINE_TASK_PREP_MARGIN_SECONDS = 600.0
_PIPELINE_TASK_TIMEOUT_HEADROOM_RATIO = 1.25


def pipeline_task_timeout_from_smoke(timeout_raw: str, *, floor_minutes: int = 15) -> str:
    """Derive Tekton PipelineTask timeout from catalog smoke duration (+ prep, headroom)."""
    base_secs = parse_component_timeout_seconds(timeout_raw)
    if base_secs is None:
        return f"{floor_minutes}m0s"
    total = (base_secs + _PIPELINE_TASK_PREP_MARGIN_SECONDS) * _PIPELINE_TASK_TIMEOUT_HEADROOM_RATIO
    minutes = max(floor_minutes, math.ceil(total / 60))
    return f"{minutes}m0s"
