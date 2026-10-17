#!/usr/bin/env python3
"""Per-suite JUnit statistics for publish-results / Slack (Jenkins-style breakdown)."""

from __future__ import annotations

import json
import re
from collections.abc import Iterable
from pathlib import Path
from typing import Any

from suite.component_junit import junit_counts

# artifact XML stem (without .xml) → display label
_SUITE_DISPLAY_NAMES: dict[str, str] = {
    "cluster-health": "Cluster Health",
    "operator-health": "Operator Health",
    "workbenches-smoke": "Workbenches",
    "ai-hub-smoke": "AI Hub",
    "model-registry-smoke": "Model Registry",
    "llama_stack-smoke": "Llama Stack",
    "model_server-smoke": "Model Server",
    "model_runtime-smoke": "Model Runtime",
    "maas_billing-smoke": "MaaS Billing",
    "ai_pipelines-smoke": "AI Pipelines",
    "kuberay-smoke": "KubeRay",
    "mlflow-smoke": "MLflow",
    "ogx-smoke": "OGX",
    "ai_safety-smoke": "AI Safety",
    "dashboard-cypress-smoke": "Dashboard Cypress",
}


def suite_display_name(artifact_stem: str) -> str:
    stem = artifact_stem.strip()
    if stem in _SUITE_DISPLAY_NAMES:
        return _SUITE_DISPLAY_NAMES[stem]
    label = stem.removesuffix("-smoke").replace("-", " ").replace("_", " ").strip()
    return label.title() if label else stem


_ARTIFACT_TO_COMPONENT: dict[str, str] = {
    "cluster-health": "cluster_health",
    "operator-health": "operator_health",
    "workbenches-smoke": "workbenches",
    # model_registry pytest uses junit_suite_name=ai-hub; both map to catalog id model_registry.
    "ai-hub-smoke": "model_registry",
    "model-registry-smoke": "model_registry",
    "llama_stack-smoke": "llama_stack",
    "model_server-smoke": "model_server",
    "model_runtime-smoke": "model_runtime",
    "maas_billing-smoke": "maas_billing",
    "ai_pipelines-smoke": "ai_pipelines",
    "kuberay-smoke": "kuberay",
    "mlflow-smoke": "mlflow",
    "ogx-smoke": "ogx",
    "ai_safety-smoke": "ai_safety",
    "dashboard-cypress-smoke": "dashboard_cypress",
}

# Tekton publish-results result names (must match rhoai-e2e-pipeline.yaml taskSpec.results).
SMOKE_CATALOG_COMPONENT_IDS: tuple[str, ...] = (
    "workbenches",
    "model_registry",
    "model_server",
    "model_runtime",
    "maas_billing",
    "ai_pipelines",
    "kuberay",
    "mlflow",
    "ogx",
    "ai_safety",
    "llama_stack",
    "dashboard_cypress",
)

NOT_RUN_LABEL = "N/A"
DISABLED_LABEL = "disabled"
NO_RESULTS_LABEL = "N/A (no results)"
GATE_NOT_RUN_SUMMARY = "N/A (not run)"


def _requested_bvt_smoke_gates(test_gates: str) -> frozenset[str]:
    requested = {part.strip().lower() for part in (test_gates or "").split(",") if part.strip()}
    return frozenset(gate for gate in ("bvt", "smoke") if gate in requested)


def format_gate_not_run_ui_line(gate: str) -> str:
    """``smoke: N/A (not run)`` for publish-results TASK_MESSAGE / TEST_OUTPUT.note."""
    return f"{gate.strip().lower()}: {GATE_NOT_RUN_SUMMARY}"


def gate_line_in_note(note: str, gate: str) -> bool:
    gate_key = gate.strip().lower()
    if not gate_key:
        return False
    for line in re.split(r"[;\n]", note or ""):
        if line.strip().lower().startswith(f"{gate_key}:"):
            return True
    return False


