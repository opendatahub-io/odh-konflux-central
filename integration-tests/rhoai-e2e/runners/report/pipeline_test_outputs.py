"""Collect per-gate TEST_OUTPUT JSON for bvt/smoke gate checks and finalize UI."""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from runners.report.junit_suite_report import (
    _suites_from_test_output_obj,
    augment_publish_gate_note,
    build_publish_results_gate_summaries,
    format_test_outputs_for_ui,
    gate_line_in_note,
    order_suites,
    read_gate_sidecar,
    test_output_includes_combined_gates,
)
from suite.test_output_pass_rate import classify_result_by_pass_rate

# Workspace sidecar key (``by_gate["smoke"]``) and filename ``.rhoai-e2e-smoke-test-output.json``
# hold aggregated JUnit from every component ``test-*`` task for all selected component
# phases (smoke, tier1, …) — not smoke-only despite the historical name.
COMPONENT_AGGREGATE_GATE_KEY = "smoke"

# ``TEST_GATES`` values that share the component aggregate sidecar today. ``tier1`` does not
# have its own sidecar yet; tier1 tests are included in ``COMPONENT_AGGREGATE_GATE_KEY``.
COMPONENT_AGGREGATE_REQUEST_GATES = frozenset({"smoke", "tier1"})


def component_aggregate_requested(test_gates: str) -> bool:
    requested = {part.strip().lower() for part in test_gates.split(",") if part.strip()}
    return bool(requested.intersection(COMPONENT_AGGREGATE_REQUEST_GATES))


def gates_from_test_gates_csv(test_gates: str) -> list[str]:
    requested = {part.strip().lower() for part in test_gates.split(",") if part.strip()}
    return [gate for gate in ("bvt", "smoke") if gate in requested]


def konflux_list_warnings_count(*, skipped: int = 0, warnings: int = 0) -> int:
    """Konflux PipelineRun list yellow icon aggregates ``warnings``, not ``skipped``."""
    return int(skipped or 0) + int(warnings or 0)


def _read_json_file(path: str) -> str:
    if not path:
        return ""
    try:
        return Path(path).read_text(encoding="utf-8").strip()
    except OSError:
        return ""


def gate_output_activity_score(data: dict[str, object]) -> int:
    """How many test cases a gate TEST_OUTPUT represents (for merge precedence)."""
    successes = int(data.get("successes", data.get("passed", 0)) or 0)
    failures = int(data.get("failures", 0) or 0)
    skipped = int(data.get("skipped", 0) or 0)
    total = successes + failures + skipped
    suites = data.get("suites")
    if isinstance(suites, list) and suites:
        suite_total = 0
        for item in suites:
            if not isinstance(item, dict):
                continue
            suite_total += int(item.get("total", 0) or 0) or (
                int(item.get("passed", 0) or 0)
                + int(item.get("failed", 0) or 0)
                + int(item.get("skipped", 0) or 0)
            )
        if suite_total > total:
            total = suite_total
    return total


def merge_gate_test_output(existing: str, candidate: str) -> str:
    """Keep the richer gate payload; never replace real results with empty sidecars."""
    if not candidate.strip():
        return existing
    if not existing.strip():
        return candidate
    cur = _parse_gate_json(existing)
    nxt = _parse_gate_json(candidate)
    if cur is None:
        return candidate
    if nxt is None:
        return existing
    if gate_output_activity_score(nxt) >= gate_output_activity_score(cur):
        return candidate
    return existing


def collect_bvt_smoke_outputs(
    taskruns: list[dict[str, Any]],
    *,
    list_from_taskruns,
    smoke_path: str = "",
    bvt_path: str = "",
) -> dict[str, str]:
    """Merge TaskRun results with workspace/env fallbacks.

    Workspace sidecars fill gaps when a gate step fails before results are visible in the
    API; richer TaskRun payloads win over empty placeholder sidecars.
    """
    by_gate: dict[str, str] = {gate: raw for gate, raw in list_from_taskruns(taskruns)}
    for env_name, gate in (("BVT_TEST_OUTPUT", "bvt"), ("SMOKE_TEST_OUTPUT", "smoke")):
        val = os.environ.get(env_name, "").strip()
        if val.lstrip().startswith("{"):
            by_gate[gate] = merge_gate_test_output(by_gate.get(gate, ""), val)
    bvt_text = _read_json_file(bvt_path)
    if bvt_text.lstrip().startswith("{"):
        by_gate["bvt"] = merge_gate_test_output(by_gate.get("bvt", ""), bvt_text)
    smoke_text = _read_json_file(smoke_path)
    if smoke_text.lstrip().startswith("{"):
        by_gate["smoke"] = merge_gate_test_output(by_gate.get("smoke", ""), smoke_text)
    return by_gate


