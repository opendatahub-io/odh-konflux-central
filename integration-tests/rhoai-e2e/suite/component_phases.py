"""Resolve which component-level test phases (smoke, tier1, …) run in one pytest session."""

from __future__ import annotations

# Phases executed per component (not BVT cluster/operator health).
COMPONENT_PHASE_IDS: frozenset[str] = frozenset({"smoke", "tier1"})


def parse_component_test_phases(raw: str) -> tuple[str, ...]:
    """Return ordered phase ids from a comma-separated TEST_GATES / COMPONENTS_TEST_PHASES string."""
    seen: list[str] = []
    for part in (raw or "").split(","):
        phase = part.strip().lower()
        if not phase or phase not in COMPONENT_PHASE_IDS:
            continue
        if phase not in seen:
            seen.append(phase)
    return tuple(seen)


def component_test_phases_from_flags(*, run_smoke: bool, run_tier1: bool) -> tuple[str, ...]:
    phases: list[str] = []
    if run_smoke:
        phases.append("smoke")
    if run_tier1:
        phases.append("tier1")
    return tuple(phases)


def combine_pytest_markers(
    phase_markers: dict[str, str],
    phases: tuple[str, ...],
    *,
    fallback_smoke_marker: str = "smoke",
) -> str:
    """Build one pytest -m expression for all selected phases (single framework invocation)."""
    if not phases:
        return fallback_smoke_marker
    parts: list[str] = []
    for phase in phases:
        raw = (phase_markers.get(phase) or "").strip()
        if not raw:
            if phase == "smoke":
                raw = fallback_smoke_marker
            elif phase == "tier1":
                raw = "tier1"
            else:
                continue
        if " or " in raw or " and " in raw or "(" in raw:
            parts.append(f"({raw})")
        else:
            parts.append(raw)
    if not parts:
        return fallback_smoke_marker
    if len(parts) == 1:
        return parts[0]
    return " or ".join(parts)