def tests_summary_from_gate_sidecars(
    *,
    bvt_raw: str = "",
    smoke_raw: str = "",
    test_gates: str = "",
) -> str:
    """Sum BVT + smoke sidecar counts (avoids double-count when propagate merged test-finalize + bvt)."""
    requested = _requested_bvt_smoke_gates(test_gates)
    passed = failed = skipped = 0
    for gate, raw in (("bvt", bvt_raw), ("smoke", smoke_raw)):
        if gate not in requested:
            continue
        text = (raw or "").strip()
        if not text.lstrip().startswith("{"):
            continue
        try:
            obj = json.loads(text)
        except json.JSONDecodeError:
            continue
        if not isinstance(obj, dict):
            continue
        p, f, s = counts_from_test_output_obj(obj)
        passed += p
        failed += f
        skipped += s
    if passed + failed + skipped <= 0:
        return ""
    return format_stats_line(passed=passed, failed=failed, skipped=skipped)


def test_output_includes_combined_gates(raw: str) -> bool:
    """True when TEST_OUTPUT note already has both bvt and smoke gate lines."""
    text = (raw or "").strip()
    if not text.lstrip().startswith("{"):
        return False
    try:
        obj = json.loads(text)
    except json.JSONDecodeError:
        return False
    if not isinstance(obj, dict):
        return False
    note = str(obj.get("note", ""))
    return gate_line_in_note(note, "bvt") and gate_line_in_note(note, "smoke")


def augment_publish_gate_note(
    note: str,
    *,
    test_gates: str,
    gate_summaries: dict[str, str],
) -> str:
    """Append ``smoke: N/A (not run)`` (etc.) when a requested gate has no stats."""
    lines = [ln.strip() for ln in (note or "").splitlines() if ln.strip()]
    for gate, result_key in (("bvt", "BVT_GATE"), ("smoke", "SMOKE_GATE")):
        if gate not in _requested_bvt_smoke_gates(test_gates):
            continue
        if gate_line_in_note(note, gate):
            continue
        summary = (gate_summaries.get(result_key) or "").strip()
        if summary and summary != GATE_NOT_RUN_SUMMARY and not is_gate_summary_placeholder(summary):
            continue
        lines.append(format_gate_not_run_ui_line(gate))
    return "\n".join(dedupe_lines(lines))


def order_suites(suites: list[dict[str, Any]], component_order: list[str] | None) -> list[dict[str, Any]]:
    """Sort *suites* by catalog/component run order when provided."""
    if not component_order:
        return sorted(suites, key=lambda s: str(s.get("id", "")))
    rank = {cid: i for i, cid in enumerate(component_order)}
    return sorted(
        suites,
        key=lambda s: (
            rank.get(_ARTIFACT_TO_COMPONENT.get(str(s.get("id", "")), ""), 999),
            str(s.get("id", "")),
        ),
    )


def parse_junit_suites(
    artifacts_dir: str | Path,
    *,
    component_order: list[str] | None = None,
    recursive: bool = False,
) -> list[dict[str, Any]]:
    """One entry per JUnit XML file under *artifacts_dir* (sorted by name)."""
    from suite.component_junit import is_intermediate_cypress_junit

    root = Path(artifacts_dir)
    if not root.is_dir():
        return []
    xml_iter = sorted(root.rglob("*.xml")) if recursive else sorted(root.glob("*.xml"))
    suites: list[dict[str, Any]] = []
    for xml_path in xml_iter:
        if recursive and is_intermediate_cypress_junit(xml_path, root):
            continue
        counts = junit_counts(xml_path)
        if counts is None:
            continue
        failed = counts["failures"] + counts["errors"]
        suites.append(
            {
                "id": xml_path.stem,
                "name": suite_display_name(xml_path.stem),
                "total": counts["total"],
                "passed": counts["passed"],
                "failed": failed,
                "skipped": counts["skipped"],
            }
        )
    return order_suites(suites, component_order)


