#!/usr/bin/env python3
"""Parse JUnit XML files and write a Konflux-standardised TEST_OUTPUT result.

Env (required):
    TEST_OUTPUT_PATH    -- Tekton result file for the JSON summary
    ARTIFACT_BROWSER_BASE -- base URL for the artifact browser (no trailing slash)
    PR_NAME             -- PipelineRun name
Env (optional):
    ARTIFACTS_URL_PATH  -- Tekton result file for the artifact browser URL (omit when
                           WRITE_ARTIFACTS_URL=false)
    ARTIFACT_BROWSER_REPO_PATH -- path segment in browser (default odh-ci-artifacts)
    ARTIFACTS_DIR       -- directory containing JUnit XML (default /artifacts)
    OCI_TAG_SUFFIX      -- gate subfolder name (e.g. bvt, smoke); empty reads root/recursive
    NOTE_PREFIX         -- prefix for the note field (default "BVT")
    COMPONENT_ID        -- when set, write TEST_OUTPUT for one smoke component only
    COMPONENT_TEST_PLAN_JSON -- plan path (required with COMPONENT_ID)
    WRITE_ARTIFACTS_URL  -- when false, skip ARTIFACTS_URL (upload happens in publish-results)
    JUNIT_RECURSIVE     -- when true, collect *.xml under ARTIFACTS_DIR recursively
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path

from _bootstrap import ensure_rhoai_e2e_path

ensure_rhoai_e2e_path()

from suite.component_junit import junit_counts
from suite.test_output_pass_rate import gate_test_output_with_pass_rate_result
from steps.tests_payload import (
    gate_test_output_sidecar_path,
    junit_xml_for_component,
    oci_upload_marker,
    resolve_tests_payload_root,
)
from runners.report.junit_suite_report import (
    format_suite_stats_compact,
    overall_pass_rate_note,
    parse_junit_suites,
    suite_display_name,
)
from runners.report.pipeline_test_outputs import konflux_list_warnings_count
from steps.tekton_util import parse_junit_summary, require_env, write_result

# Tekton step results / termination messages are capped at 4096 bytes.
_TEKTON_TEST_OUTPUT_MAX_BYTES = 3500

_BVT_HEALTH_SUITE_IDS = frozenset({"cluster-health", "operator-health"})


def _component_task_test_output_payload(payload: dict[str, object]) -> dict[str, object]:
    """Per-component TEST_OUTPUT for Konflux task Results and DAG badges.

    Keep ``failures`` for the badge; zero ``successes``, ``skipped``, and
    ``warnings`` so PipelineRun rollup does not double-count. Skips stay in
    ``note`` and ``suites``.
    """
    return {**payload, "successes": 0, "skipped": 0, "warnings": 0}


def _summary_from_suites(suites: list[dict[str, object]]) -> dict[str, int]:
    passed = sum(int(s.get("passed", 0)) for s in suites)
    failed = sum(int(s.get("failed", 0)) for s in suites)
    skipped = sum(int(s.get("skipped", 0)) for s in suites)
    total = sum(int(s.get("total", 0)) for s in suites) or (passed + failed + skipped)
    return {
        "total": total,
        "passed": passed,
        "failures": failed,
        "errors": 0,
        "skipped": skipped,
    }


def _truthy(name: str, *, default: bool = False) -> bool:
    raw = os.environ.get(name, "").strip().lower()
    if not raw:
        return default
    return raw in ("1", "true", "yes")


def _test_output_result(*, passed: int, failed: int, skipped: int = 0) -> str:
    """Map JUnit counts to Konflux TEST_OUTPUT result.

    SUCCESS when passed > 0 and failed == 0 (skips ignored). WARNING on partial
    pass. FAILURE otherwise. Aggregate gates may adjust via allow_skip_success or
    ``_apply_incomplete_gate_pass_penalty``; per-component tasks use this as-is.
    """
    if passed > 0 and failed == 0:
        return "SUCCESS"
    if passed > 0 and failed > 0:
        return "WARNING"
    return "FAILURE"


def _apply_incomplete_gate_pass_penalty(
    result: str,
    *,
    passed: int,
    failed: int,
    skipped: int,
) -> str:
    """Downgrade SUCCESS to WARNING when aggregate gate has skips and passed < total."""
    if result != "SUCCESS" or passed <= 0 or failed != 0 or skipped <= 0:
        return result
    total = passed + failed + skipped
    if passed < total:
        return "WARNING"
    return result


def resolve_junit_artifacts_dir(artifacts_dir: str | Path) -> Path:
    """Directory holding JUnit XML (gate subfolder after OCI upload staging, else task root)."""
    root = Path(artifacts_dir)
    gate = os.environ.get("OCI_TAG_SUFFIX", "").strip().lower()
    if gate:
        gated = root / gate
        if gated.is_dir() and any(gated.glob("*.xml")):
            return gated
    if any(root.glob("*.xml")):
        return root
    if gate and (root / gate).is_dir():
        return root / gate
    return root


def _component_order_from_plan(root: Path, artifacts_path: Path) -> list[str] | None:
    for plan_path in (
        root / "component-test-plan.json",
        artifacts_path / "component-test-plan.json",
        root / "component-smoke-plan.json",
        artifacts_path / "component-smoke-plan.json",
    ):
        if not plan_path.is_file():
            continue
        try:
            plan_raw = json.loads(plan_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        items = plan_raw.get("components") if isinstance(plan_raw, dict) else None
        if isinstance(items, list):
            order = [
                str(c.get("id", "")).strip()
                for c in items
                if isinstance(c, dict) and str(c.get("id", "")).strip()
            ]
            if order:
                return order
    return None


def _component_badge_failures(*, junit_failed: int, result: str) -> int:
    """Konflux DAG node color uses ``failures``; FAILURE with zero counts stays yellow."""
    if junit_failed > 0:
        return junit_failed
    if result == "FAILURE":
        return 1
    return 0


def build_single_component_test_output_payload(
    *,
    component_id: str,
    artifacts_dir: Path,
    plan_path: Path,
) -> tuple[dict[str, object], str]:
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    if component_id == "dashboard_cypress":
        from components.dashboard_cypress.runtime import ensure_dashboard_cypress_junit_collected

        ensure_dashboard_cypress_junit_collected(
            artifacts_dir=artifacts_dir,
            plan_path=plan_path,
            component_id=component_id,
        )
    xml_path = junit_xml_for_component(component_id, artifacts_dir, plan_path)
    if xml_path is None or not xml_path.is_file():
        note = f"{component_id}: infrastructure error - no JUnit file"
        return (
            _component_task_test_output_payload(
                {
                    "result": "FAILURE",
                    "timestamp": ts,
                    "failures": 1,
                    "warnings": 0,
                    "successes": 0,
                    "note": note,
                }
            ),
            note,
        )
    counts = junit_counts(xml_path)
    if counts is None:
        note = f"{component_id}: unreadable JUnit ({xml_path.name})"
        return (
            _component_task_test_output_payload(
                {
                    "result": "FAILURE",
                    "timestamp": ts,
                    "failures": 1,
                    "warnings": 0,
                    "successes": 0,
                    "note": note,
                }
            ),
            note,
        )
    failed = counts["failures"] + counts["errors"]
    suite = {
        "id": xml_path.stem,
        "name": suite_display_name(xml_path.stem),
        "total": counts["total"],
        "passed": counts["passed"],
        "failed": failed,
        "skipped": counts["skipped"],
    }
    note = f"{suite['name']}: {format_suite_stats_compact(suite)}"
    result = _test_output_result(
        passed=counts["passed"],
        failed=failed,
        skipped=counts["skipped"],
    )
    payload: dict[str, object] = {
        "result": result,
        "timestamp": ts,
        "failures": _component_badge_failures(junit_failed=failed, result=result),
        "warnings": konflux_list_warnings_count(skipped=counts["skipped"]),
        "successes": counts["passed"],
        "skipped": counts["skipped"],
        "note": note,
        "suites": [suite],
    }
    return _component_task_test_output_payload(payload), note


def build_test_output_payload(
    artifacts_dir: str | Path,
    *,
    note_prefix: str = "BVT",
    component_order: list[str] | None = None,
    component_id: str = "",
    plan_path: Path | None = None,
    recursive: bool = False,
) -> tuple[dict[str, object], str]:
    """Build Konflux TEST_OUTPUT JSON and summary note from JUnit under *artifacts_dir*."""
    if component_id:
        if plan_path is None:
            raise ValueError("COMPONENT_TEST_PLAN_JSON is required when COMPONENT_ID is set")
        return build_single_component_test_output_payload(
            component_id=component_id,
            artifacts_dir=Path(artifacts_dir),
            plan_path=plan_path,
        )

    root = Path(artifacts_dir)
    artifacts_path = resolve_junit_artifacts_dir(artifacts_dir)
    order = component_order if component_order is not None else _component_order_from_plan(root, artifacts_path)

    if recursive:
        s = parse_junit_summary(artifacts_path, recursive=True)
        suites = parse_junit_suites(artifacts_path, component_order=order or None, recursive=True)
    else:
        s = parse_junit_summary(artifacts_path)
        suites = parse_junit_suites(artifacts_path, component_order=order or None)

    prefix_upper = note_prefix.strip().upper()
    if prefix_upper == "COMPONENT" and suites:
        suites = [
            suite
            for suite in suites
            if str(suite.get("id", "")).strip() not in _BVT_HEALTH_SUITE_IDS
        ]
        s = _summary_from_suites(suites)

    failed_total = s["failures"] + s["errors"]
    result = _test_output_result(
        passed=s["passed"],
        failed=failed_total,
        skipped=s["skipped"],
    )
    # BVT health checks may skip entirely on external existing clusters, or skip optional
    # suites (e.g. operator_health absent from opendatahub-tests image). Component smoke
    # (golang KFTO, pytest model_runtime) may skip unselected tiers while executed tests pass.
    allow_skip_success = False
    if failed_total == 0 and s["skipped"] > 0:
        if prefix_upper == "BVT":
            if s["passed"] > 0:
                allow_skip_success = True
            elif suites:
                suite_ids = {
                    str(suite.get("id", "")).strip()
                    for suite in suites
                    if str(suite.get("id", "")).strip()
                }
                allow_skip_success = bool(suite_ids) and suite_ids <= _BVT_HEALTH_SUITE_IDS
        elif prefix_upper == "COMPONENT" and s["passed"] > 0:
            allow_skip_success = True
    if allow_skip_success:
        result = "SUCCESS"
    else:
        result = _apply_incomplete_gate_pass_penalty(
            result,
            passed=s["passed"],
            failed=failed_total,
            skipped=s["skipped"],
        )
    note = overall_pass_rate_note(note_prefix, suites) if suites else (
        f"{note_prefix.strip().rstrip(':').lower()}: "
        f"{s['passed']}/{s['total']} passed, {s['failures']} failed, "
        f"{s['errors']} errors, {s['skipped']} skipped"
    )
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    payload: dict[str, object] = {
        "result": result,
        "timestamp": ts,
        "failures": s["failures"] + s["errors"],
        "warnings": konflux_list_warnings_count(skipped=s["skipped"]),
        "successes": s["passed"],
        "skipped": s["skipped"],
        "note": note,
    }
    if suites:
        payload["suites"] = suites
    if _truthy("APPLY_TEST_FINALIZE_PASS_RATE") and prefix_upper == "COMPONENT":
        payload = gate_test_output_with_pass_rate_result(payload)
    return payload, note


def _shrink_test_output_payload(payload: dict[str, object]) -> dict[str, object]:
    """Drop per-suite details when JSON would exceed Tekton's result size limit."""
    def _payload_size(value: dict[str, object]) -> int:
        return len(json.dumps(value, separators=(",", ":")).encode("utf-8"))

    if _payload_size(payload) <= _TEKTON_TEST_OUTPUT_MAX_BYTES:
        return payload
    compact = {k: v for k, v in payload.items() if k != "suites"}
    note = str(compact.get("note", "")).strip()
    if note:
        compact["note"] = f"{note} (per-suite details omitted; see artifact browser)"
    if _payload_size(compact) <= _TEKTON_TEST_OUTPUT_MAX_BYTES:
        return compact
    compact["note"] = "per-suite details omitted; see artifact browser"
    if _payload_size(compact) > _TEKTON_TEST_OUTPUT_MAX_BYTES:
        compact.pop("note", None)
    return compact