def _parse_gate_json(raw: str) -> dict[str, object] | None:
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return None
    return data if isinstance(data, dict) else None


def _result_rank(result: str) -> int:
    normalized = result.strip().upper()
    if normalized == "FAILURE":
        return 0
    if normalized == "WARNING":
        return 1
    if normalized == "SUCCESS":
        return 2
    return -1


def build_combined_test_output_payload(
    outputs: list[tuple[str, str]],
) -> dict[str, object] | None:
    """Build one Konflux TEST_OUTPUT object with a combined bvt/smoke note for the UI."""
    if not outputs:
        return None
    parsed: list[tuple[str, dict[str, object]]] = []
    for gate, raw in outputs:
        data = _parse_gate_json(raw)
        if data is not None:
            parsed.append((gate, data))
    if not parsed:
        return None

    note = format_test_outputs_for_ui(outputs)
    worst = min(
        (str(data.get("result", "")).strip().upper() for _, data in parsed),
        key=_result_rank,
    )
    successes = sum(int(data.get("successes", data.get("passed", 0)) or 0) for _, data in parsed)
    failures = sum(int(data.get("failures", 0) or 0) for _, data in parsed)
    skipped = sum(int(data.get("skipped", 0) or 0) for _, data in parsed)
    warnings = sum(int(data.get("warnings", 0) or 0) for _, data in parsed)
    timestamp = next(
        (str(data.get("timestamp", "")).strip() for _, data in parsed if data.get("timestamp")),
        "",
    )
    payload: dict[str, object] = {
        "result": worst if _result_rank(worst) >= 0 else "FAILURE",
        "failures": failures,
        "warnings": konflux_list_warnings_count(skipped=skipped, warnings=warnings),
        "successes": successes,
        "skipped": skipped,
        "note": note,
    }
    if timestamp:
        payload["timestamp"] = timestamp
    else:
        payload["timestamp"] = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    merged_suites: list[dict[str, Any]] = []
    for gate, data in parsed:
        suites = _suites_from_test_output_obj(data)
        if not suites:
            continue
        if gate == "smoke":
            merged_suites.extend(order_suites(suites, None))
        else:
            merged_suites.extend(suites)
    if merged_suites:
        payload["suites"] = merged_suites
    return payload


def _gate_test_output_counts(data: dict[str, object]) -> tuple[int, int, int]:
    passed = int(data.get("successes", data.get("passed", 0)) or 0)
    failed = int(data.get("failures", 0) or 0)
    skipped = int(data.get("skipped", 0) or 0)
    return passed, failed, skipped


def _component_aggregate_counts(
    by_gate: dict[str, str] | None,
    payload: dict[str, object],
) -> tuple[int, int, int]:
    if by_gate:
        raw = by_gate.get(COMPONENT_AGGREGATE_GATE_KEY, "").strip()
        if raw:
            data = _parse_gate_json(raw)
            if data is not None:
                passed, failed, skipped = _gate_test_output_counts(data)
                if passed or failed or skipped:
                    return passed, failed, skipped
    return _gate_test_output_counts(payload)