def format_jenkins_suite_line(suite: dict[str, Any]) -> str:
    """``• Workbenches (4 passed, 1 failed, 0 skipped)`` — matches Jenkins Slack style."""
    name = str(suite.get("name", "Suite"))
    return f"\u2022 {name} ({format_suite_stats_compact(suite)})"


def format_suite_stats_compact(suite: dict[str, Any]) -> str:
    """``4 passed, 1 failed, 0 skipped`` for Konflux per-component Results fields."""
    passed = int(suite.get("passed", 0))
    failed = int(suite.get("failed", 0))
    skipped = int(suite.get("skipped", 0))
    return f"{passed} passed, {failed} failed, {skipped} skipped"


def suites_by_component_id(raw: str) -> dict[str, dict[str, Any]]:
    """Map smoke catalog component id → suite dict from ``TEST_OUTPUT`` JSON."""
    if not raw.strip():
        return {}
    try:
        obj = json.loads(raw)
    except json.JSONDecodeError:
        return {}
    if not isinstance(obj, dict):
        return {}
    suites = obj.get("suites")
    if not isinstance(suites, list):
        return {}
    out: dict[str, dict[str, Any]] = {}
    for item in suites:
        if not isinstance(item, dict):
            continue
        suite_id = str(item.get("id", "")).strip()
        cid = _ARTIFACT_TO_COMPONENT.get(suite_id)
        if cid and cid not in out:
            out[cid] = item
    return out


def smoke_component_result_label(
    cid: str,
    *,
    smoke_in_gates: bool,
    selected_ids: frozenset[str],
    disabled_ids: frozenset[str],
    suite: dict[str, Any] | None,
) -> str:
    """Human-readable publish-results value for one smoke catalog component."""
    if not smoke_in_gates:
        return NOT_RUN_LABEL
    if cid in disabled_ids:
        return DISABLED_LABEL
    if cid not in selected_ids:
        return NOT_RUN_LABEL
    if suite:
        total = int(suite.get("total", 0))
        if total > 0:
            return format_suite_stats_compact(suite)
    return NO_RESULTS_LABEL


def smoke_component_result_lines(
    raw_test_output: str,
    *,
    smoke_in_gates: bool = True,
    selected_ids: frozenset[str] | None = None,
    disabled_ids: frozenset[str] | None = None,
) -> list[str]:
    """``workbenches: 5 passed, 0 failed, 0 skipped`` lines for consolidated UI summaries."""
    selected = selected_ids if selected_ids is not None else frozenset(SMOKE_CATALOG_COMPONENT_IDS)
    disabled = disabled_ids if disabled_ids is not None else frozenset()
    by_component = suites_by_component_id(raw_test_output)
    lines: list[str] = []
    for cid in SMOKE_CATALOG_COMPONENT_IDS:
        label = smoke_component_result_label(
            cid,
            smoke_in_gates=smoke_in_gates,
            selected_ids=selected,
            disabled_ids=disabled,
            suite=by_component.get(cid),
        )
        lines.append(f"{cid}: {label}")
    return lines


def write_smoke_component_tekton_results(
    raw_test_output: str,
    *,
    smoke_in_gates: bool = True,
    selected_ids: frozenset[str] | None = None,
    disabled_ids: frozenset[str] | None = None,
) -> list[str]:
    """Write one Tekton result per smoke catalog component (env ``COMPONENT_RESULT_PATH_<id>``)."""
    import os

    from steps.tekton_util import write_result

    lines = smoke_component_result_lines(
        raw_test_output,
        smoke_in_gates=smoke_in_gates,
        selected_ids=selected_ids,
        disabled_ids=disabled_ids,
    )
    if len(lines) != len(SMOKE_CATALOG_COMPONENT_IDS):
        raise ValueError(
            f"smoke component line count {len(lines)} != catalog {len(SMOKE_CATALOG_COMPONENT_IDS)}"
        )
    for cid, line in zip(SMOKE_CATALOG_COMPONENT_IDS, lines):
        path = os.environ.get(f"COMPONENT_RESULT_PATH_{cid}", "").strip()
        if not path:
            continue
        write_result(path, line.split(": ", 1)[1])
    summary_path = os.environ.get("SMOKE_COMPONENTS_PATH", "").strip()
    if summary_path and lines:
        write_result(summary_path, "\n".join(lines))
    return lines


