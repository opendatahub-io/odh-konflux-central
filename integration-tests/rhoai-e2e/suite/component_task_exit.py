"""Tekton run-step exit vs strict component-test.exit after JUnit is on disk."""

from __future__ import annotations

import json
import sys
from pathlib import Path

from suite.component_junit import junit_counts, junit_pass_rate


def component_exit_file_path(artifacts_dir: Path, component_id: str = "") -> Path:
    """Per-component exit marker in Tekton (avoids stale shared component-test.exit)."""
    cid = (component_id or "").strip()
    if cid:
        if "/" in cid or "\\" in cid or ".." in cid:
            raise ValueError(f"invalid component_id: {component_id!r}")
        return artifacts_dir / f"{cid}.component-test.exit"
    return artifacts_dir / "component-test.exit"


def _nonzero_exit(raw_ec: int) -> int:
    return raw_ec if raw_ec != 0 else 1


def component_from_plan(plan_path: Path, component_id: str) -> dict[str, str] | None:
    if not plan_path.is_file():
        return None
    try:
        plan_raw = json.loads(plan_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(plan_raw, dict):
        return None
    for item in plan_raw.get("components") or []:
        if not isinstance(item, dict):
            continue
        if str(item.get("id", "")).strip() == component_id:
            return {
                "id": component_id,
                "artifact_prefix": str(item.get("artifact_prefix", "")).strip(),
                "min_pass_rate_for_success": str(item.get("min_pass_rate_for_success", "")).strip(),
            }
    return None


def resolve_strict_exit_with_min_pass_rate(
    comp: dict[str, str],
    ec: int,
    artifacts_dir: Path,
) -> int:
    """Apply min_pass_rate_for_success when pytest failed but pass rate meets threshold."""
    if ec == 0:
        return 0
    min_raw = comp.get("min_pass_rate_for_success", "").strip()
    if not min_raw:
        return ec
    try:
        min_rate = float(min_raw)
    except ValueError:
        print(
            f"WARN: invalid min_pass_rate_for_success for {comp['id']!r}; using pytest exit {ec}",
            file=sys.stderr,
        )
        return ec
    if not 0.0 <= min_rate <= 1.0:
        print(
            f"WARN: out-of-range min_pass_rate_for_success for {comp['id']!r}; using pytest exit {ec}",
            file=sys.stderr,
        )
        return ec
    prefix = comp.get("artifact_prefix", "").strip()
    if not prefix:
        return ec
    rate = junit_pass_rate(artifacts_dir / f"{prefix}.xml")
    if rate is None:
        return ec
    pct = rate * 100.0
    threshold_pct = min_rate * 100.0
    if rate >= min_rate:
        print(
            f"Component {comp['id']!r} pass rate {pct:.1f}% >= {threshold_pct:.0f}% "
            f"— treating pytest exit {ec} as success",
            flush=True,
        )
        return 0
    return ec


def _tekton_step_exit(*, strict: int, passed: int) -> int:
    """Exit 0 only when at least one test passed (SUCCESS or WARNING in Konflux UI)."""
    if passed > 0:
        return 0
    return strict if strict != 0 else 1


def _no_artifact_exit(raw_ec: int) -> tuple[int, int]:
    if raw_ec == 0:
        return 0, 0
    strict = _nonzero_exit(raw_ec)
    return strict, strict


def _skip_only_hollow_exit(comp_id: str) -> tuple[int, int]:
    """0 passed with only synthetic/prereq skips — fail Tekton; pipeline continues via onError."""
    print(
        f"Component {comp_id!r}: hollow green (0 passed, skipped only) — failing Tekton step",
        flush=True,
    )
    return 1, 1


def resolve_junit_aggregate_exit(
    artifacts_dir: Path,
    xml_paths: tuple[Path, ...],
    *,
    raw_ec: int,
) -> tuple[int, int]:
    """Return ``(strict_exit, tekton_step_exit)`` for multi-suite gates (e.g. BVT)."""
    existing = tuple(p for p in xml_paths if p.is_file())
    if not existing:
        return _no_artifact_exit(raw_ec)

    passed = failed = skipped = 0
    parsed = False
    for path in existing:
        counts = junit_counts(path)
        if counts is None:
            continue
        parsed = True
        passed += counts["passed"]
        failed += counts["failures"] + counts["errors"]
        skipped += counts["skipped"]
    if not parsed:
        strict = _nonzero_exit(raw_ec)
        return strict, strict

    if passed > 0 and failed == 0:
        return 0, 0
    # BVT aggregate tolerates skip-only suites when other gates passed; single-component
    # smoke uses _skip_only_hollow_exit instead (see resolve_component_exit_codes).
    if passed == 0 and failed == 0 and skipped > 0:
        return 0, 0
    strict = _nonzero_exit(raw_ec)
    return strict, _tekton_step_exit(strict=strict, passed=passed)


def resolve_component_exit_codes(
    comp: dict[str, str],
    *,
    raw_ec: int,
    artifacts_dir: Path,
) -> tuple[int, int]:
    """Return ``(strict_exit, tekton_step_exit)`` for one component smoke task.

    Tekton run step exits 0 only when at least one test passed. Skip-only suites (version
    gate / prereq synthetic JUnit with 0 passed) fail the finalize step (red TaskRun) while
    summarize still publishes JUnit stats; pipeline ``onError: continue`` runs other components.
    ``strict_exit`` is stored in ``component-test.exit`` for test-finalize.
    """
    prefix = comp.get("artifact_prefix", "").strip()
    if not prefix:
        return _no_artifact_exit(raw_ec)

    xml_path = artifacts_dir / f"{prefix}.xml"
    if not xml_path.is_file():
        return _no_artifact_exit(raw_ec)

    counts = junit_counts(xml_path)
    if counts is None:
        strict = _nonzero_exit(raw_ec)
        return strict, strict

    passed = counts["passed"]
    failed = counts["failures"] + counts["errors"]
    skipped = counts["skipped"]
    if passed > 0 and failed == 0:
        return 0, 0
    if passed == 0 and failed == 0 and skipped > 0:
        return _skip_only_hollow_exit(comp.get("id", "?"))
    strict = resolve_strict_exit_with_min_pass_rate(
        comp,
        _nonzero_exit(raw_ec),
        artifacts_dir,
    )
    return strict, _tekton_step_exit(strict=strict, passed=passed)