def apply_test_finalize_display_result(
    payload: dict[str, object],
    *,
    by_gate: dict[str, str] | None = None,
    test_gates: str = "",
) -> dict[str, object]:
    """Rewrite ``result`` on test-finalize TEST_OUTPUT for Konflux node color only.

    Component aggregate (all ``test-*`` JUnit for smoke/tier1/…) uses pass-rate tiers.
    BVT keeps its existing result. Per-component TaskRuns and publish-results unchanged.
    """
    gates = gates_from_test_gates_csv(test_gates) if test_gates.strip() else []
    ranked: list[str] = []

    if by_gate and "bvt" in gates:
        bvt_data = _parse_gate_json(by_gate.get("bvt", ""))
        if bvt_data is not None:
            bvt_result = str(bvt_data.get("result", "")).strip().upper()
            if _result_rank(bvt_result) >= 0:
                ranked.append(bvt_result)

    if component_aggregate_requested(test_gates) or not test_gates.strip():
        passed, failed, skipped = _component_aggregate_counts(by_gate, payload)
        ranked.append(
            classify_result_by_pass_rate(passed=passed, failed=failed, skipped=skipped)
        )

    if not ranked:
        return payload

    out = dict(payload)
    out["result"] = min(ranked, key=_result_rank)
    return out


def build_finalize_test_output_from_taskruns(
    taskruns: list[dict[str, Any]],
    *,
    test_gates: str,
    smoke_path: str = "",
    bvt_path: str = "",
    list_from_taskruns: Callable[[list[dict[str, Any]]], list[tuple[str, str]]] | None = None,
) -> dict[str, object] | None:
    """Same payload merge as test-finalize ``write-combined-test-output``."""
    from runners.report.pipelinerun_summary import list_pipeline_test_outputs

    list_fn = list_from_taskruns or list_pipeline_test_outputs
    gates = gates_from_test_gates_csv(test_gates)
    if not gates:
        return None
    by_gate = collect_bvt_smoke_outputs(
        taskruns,
        list_from_taskruns=list_fn,
        smoke_path=smoke_path,
        bvt_path=bvt_path,
    )
    outputs = [(gate, by_gate[gate]) for gate in gates if gate in by_gate and by_gate[gate].strip()]
    smoke_raw = by_gate.get("smoke", "")
    if smoke_raw.strip() and test_output_includes_combined_gates(smoke_raw):
        outputs = [("combined", smoke_raw)]
    payload = build_combined_test_output_payload(outputs)
    if payload is None:
        return None
    summaries = build_publish_results_gate_summaries(
        combined_obj=payload,
        bvt_raw=by_gate.get("bvt", ""),
        smoke_raw=by_gate.get("smoke", ""),
        test_gates=test_gates,
    )
    out = dict(payload)
    out["note"] = augment_publish_gate_note(
        str(payload.get("note", "")),
        test_gates=test_gates,
        gate_summaries=summaries,
    )
    return out


def resolve_pipeline_test_output_text(
    taskruns: list[dict[str, Any]],
    *,
    test_gates: str = "",
    smoke_path: str = "",
    bvt_path: str = "",
) -> str | None:
    """Konflux TEST_OUTPUT JSON string for pipeline-level propagation."""
    from runners.report.pipelinerun_summary import list_pipeline_test_outputs, pick_pipeline_test_output

    payload = build_finalize_test_output_from_taskruns(
        taskruns,
        test_gates=test_gates,
        smoke_path=smoke_path,
        bvt_path=bvt_path,
        list_from_taskruns=lambda tr: list_pipeline_test_outputs(tr, for_ui=False),
    )
    if payload is not None:
        return json.dumps(payload, separators=(",", ":"))

    for _gate, raw in list_pipeline_test_outputs(taskruns, for_ui=True):
        if raw.lstrip().startswith("{"):
            return raw

    picked = pick_pipeline_test_output(taskruns)
    return picked or None


def konflux_failure_test_output_json(*, note: str) -> str:
    """Minimal valid Konflux TEST_OUTPUT when only a failure reason is known."""
    payload = {
        "result": "FAILURE",
        "timestamp": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "successes": 0,
        "failures": 0,
        "warnings": 0,
        "note": note,
    }
    return json.dumps(payload, separators=(",", ":"))


def konflux_conforma_skip_test_output_json(*, note: str) -> str:
    """WARNING TEST_OUTPUT when conforma failed and rhoai-e2e e2e was intentionally skipped."""
    payload = {
        "result": "WARNING",
        "timestamp": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "successes": 0,
        "failures": 0,
        "warnings": konflux_list_warnings_count(warnings=1),
        "skipped": 0,
        "note": (note or "Skipped: conforma failed — e2e smoke not run").strip()[:3000],
    }
    return json.dumps(payload, separators=(",", ":"))