def _normalize_newlines(text: str) -> str:
    return (text or "").replace("\\n", "\n")


def dedupe_lines(lines: Iterable[str]) -> list[str]:
    """Preserve order; drop blank and repeated lines."""
    seen: set[str] = set()
    out: list[str] = []
    for line in lines:
        text = line.strip()
        if not text or text in seen:
            continue
        seen.add(text)
        out.append(text)
    return out


def _health_check_suites(suites: list[dict[str, Any]]) -> bool:
    if not suites:
        return False
    return all(str(s.get("id", "")).startswith(("cluster-", "operator-")) for s in suites)


def format_stats_line(*, passed: int, failed: int, skipped: int) -> str:
    """``N passed, M failed, S skipped, T total (P% pass rate)`` for Konflux task Results."""
    total = passed + failed + skipped
    if total <= 0:
        return "no tests"
    pct = round(100.0 * passed / total)
    return f"{passed} passed, {failed} failed, {skipped} skipped, {total} total ({pct}% pass rate)"


def counts_from_test_output_obj(obj: dict[str, Any]) -> tuple[int, int, int]:
    """Return (passed, failed, skipped) from Konflux TEST_OUTPUT JSON."""
    if "successes" in obj or "failures" in obj or "skipped" in obj:
        return (
            int(obj.get("successes", 0)),
            int(obj.get("failures", 0)),
            int(obj.get("skipped", 0)),
        )
    suites = _suites_from_test_output_obj(obj)
    if suites:
        passed = sum(int(s.get("passed", 0)) for s in suites)
        failed = sum(int(s.get("failed", 0)) for s in suites)
        skipped = sum(int(s.get("skipped", 0)) for s in suites)
        return passed, failed, skipped
    return 0, 0, 0


def gate_summary_from_test_output_raw(raw: str) -> str:
    """One-line gate stats from TEST_OUTPUT JSON, or empty when unavailable."""
    text = (raw or "").strip()
    if not text.lstrip().startswith("{"):
        return ""
    try:
        obj = json.loads(text)
    except json.JSONDecodeError:
        return ""
    if not isinstance(obj, dict):
        return ""
    passed, failed, skipped = counts_from_test_output_obj(obj)
    if passed + failed + skipped <= 0:
        return ""
    return format_stats_line(passed=passed, failed=failed, skipped=skipped)


def gate_summary_from_combined_note(note: str, gate: str) -> str:
    """Parse ``bvt: 100% pass rate (9 passed, 0 failed, 0 skipped)`` from a combined note."""
    gate_key = gate.strip().lower()
    if not gate_key:
        return ""
    for line in re.split(r"[;\n]", note):
        text = line.strip()
        if not text.lower().startswith(f"{gate_key}:"):
            continue
        match = re.search(r"(\d+) passed, (\d+) failed, (\d+) skipped", text)
        if not match:
            continue
        return format_stats_line(
            passed=int(match.group(1)),
            failed=int(match.group(2)),
            skipped=int(match.group(3)),
        )
    return ""


_GATE_SUMMARY_PLACEHOLDERS = frozenset({"", "no tests", "n/a", "(unknown)"})


def is_gate_summary_placeholder(value: str) -> bool:
    return (value or "").strip().lower() in _GATE_SUMMARY_PLACEHOLDERS