def _write_test_output_json(
    artifacts_dir: str | Path,
    test_output_path: str,
    *,
    note_prefix: str = "BVT",
    component_order: list[str] | None = None,
    component_id: str = "",
    plan_path: Path | None = None,
    recursive: bool = False,
) -> tuple[str, str]:
    payload, note = build_test_output_payload(
        artifacts_dir,
        note_prefix=note_prefix,
        component_order=component_order,
        component_id=component_id,
        plan_path=plan_path,
        recursive=recursive,
    )
    payload = _shrink_test_output_payload(payload)
    output_json = json.dumps(payload, separators=(",", ":"))
    write_result(test_output_path, output_json)
    return note, output_json


def write_junit_test_output(
    artifacts_dir: str | Path,
    test_output_path: str,
    *,
    note_prefix: str = "BVT",
    component_order: list[str] | None = None,
    component_id: str = "",
    plan_path: Path | None = None,
    recursive: bool = False,
) -> str:
    """Write TEST_OUTPUT Tekton result from JUnit files (safe to call after each smoke component)."""
    note, _ = _write_test_output_json(
        artifacts_dir,
        test_output_path,
        note_prefix=note_prefix,
        component_order=component_order,
        component_id=component_id,
        plan_path=plan_path,
        recursive=recursive,
    )
    return note