def konflux_publish_success_test_output_json(*, note: str = "") -> str:
    """Minimal Konflux TEST_OUTPUT for a successful publish-results task (green DAG node)."""
    payload = {
        "result": "SUCCESS",
        "timestamp": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "successes": 0,
        "failures": 0,
        "warnings": 0,
        "note": note or "Results published",
    }
    return json.dumps(payload, separators=(",", ":"))


def build_publish_task_test_output(payload: dict[str, object]) -> dict[str, object]:
    """Minimal SUCCESS TEST_OUTPUT for publish-results (green DAG; stats live in gate results + note)."""
    note = str(payload.get("note", "")).strip()
    timestamp = str(payload.get("timestamp", "")).strip()
    out: dict[str, object] = {
        "result": "SUCCESS",
        "successes": 0,
        "failures": 0,
        "warnings": 0,
        "skipped": 0,
        "note": note or "Results published",
    }
    if timestamp:
        out["timestamp"] = timestamp
    else:
        out["timestamp"] = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    return out


def publish_results_test_output_payload(
    payload: dict[str, object] | None,
    *,
    test_gates: str = "",
    bvt_raw: str = "",
    smoke_raw: str = "",
) -> dict[str, object]:
    """Gate note + SUCCESS counts for publish-results (Konflux colors on ``result`` only)."""
    if payload is None:
        return build_publish_task_test_output({"note": "Results published"})
    summaries = build_publish_results_gate_summaries(
        combined_obj=payload,
        bvt_raw=bvt_raw,
        smoke_raw=smoke_raw,
        test_gates=test_gates,
    )
    note = augment_publish_gate_note(
        str(payload.get("note", "")),
        test_gates=test_gates,
        gate_summaries=summaries,
    )
    merged = dict(payload)
    merged["note"] = note
    return build_publish_task_test_output(merged)


def publish_results_test_output_json(
    payload: dict[str, object] | None,
    *,
    test_gates: str = "",
    bvt_raw: str = "",
    smoke_raw: str = "",
) -> str:
    from steps.tekton_util import slim_test_output_for_tekton

    publish_payload = publish_results_test_output_payload(
        payload,
        test_gates=test_gates,
        bvt_raw=bvt_raw,
        smoke_raw=smoke_raw,
    )
    return slim_test_output_for_tekton(
        json.dumps(publish_payload, separators=(",", ":")),
    )


def publish_results_test_output_json_from_raw(
    raw: str,
    *,
    test_gates: str = "",
    bvt_path: str = "",
    smoke_path: str = "",
) -> str:
    """Rewrite combined gate JSON as publish-results SUCCESS TEST_OUTPUT."""
    text = (raw or "").strip()
    if not text:
        return konflux_failure_test_output_json(note="no TEST_OUTPUT")
    payload: dict[str, object] | None = None
    if text.lstrip().startswith("{"):
        try:
            parsed = json.loads(text)
        except json.JSONDecodeError:
            return text
        if isinstance(parsed, dict):
            payload = parsed
    if payload is None:
        return text
    note = str(payload.get("note", ""))
    bvt_raw = read_gate_sidecar(bvt_path)
    smoke_raw = read_gate_sidecar(smoke_path)
    has_gate_data = (
        gate_line_in_note(note, "bvt")
        or gate_line_in_note(note, "smoke")
        or bvt_raw.lstrip().startswith("{")
        or smoke_raw.lstrip().startswith("{")
    )
    if not has_gate_data:
        return text
    return publish_results_test_output_json(
        payload,
        test_gates=test_gates,
        bvt_raw=bvt_raw,
        smoke_raw=smoke_raw,
    )


def combined_test_output_from_sidecars(
    *,
    test_gates: str = "",
    smoke_path: str = "",
    bvt_path: str = "",
) -> dict[str, object] | None:
    """Rebuild combined bvt/smoke TEST_OUTPUT from workspace sidecars."""
    return build_finalize_test_output_from_taskruns(
        [],
        test_gates=test_gates,
        smoke_path=smoke_path,
        bvt_path=bvt_path,
    )