def build_publish_results_gate_summaries(
    *,
    combined_raw: str = "",
    combined_obj: dict[str, Any] | None = None,
    bvt_raw: str = "",
    smoke_raw: str = "",
    test_gates: str = "",
) -> dict[str, str]:
    """Tekton result values for publish-results TESTS_SUMMARY / BVT_GATE / SMOKE_GATE."""
    out: dict[str, str] = {}
    obj = combined_obj
    if obj is None and combined_raw.strip():
        try:
            parsed = json.loads(combined_raw)
            obj = parsed if isinstance(parsed, dict) else None
        except json.JSONDecodeError:
            obj = None
    if isinstance(obj, dict):
        note = str(obj.get("note", "")).strip()
    else:
        note = ""
    sidecar_summary = tests_summary_from_gate_sidecars(
        bvt_raw=bvt_raw,
        smoke_raw=smoke_raw,
        test_gates=test_gates,
    )
    if sidecar_summary:
        out["TESTS_SUMMARY"] = sidecar_summary
    elif isinstance(obj, dict):
        passed, failed, skipped = counts_from_test_output_obj(obj)
        if passed + failed + skipped > 0:
            out["TESTS_SUMMARY"] = format_stats_line(passed=passed, failed=failed, skipped=skipped)
    bvt = gate_summary_from_test_output_raw(bvt_raw)
    if not bvt and note:
        bvt = gate_summary_from_combined_note(note, "bvt")
    if bvt:
        out["BVT_GATE"] = bvt
    smoke = gate_summary_from_test_output_raw(smoke_raw)
    if not smoke and note:
        smoke = gate_summary_from_combined_note(note, "smoke")
    if smoke:
        out["SMOKE_GATE"] = smoke
    if "BVT_GATE" not in out:
        out["BVT_GATE"] = GATE_NOT_RUN_SUMMARY
    if "SMOKE_GATE" not in out:
        out["SMOKE_GATE"] = GATE_NOT_RUN_SUMMARY
    return out


def build_tier1_gate_summary(test_gates: str = "") -> str:
    """Static tier1 gate line until a tier1 sidecar exists."""
    gates = {g.strip().lower() for g in (test_gates or "").split(",") if g.strip()}
    if "tier1" not in gates:
        return "n/a"
    return "no tests"


def read_gate_sidecar(path: str) -> str:
    """Read BVT/smoke sidecar JSON from workspace; return empty when missing."""
    raw = (path or "").strip()
    if not raw:
        return ""
    try:
        return Path(raw).read_text(encoding="utf-8")
    except OSError:
        return ""


def format_compact_run_summary_tekton_result(
    *,
    pipeline_run_name: str = "",
    cluster: str = "",
    operator_version: str = "",
    test_status: str = "",
    artifacts_url: str = "",
    fbcf_image: str = "",
    test_stats_lines: list[str] | None = None,
) -> str:
    """Small RUN_SUMMARY Tekton result; full detail lives in PipelineRun annotations."""
    rows: list[str] = ["Run context"]
    if pipeline_run_name.strip():
        rows.append(f"- PipelineRun: {pipeline_run_name.strip()}")
    rows.append(f"- Cluster: {cluster.strip() or '(unknown)'}")
    rows.append(f"- Operator: {operator_version.strip() or '(unknown)'}")
    if test_status.strip():
        rows.append(f"- Pipeline tasks: {test_status.strip()}")
    if fbcf_image.strip() and fbcf_image.strip() not in ("(unknown)", "n/a"):
        rows.append(f"- FBCF image: {fbcf_image.strip()}")
    if artifacts_url.strip():
        rows.append(f"- Artifacts: {artifacts_url.strip()}")
    stats = [ln.strip().lstrip("- ").strip() for ln in (test_stats_lines or []) if ln.strip()]
    if stats:
        rows.extend(["", "Test statistics"])
        rows.extend(f"- {line}" for line in stats)
    rows.extend(["", "- Full summary: PipelineRun rhoai-e2e.* annotations"])
    return "\n".join(rows)