def main() -> int:
    test_output_path = require_env("TEST_OUTPUT_PATH")
    browser_base = require_env("ARTIFACT_BROWSER_BASE")
    repo_path = os.environ.get("ARTIFACT_BROWSER_REPO_PATH", "odh-ci-artifacts").strip().strip("/")
    pr_name = require_env("PR_NAME")
    artifacts_dir = Path(os.environ.get("ARTIFACTS_DIR", "/artifacts").strip())
    note_prefix = os.environ.get("NOTE_PREFIX", "BVT").strip()
    component_id = os.environ.get("COMPONENT_ID", "").strip()
    plan_raw = os.environ.get("COMPONENT_TEST_PLAN_JSON", "").strip()
    plan_path = Path(plan_raw) if plan_raw else None
    write_artifacts_url = _truthy("WRITE_ARTIFACTS_URL", default=True)
    recursive = _truthy("JUNIT_RECURSIVE", default=False)

    note, output_json = _write_test_output_json(
        artifacts_dir,
        test_output_path,
        note_prefix=note_prefix,
        component_id=component_id,
        plan_path=plan_path,
        recursive=recursive,
    )
    if not component_id:
        sidecar = gate_test_output_sidecar_path(artifacts_dir, note_prefix=note_prefix)
        if sidecar is not None:
            sidecar.write_text(f"{output_json}\n", encoding="utf-8")
    print(note)

    if not write_artifacts_url:
        return 0

    from steps.write_artifacts_url import write_artifacts_url_result

    artifacts_url_path = require_env("ARTIFACTS_URL_PATH")
    url = write_artifacts_url_result(
        artifacts_url_path=artifacts_url_path,
        pr_name=pr_name,
        browser_base=browser_base,
        repo_path=repo_path,
        tests_payload_dir=resolve_tests_payload_root(artifacts_dir),
    )
    if not url and not oci_upload_marker(artifacts_dir).is_file():
        print("Artifacts: (OCI upload did not complete)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