def format_run_summary_block(
    *,
    pipeline_run_name: str = "",
    cluster: str = "",
    fbcf_image: str = "",
    operator_version: str = "",
    test_status: str = "",
    test_output: str = "",
    artifacts_url: str = "",
    report_portal_url: str = "",
    jira_url: str = "",
    smoke_component_lines: list[str] | None = None,
) -> str:
    """Single multiline Tekton result: ``NAME: value`` per line (Konflux-friendly)."""
    rows: list[str] = []
    always_show = frozenset({"PIPELINE_RUN_NAME", "CLUSTER", "OPERATOR_VERSION", "TEST_STATUS"})
    for key, val in (
        ("PIPELINE_RUN_NAME", pipeline_run_name),
        ("CLUSTER", cluster or "(unknown)"),
        ("FBCF_IMAGE", fbcf_image),
        ("OPERATOR_VERSION", operator_version or "(unknown)"),
        ("TEST_STATUS", test_status or "Completed"),
        ("TEST_OUTPUT", test_output),
        ("ARTIFACTS_URL", artifacts_url),
        ("REPORT_PORTAL_URL", report_portal_url),
        ("JIRA_URL", jira_url),
    ):
        text = (val or "").strip()
        if key == "TEST_OUTPUT" and text and "\n" in text:
            rows.append("TEST_OUTPUT:")
            rows.extend(text.splitlines())
            continue
        if text or key in always_show:
            rows.append(f"{key}: {text or '(unknown)'}")
    comp = [ln.strip() for ln in (smoke_component_lines or []) if ln.strip()]
    if comp:
        if rows:
            rows.append("---")
        rows.extend(comp)
    return "\n".join(rows)


def suite_lines_from_test_output(raw: str) -> list[str]:
    """Parse ``TEST_OUTPUT`` JSON and return Jenkins-style suite lines."""
    if not raw.strip():
        return []
    try:
        obj = json.loads(raw)
    except json.JSONDecodeError:
        return []
    if not isinstance(obj, dict):
        return []
    suites = obj.get("suites")
    if not isinstance(suites, list):
        return []
    lines: list[str] = []
    for item in suites:
        if isinstance(item, dict) and item.get("name"):
            lines.append(format_jenkins_suite_line(item))
    return lines


def format_component_line_for_ui(suite: dict[str, Any]) -> str:
    """``workbenches: 4 passed, 1 failed, 0 skipped`` for Konflux TEST_OUTPUT."""
    suite_id = str(suite.get("id", "")).strip()
    label = _ARTIFACT_TO_COMPONENT.get(suite_id)
    if not label:
        if suite_id:
            label = suite_id.replace("-", "_")
        else:
            label = str(suite.get("name", "suite")).lower().replace(" ", "_")
    return f"{label}: {format_suite_stats_compact(suite)}"


def _note_covers_single_suite(note: str, suite: dict[str, Any]) -> bool:
    """True when *note* already states the same stats as the only suite (component task)."""
    name = str(suite.get("name", "")).strip()
    if not name:
        return False
    stats = format_suite_stats_compact(suite)
    expected = f"{name}: {stats}"
    return any(line.strip() == expected for line in _normalize_newlines(note).splitlines())


def format_human_results_text(
    raw: str,
    *,
    include_component_suites: bool = True,
) -> str:
    """Plain multiline text for Konflux Results (TASK_MESSAGE, propagated summaries)."""
    text = (raw or "").strip()
    if not text:
        return ""
    if not text.lstrip().startswith("{"):
        return "\n".join(dedupe_lines(_normalize_newlines(text).splitlines()))

    try:
        obj = json.loads(text)
    except json.JSONDecodeError:
        return text
    if not isinstance(obj, dict):
        return text

    lines: list[str] = []
    note = _normalize_newlines(str(obj.get("note", ""))).strip()
    if note:
        lines.extend(dedupe_lines(ln.strip() for ln in note.splitlines() if ln.strip()))

    suites = _suites_from_test_output_obj(obj)
    if suites:
        if not lines:
            lines.append(format_summary_line_for_ui(obj, suites))
        if include_component_suites and not _health_check_suites(suites):
            if len(suites) == 1 and note and _note_covers_single_suite(note, suites[0]):
                pass
            else:
                lines.extend(format_component_line_for_ui(s) for s in order_suites(suites, None))
    elif not lines:
        result = str(obj.get("result", "")).strip()
        if result:
            failures = int(obj.get("failures", 0))
            successes = int(obj.get("successes", 0))
            lines.append(f"result={result} ({successes} passed, {failures} failed)")

    return "\n".join(dedupe_lines(lines))


def _gate_prefix_from_note(note: str, default: str = "tests") -> str:
    head = note.split(":", 1)[0].strip().lower()
    return head if head else default


def format_summary_line_for_ui(
    obj: dict[str, Any],
    suites: list[dict[str, Any]],
    *,
    gate: str | None = None,
) -> str:
    """``smoke: 93% pass rate (14 passed, 1 failed, 0 skipped)`` — gate totals only."""
    passed = sum(int(s.get("passed", 0)) for s in suites)
    failed = sum(int(s.get("failed", 0)) for s in suites)
    skipped = sum(int(s.get("skipped", 0)) for s in suites)
    total = sum(int(s.get("total", 0)) for s in suites) or (passed + failed + skipped)
    gate_label = (gate or _gate_prefix_from_note(str(obj.get("note", "")))).strip().lower()
    if total <= 0:
        return f"{gate_label}: no JUnit results"
    pct = (100.0 * passed / total) if total else 0.0
    return f"{gate_label}: {pct:.0f}% pass rate ({passed} passed, {failed} failed, {skipped} skipped)"


def _suites_from_test_output_obj(obj: dict[str, Any]) -> list[dict[str, Any]]:
    suites_raw = obj.get("suites")
    if not isinstance(suites_raw, list):
        return []
    return [item for item in suites_raw if isinstance(item, dict)]


def format_gate_block_for_ui(gate: str, raw: str) -> list[str]:
    """Gate-level summary only (per-component stats live in separate Tekton results)."""
    if not raw.strip():
        return []
    try:
        obj = json.loads(raw)
    except json.JSONDecodeError:
        return dedupe_lines([raw.strip()])
    if not isinstance(obj, dict):
        return dedupe_lines([raw.strip()])

    note = _normalize_newlines(str(obj.get("note", ""))).strip()
    note_lines = dedupe_lines(ln.strip() for ln in note.splitlines() if ln.strip()) if note else []
    if gate == "combined" and note_lines:
        return note_lines
    if len(note_lines) > 1:
        return note_lines

    suites = _suites_from_test_output_obj(obj)
    if suites:
        return [format_summary_line_for_ui(obj, suites, gate=gate)]
    if note_lines:
        return note_lines
    return dedupe_lines(format_test_output_for_ui(raw).splitlines())


def format_test_outputs_for_ui(outputs: list[tuple[str, str]]) -> str:
    """Format one or more gate TEST_OUTPUT JSON blobs for Konflux Results."""
    if not outputs:
        return ""
    lines: list[str] = []
    for gate, raw in outputs:
        lines.extend(format_gate_block_for_ui(gate, raw))
    return "\n".join(dedupe_lines(lines))


def format_test_output_for_ui(raw: str) -> str:
    """Human-readable TEST_OUTPUT: gate summary line(s) only."""
    if not raw.strip():
        return ""
    try:
        obj = json.loads(raw)
    except json.JSONDecodeError:
        return raw.strip()
    if not isinstance(obj, dict):
        return raw.strip()

    suites = _suites_from_test_output_obj(obj)

    if suites:
        return format_summary_line_for_ui(obj, suites)
    note = str(obj.get("note", "")).strip()
    result = str(obj.get("result", "")).strip()
    if note:
        return note
    if result:
        failures = int(obj.get("failures", 0))
        successes = int(obj.get("successes", 0))
        return f"result={result} ({successes} passed, {failures} failed)"
    return ""


def overall_pass_rate_note(prefix: str, suites: list[dict[str, Any]]) -> str:
    gate = prefix.strip().rstrip(":").lower()
    return format_summary_line_for_ui({"note": f"{gate}:"}, suites)
