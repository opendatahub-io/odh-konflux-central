#!/usr/bin/env python3
"""Write TASK_MESSAGE Tekton result for Konflux per-task Results panel.

Runs as a Task ``finally`` step after success or failure::

    exec bash "${SCRIPTS_REPO_ROOT}/tekton/scripts/run_write_task_message.sh"

Env:
    TASK_MESSAGE_PATH  -- Tekton result file (``$(results.TASK_MESSAGE.path)``)
    PIPELINE_TASK      -- optional; pod label ``tekton.dev/pipelineTask`` (downward API)
    SCRIPTS_REPO_ROOT  -- rhoai-e2e checkout (set per task; EPHC shallow-clones first)
    TEST_OUTPUT_PATH   -- optional; backfill from JUnit when summarize step failed
    ARTIFACTS_DIR, COMPONENT_ID, COMPONENT_TEST_PLAN_JSON -- component smoke fallback
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

from _bootstrap import ensure_rhoai_e2e_path

ensure_rhoai_e2e_path()

from runners.report.junit_suite_report import (
    augment_publish_gate_note,
    build_publish_results_gate_summaries,
    build_tier1_gate_summary,
    format_human_results_text,
    is_gate_summary_placeholder,
    read_gate_sidecar,
)
from runners.report.pipeline_test_outputs import (
    combined_test_output_from_sidecars,
    konflux_failure_test_output_json,
    konflux_publish_success_test_output_json,
    publish_results_test_output_json,
)
from suite.konflux_task_message import format_konflux_task_message
from steps.tekton_util import (
    _TEKTON_RESULT_MAX_BYTES,
    _TEKTON_TASK_RESULTS_BUDGET_BYTES,
    clamp_tekton_result,
    fit_tekton_task_results,
    read_tekton_results_at_paths,
    read_tekton_task_result_files,
    slim_test_output_for_tekton,
    tekton_results_termination_payload_size,
    tekton_task_results_payload_size,
    write_result,
    write_tekton_results_at_paths,
    write_tekton_task_result_files,
)

# Tekton step / task results are capped at 4096 bytes.
_MAX_BYTES = 3800
_MAX_HINT = 160
# collect-diagnostics declares DIAGNOSTICS_MANIFEST + OPERATOR_VERSION + KUBECONFIG_PATH too.
_COLLECT_DIAGNOSTICS_TASK_MESSAGE_MAX_BYTES = 480
# prepare also declares OPENDATAHUB_TESTS_IMAGE + DISTRIBUTED_WORKLOADS_TESTS_IMAGE + RUN_SMOKE_*;
# keep TASK_MESSAGE small so the task stays under Tekton's 4096 B results cap.
_PREPARE_TASK_MESSAGE_MAX_BYTES = 480
# publish-results also declares TEST_OUTPUT, gate summaries, CLUSTER, etc. on the same task.
_PUBLISH_RESULTS_TASK_MESSAGE_MAX_BYTES = 720
_ELLIPSIS = "…"
_ELLIPSIS_BYTES = len(_ELLIPSIS.encode("utf-8"))

_RESULT_HINT_PRIORITY: tuple[str, ...] = (
    "TEST_OUTPUT",
    "INSTALL_STATUS",
    "OPERATOR_VERSION",
    "clusterName",
    "ocpMinor",
    "ocpChannel",
    "secretRef",
    "FBCF_IMAGE",
    "ARTIFACTS_URL",
    "OPENDATAHUB_TESTS_IMAGE",
)

_SKIP_HINT_KEYS = frozenset({"TASK_MESSAGE", "DIAGNOSTICS_MANIFEST"})

_MULTILINE_RESULT_KEYS = frozenset({"TEST_OUTPUT"})

_PUBLISH_GATE_RESULT_NAMES: tuple[str, ...] = ("TESTS_SUMMARY", "BVT_GATE", "SMOKE_GATE")

_TEST_FINALIZE_RESULT_PRIORITY: tuple[str, ...] = (
    "TEST_OUTPUT",
    "TASK_MESSAGE",
    *_PUBLISH_GATE_RESULT_NAMES,
)

_PUBLISH_RESULT_PATH_ENVS: tuple[tuple[str, str], ...] = (
    ("TEST_OUTPUT", "TEST_OUTPUT_PATH"),
    ("TASK_MESSAGE", "TASK_MESSAGE_PATH"),
    ("TESTS_SUMMARY", "TESTS_SUMMARY_PATH"),
    ("BVT_GATE", "BVT_GATE_PATH"),
    ("SMOKE_GATE", "SMOKE_GATE_PATH"),
    ("CLUSTER", "CLUSTER_PATH"),
    ("OPERATOR_VERSION", "OPERATOR_VERSION_PATH"),
    ("ARTIFACTS_URL", "ARTIFACTS_URL_PATH"),
)

_TEST_FINALIZE_RESULT_PATH_ENVS: tuple[tuple[str, str], ...] = tuple(
    (name, f"{name}_PATH") for name in _TEST_FINALIZE_RESULT_PRIORITY
)


def _compact(text: str, limit: int = _MAX_HINT) -> str:
    one = " ".join((text or "").split())
    if len(one) <= limit:
        return one
    return one[: limit - 1] + "…"


def _read_termination() -> tuple[str, str]:
    root = Path("/tekton/termination")
    if not root.is_dir():
        return "", ""
    step = (root / "step").read_text(encoding="utf-8").strip() if (root / "step").is_file() else ""
    msg = (root / "message").read_text(encoding="utf-8").strip() if (root / "message").is_file() else ""
    if not msg and (root / "reason").is_file():
        msg = (root / "reason").read_text(encoding="utf-8").strip()
    return step, msg


def _read_sibling_results() -> dict[str, str]:
    out: dict[str, str] = {}
    root = Path("/tekton/results")
    if not root.is_dir():
        return out
    for path in sorted(root.iterdir()):
        if not path.is_file():
            continue
        try:
            val = path.read_text(encoding="utf-8").strip()
        except OSError:
            continue
        if val:
            out[path.name] = val
    return out


def _test_output_hint(results: dict[str, str], *, pipeline_task: str = "") -> str:
    """Human-readable pass/fail lines from TEST_OUTPUT when present."""
    include_component_suites = pipeline_task != "test-finalize"
    for key in _MULTILINE_RESULT_KEYS:
        val = results.get(key, "").strip()
        if val:
            return format_human_results_text(
                val,
                include_component_suites=include_component_suites,
            )
    return ""


def _pass_hint(results: dict[str, str]) -> tuple[str, bool]:
    """Return (hint text, multiline)."""
    for key in _RESULT_HINT_PRIORITY:
        val = results.get(key, "").strip()
        if not val:
            continue
        if key in _MULTILINE_RESULT_KEYS:
            return format_human_results_text(val), True
        return _compact(f"{key}={val}"), False
    for key, val in results.items():
        if key in _SKIP_HINT_KEYS or not val.strip():
            continue
        return _compact(f"{key}={val.strip()}"), False
    if "DIAGNOSTICS_MANIFEST" in results:
        return "diagnostics collected", False
    return "", False


def _test_output_result_class(results: dict[str, str]) -> str:
    raw = results.get("TEST_OUTPUT", "").strip()
    if not raw.lstrip().startswith("{"):
        return ""
    try:
        obj = json.loads(raw)
    except json.JSONDecodeError:
        return ""
    if not isinstance(obj, dict):
        return ""
    return str(obj.get("result", "")).strip().upper()


def _ensure_component_test_output(results: dict[str, str]) -> dict[str, str]:
    """Backfill TEST_OUTPUT from component JUnit when summarize step did not run."""
    if results.get("TEST_OUTPUT", "").strip():
        return results
    artifacts = os.environ.get("ARTIFACTS_DIR", "").strip()
    component_id = os.environ.get("COMPONENT_ID", "").strip()
    if not artifacts or not component_id:
        return results
    test_output_path = os.environ.get("TEST_OUTPUT_PATH", "").strip()
    if test_output_path:
        try:
            existing = Path(test_output_path).read_text(encoding="utf-8").strip()
        except OSError:
            existing = ""
        if existing:
            return {**results, "TEST_OUTPUT": existing}
    plan_raw = os.environ.get("COMPONENT_TEST_PLAN_JSON", "").strip()
    plan_path = Path(plan_raw) if plan_raw else None
    from steps.summarize_test_output import build_test_output_payload

    try:
        payload, _note = build_test_output_payload(
            artifacts,
            note_prefix="Component",
            component_id=component_id,
            plan_path=plan_path,
        )
    except (OSError, ValueError, KeyError):
        return results
    output_json = json.dumps(payload, separators=(",", ":"))
    if test_output_path:
        write_result(test_output_path, output_json)
    return {**results, "TEST_OUTPUT": output_json}


def _combined_test_output_raw(sibling: dict[str, str]) -> str:
    """Best-effort combined bvt/smoke TEST_OUTPUT JSON for publish summaries."""
    payload = combined_test_output_from_sidecars(
        test_gates=os.environ.get("TEST_GATES", "").strip(),
        smoke_path=os.environ.get("SMOKE_TEST_OUTPUT_PATH", "").strip(),
        bvt_path=os.environ.get("BVT_TEST_OUTPUT_PATH", "").strip(),
    )
    if payload is not None:
        return json.dumps(payload, separators=(",", ":"))

    raw = sibling.get("TEST_OUTPUT", "").strip()
    if raw.lstrip().startswith("{"):
        try:
            obj = json.loads(raw)
        except json.JSONDecodeError:
            obj = None
        else:
            if isinstance(obj, dict):
                note = str(obj.get("note", "")).strip()
                if note and ("bvt:" in note.lower() or "smoke:" in note.lower()):
                    return raw

    file_raw = _read_tekton_result_file("TEST_OUTPUT_PATH")
    if file_raw.lstrip().startswith("{"):
        return file_raw
    return raw


def _publish_gate_hint(sibling: dict[str, str]) -> str:
    combined_raw = _combined_test_output_raw(sibling)
    if combined_raw:
        hint = format_human_results_text(
            combined_raw,
            include_component_suites=False,
        )
        if hint:
            return hint
    return ""


def _publish_results_step_failed() -> bool:
    """True when publish-results itself failed, not prior onError: continue steps."""
    step, term_msg = _read_termination()
    if not (term_msg or step):
        return False
    if os.environ.get("OLMINSTALL_TASK_ALWAYS_SUCCEED", "").strip() != "1":
        return True
    failed_step = (step or "").strip()
    if failed_step in ("", "write-konflux-task-summary"):
        return bool(term_msg or step)
    return False


def _read_tekton_result_file(env_name: str) -> str:
    raw = os.environ.get(env_name, "").strip()
    if not raw or "$(" in raw:
        return ""
    try:
        return Path(raw).read_text(encoding="utf-8").strip()
    except OSError:
        return ""


def _build_publish_gate_summaries_for_sibling(sibling: dict[str, str]) -> dict[str, str]:
    combined_raw = _combined_test_output_raw(sibling)
    if not combined_raw.lstrip().startswith("{"):
        combined_raw = _read_tekton_result_file("TEST_OUTPUT_PATH")
    combined_obj = None
    if combined_raw.lstrip().startswith("{"):
        try:
            parsed = json.loads(combined_raw)
            if isinstance(parsed, dict):
                combined_obj = parsed
        except json.JSONDecodeError:
            combined_obj = None
    return build_publish_results_gate_summaries(
        combined_raw=combined_raw,
        combined_obj=combined_obj,
        bvt_raw=read_gate_sidecar(os.environ.get("BVT_TEST_OUTPUT_PATH", "")),
        smoke_raw=read_gate_sidecar(os.environ.get("SMOKE_TEST_OUTPUT_PATH", "")),
        test_gates=os.environ.get("TEST_GATES", "").strip(),
    )


def _resolved_publish_gate_summaries(
    *,
    sibling: dict[str, str],
    gate_summaries: dict[str, str],
) -> dict[str, str]:
    """Prefer rebuilt gate stats; keep populate/collect writes when rebuild is empty."""
    merged = _merge_gate_summaries(current=sibling, fresh=gate_summaries)
    out: dict[str, str] = {}
    for name in _PUBLISH_GATE_RESULT_NAMES:
        val = merged.get(name, "").strip()
        if val and not is_gate_summary_placeholder(val):
            out[name] = val
    return out


def _write_publish_gate_summaries(
    *,
    paths: dict[str, str],
    summaries: dict[str, str],
) -> None:
    gate_paths = {name: paths[name] for name in _PUBLISH_GATE_RESULT_NAMES if name in paths}
    to_write = {
        name: summaries[name]
        for name in gate_paths
        if summaries.get(name, "").strip() and not is_gate_summary_placeholder(summaries[name])
    }
    if to_write:
        write_tekton_results_at_paths(to_write, gate_paths)
    for name in _PUBLISH_GATE_RESULT_NAMES:
        val = to_write.get(name, "").strip()
        if val:
            print(f"Wrote {name}: {val}", flush=True)
        elif name in gate_paths:
            print(f"WARN: {name} still unset after finalize", file=sys.stderr)


def _publish_results_task_message(
    task_label: str,
    sibling: dict[str, str],
    *,
    step_failed: bool = False,
    failure_detail: str = "",
    upstream_blockers: list[str] | None = None,
) -> str:
    """publish-results summary: green when publish succeeded; gate lines in TASK_MESSAGE."""
    if step_failed or _publish_results_step_failed():
        status_word = "Failed"
        detail = failure_detail.strip()
        if not detail:
            step, term_msg = _read_termination()
            detail = term_msg or "step failed"
            if step and step not in detail:
                detail = f"{step} - {detail}"
        head = f"{task_label}: {status_word} - {_compact(detail, limit=200)}"
        return head

    blockers = [b.strip() for b in (upstream_blockers or []) if b.strip()]
    hint = _publish_gate_hint(sibling)
    head = f"{task_label}: Succeeded"
    if blockers:
        blocked_head = f"{head} - {'; '.join(blockers[:2])}"
        if hint:
            return f"{blocked_head}\n{hint}"
        test_gates = os.environ.get("TEST_GATES", "").strip()
        from runners.report.check_requested_gates_ran import format_install_blocked_publish_note

        gate_note = format_install_blocked_publish_note(blockers, test_gates=test_gates)
        return f"{blocked_head}\n{gate_note}" if gate_note else blocked_head
    return f"{head}\n{hint}" if hint else head


def _dsc_drift_note() -> str:
    """Return DSC drift description when marker exists for this component."""
    artifacts = os.environ.get("ARTIFACTS_DIR", "").strip()
    component_id = os.environ.get("COMPONENT_ID", "").strip()
    if not artifacts or not component_id:
        return ""
    from suite.dsc_baseline import read_dsc_drift_marker

    drifts = read_dsc_drift_marker(Path(artifacts), component_id)
    if not drifts:
        return ""
    return "; ".join(drifts)


def _version_skipped_note() -> str:
    """Summary from run-config/version_skipped.json (opendatahub-tests-prepare)."""
    raw_path = os.environ.get("VERSION_SKIPPED_JSON", "").strip()
    if not raw_path:
        tests_shared = os.environ.get("TESTS_SHARED", "").strip()
        if tests_shared:
            raw_path = str(Path(tests_shared) / "run-config" / "version_skipped.json")
    if not raw_path:
        return ""
    try:
        doc = json.loads(Path(raw_path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return ""
    if not isinstance(doc, dict):
        return ""
    summary = str(doc.get("summary", "")).strip()
    return summary


def _component_prep_track_note() -> str:
    """Which prepare-components-prerequisites branch ran (ephc vs external)."""
    from steps.component_prep_track import read_component_prep_track_note

    return read_component_prep_track_note()


def build_task_message(*, pipeline_task: str = "", results: dict[str, str] | None = None) -> str:
    """Human-readable task summary for Konflux Results."""
    task_label = (pipeline_task or os.environ.get("PIPELINE_TASK", "")).strip()
    sibling = _ensure_component_test_output(results if results is not None else _read_sibling_results())
    if task_label == "wait-for-conforma":
        prewritten = sibling.get("TASK_MESSAGE", "").strip()
        if "CONFORMA_GATE=" in prewritten:
            return _emit_task_message(prewritten, max_bytes=_MAX_BYTES)
    if task_label == "publish-results":
        return _emit_task_message(
            _publish_results_task_message(task_label, sibling),
            max_bytes=_PUBLISH_RESULTS_TASK_MESSAGE_MAX_BYTES,
        )
    step, term_msg = _read_termination()
    result_class = _test_output_result_class(sibling)
    if term_msg or step:
        prefix = f"{task_label}: " if task_label else ""
        detail = term_msg or "step failed"
        if step and step not in detail:
            detail = f"{step} - {detail}"
        msg = f"{prefix}Failed - {_compact(detail, limit=240)}"
        hint = _test_output_hint(sibling, pipeline_task=task_label)
        if hint:
            msg = f"{msg}\n{hint}"
    elif result_class == "WARNING":
        hint = _test_output_hint(sibling, pipeline_task=task_label)
        if task_label == "test-finalize":
            msg = hint if hint else (f"{task_label}: Warning" if task_label else "Warning")
        else:
            head = f"{task_label}: Partial pass" if task_label else "Partial pass"
            msg = f"{head}\n{hint}" if hint else head
    elif result_class == "FAILURE":
        hint = _test_output_hint(sibling, pipeline_task=task_label)
        head = f"{task_label}: Failed" if task_label else "Failed"
        msg = f"{head}\n{hint}" if hint else head
    else:
        hint, multiline = _pass_hint(sibling)
        if task_label:
            head = f"{task_label}: Succeeded"
        else:
            head = "Succeeded"
        if hint:
            msg = f"{head}\n{hint}" if multiline else f"{head} - {hint}"
        else:
            msg = head
        version_skip = _version_skipped_note()
        if version_skip and task_label in ("opendatahub-tests-prepare", "Tests — prepare"):
            msg = f"{msg}\n{version_skip}"
        prep_track = _component_prep_track_note()
        if prep_track and task_label in ("opendatahub-tests-prepare", "Tests — prepare"):
            msg = f"{msg}\n{prep_track}"
    drift = _dsc_drift_note()
    if drift:
        drift_clause = f"DSC drift ({drift})"
        if drift_clause not in msg:
            first_line = msg.split("\n", 1)[0]
            if "Failed" in first_line or "Partial pass" in first_line:
                if "Failed - " in first_line:
                    msg = msg.replace("Failed - ", f"Failed - {drift_clause}; ", 1)
                else:
                    msg = f"{first_line}; {drift_clause}" + msg[len(first_line):]
            else:
                prefix = f"{task_label}: " if task_label else ""
                hint = _test_output_hint(sibling, pipeline_task=task_label)
                msg = f"{prefix}Failed - {drift_clause}"
                if hint:
                    msg = f"{msg}\n{hint}"
    return _emit_task_message(msg, max_bytes=_task_message_max_bytes(task_label))


def _result_paths(path_envs: tuple[tuple[str, str], ...]) -> dict[str, str]:
    paths: dict[str, str] = {}
    for name, env_name in path_envs:
        raw = os.environ.get(env_name, "").strip()
        if raw and "$(" not in raw:
            paths[name] = raw
    return paths


def _publish_result_paths() -> dict[str, str]:
    return _result_paths(_PUBLISH_RESULT_PATH_ENVS)


def _merge_gate_summaries(
    *,
    current: dict[str, str],
    fresh: dict[str, str],
) -> dict[str, str]:
    """Prefer real gate stats over seed-ui ``no tests`` placeholders."""
    merged = dict(current)
    for name, value in fresh.items():
        if not value or is_gate_summary_placeholder(value):
            continue
        if name not in merged or is_gate_summary_placeholder(merged.get(name, "")):
            merged[name] = value
    return merged


def _publish_task_test_output_json(
    *,
    sibling: dict[str, str],
    step_failed: bool,
    gate_summaries: dict[str, str] | None = None,
) -> str:
    if step_failed:
        combined_raw = _combined_test_output_raw(sibling)
        note = "publish-results step failed"
        if combined_raw.lstrip().startswith("{"):
            try:
                obj = json.loads(combined_raw)
            except json.JSONDecodeError:
                obj = None
            if isinstance(obj, dict):
                gate_note = str(obj.get("note", "")).strip()
                if gate_note:
                    note = f"{note}; {gate_note}"
        return konflux_failure_test_output_json(note=note)

    test_gates = os.environ.get("TEST_GATES", "").strip()
    summaries = gate_summaries or {}
    combined_raw = _combined_test_output_raw(sibling)
    if combined_raw.lstrip().startswith("{"):
        try:
            obj = json.loads(combined_raw)
        except json.JSONDecodeError:
            obj = None
        if isinstance(obj, dict):
            return publish_results_test_output_json(
                obj,
                test_gates=test_gates,
                bvt_raw=read_gate_sidecar(os.environ.get("BVT_TEST_OUTPUT_PATH", "")),
                smoke_raw=read_gate_sidecar(os.environ.get("SMOKE_TEST_OUTPUT_PATH", "")),
            )

    hint = _publish_gate_hint(sibling)
    if not hint and summaries:
        hint = augment_publish_gate_note(
            "",
            test_gates=test_gates,
            gate_summaries=summaries,
        )
    return konflux_publish_success_test_output_json(note=hint or "Results published")


def _truncate_task_message(msg: str, *, max_bytes: int) -> str:
    raw = msg.encode("utf-8")
    if len(raw) <= max_bytes:
        return msg
    return raw[: max_bytes - _ELLIPSIS_BYTES].decode("utf-8", errors="ignore") + _ELLIPSIS


def _emit_task_message(msg: str, *, max_bytes: int) -> str:
    return _truncate_task_message(format_konflux_task_message(msg), max_bytes=max_bytes)


def _task_message_max_bytes(task_label: str) -> int:
    if task_label == "collect-diagnostics":
        return _COLLECT_DIAGNOSTICS_TASK_MESSAGE_MAX_BYTES
    if task_label in ("opendatahub-tests-prepare", "Tests — prepare"):
        return _PREPARE_TASK_MESSAGE_MAX_BYTES
    if task_label == "publish-results":
        return _PUBLISH_RESULTS_TASK_MESSAGE_MAX_BYTES
    return _MAX_BYTES


def _finalize_publish_results() -> None:
    paths = _publish_result_paths()
    sibling = read_tekton_results_at_paths(paths) if paths else {}
    if not sibling:
        sibling = read_tekton_task_result_files()

    step_failed = _publish_results_step_failed()

    gate_summaries = _build_publish_gate_summaries_for_sibling(sibling)
    sibling = _merge_gate_summaries(current=sibling, fresh=gate_summaries)
    resolved_gates = _resolved_publish_gate_summaries(
        sibling=sibling,
        gate_summaries=gate_summaries,
    )
    for name, value in resolved_gates.items():
        sibling[name] = value
    test_gates = os.environ.get("TEST_GATES", "").strip()
    tier1 = sibling.get("TIER1_GATE", "").strip() or build_tier1_gate_summary(test_gates)
    if tier1:
        sibling["TIER1_GATE"] = tier1

    from runners.report.check_requested_gates_ran import (
        collect_hollow_green_failures,
        format_install_blocked_publish_note,
        upstream_blocked_test_gates,
    )

    hollow_failures = collect_hollow_green_failures(gate_values=resolved_gates)
    upstream_blockers = upstream_blocked_test_gates()
    if hollow_failures:
        step_failed = True

    task_label = (os.environ.get("PIPELINE_TASK", "") or "publish-results").strip()
    test_output = _publish_task_test_output_json(
        sibling=sibling,
        step_failed=step_failed,
        gate_summaries=gate_summaries,
    )
    if hollow_failures:
        note = "; ".join(hollow_failures[:3])
        if len(hollow_failures) > 3:
            note = f"{note}; +{len(hollow_failures) - 3} more"
        note = clamp_tekton_result(
            f"hollow green: {note}",
            max_bytes=min(800, _TEKTON_RESULT_MAX_BYTES),
        )
        test_output = slim_test_output_for_tekton(
            konflux_failure_test_output_json(note=note)
        )
        failure_detail = note
    elif upstream_blockers:
        blocked_note = format_install_blocked_publish_note(
            upstream_blockers,
            test_gates=test_gates,
        )
        test_output = slim_test_output_for_tekton(
            konflux_failure_test_output_json(note=blocked_note)
        )
        failure_detail = ""
    else:
        failure_detail = ""
    sibling["TEST_OUTPUT"] = test_output
    task_message = _emit_task_message(
        _publish_results_task_message(
            task_label,
            sibling,
            step_failed=step_failed,
            failure_detail=failure_detail,
            upstream_blockers=upstream_blockers,
        ),
        max_bytes=_PUBLISH_RESULTS_TASK_MESSAGE_MAX_BYTES,
    )
    sibling["TASK_MESSAGE"] = task_message

    fitted = fit_tekton_task_results(sibling)
    fitted_core = {
        key: value
        for key, value in fitted.items()
        if key not in _PUBLISH_GATE_RESULT_NAMES
    }
    test_output = fitted.get("TEST_OUTPUT", test_output)
    if paths:
        write_tekton_results_at_paths(fitted_core, paths)
    else:
        write_tekton_task_result_files(fitted_core)

    authoritative = {
        "TEST_OUTPUT": test_output,
        "TASK_MESSAGE": task_message,
        **resolved_gates,
    }
    if paths:
        write_tekton_results_at_paths(authoritative, paths)
    _write_publish_gate_summaries(paths=paths, summaries=resolved_gates)

    size = tekton_results_termination_payload_size({**fitted_core, **authoritative})
    print(
        f"publish-results Tekton results payload: {size}/{_TEKTON_TASK_RESULTS_BUDGET_BYTES} bytes",
        flush=True,
    )
    if test_output.lstrip().startswith("{"):
        try:
            result_class = str(json.loads(test_output).get("result", "")).strip().upper()
        except json.JSONDecodeError:
            result_class = "?"
        print(f"publish-results TEST_OUTPUT.result={result_class}", flush=True)
        print(f"publish-results TEST_OUTPUT.size={len(test_output)}", flush=True)
    if paths:
        written = read_tekton_results_at_paths(paths)
        stored = written.get("TEST_OUTPUT", "").strip()
        if stored.lstrip().startswith("{"):
            try:
                stored_result = str(json.loads(stored).get("result", "")).strip().upper()
            except json.JSONDecodeError:
                stored_result = "?"
            if stored_result and stored_result != "SUCCESS":
                print(
                    f"WARN: publish-results TEST_OUTPUT on disk is {stored_result}, expected SUCCESS",
                    file=sys.stderr,
                )
        placeholder_gates = [
            name
            for name in _PUBLISH_GATE_RESULT_NAMES
            if is_gate_summary_placeholder(written.get(name, ""))
        ]
        if placeholder_gates:
            print(
                f"WARN: publish-results placeholders remain: {', '.join(placeholder_gates)}",
                file=sys.stderr,
            )
    if not test_output.strip() or not task_message.strip():
        print("WARN: publish-results missing TEST_OUTPUT or TASK_MESSAGE after finalize", file=sys.stderr)


def _write_fitted_task_results(fitted: dict[str, str], paths: dict[str, str]) -> None:
    if paths:
        write_tekton_results_at_paths(fitted, paths)
    else:
        write_tekton_task_result_files(fitted)


def _finalize_test_finalize(*, task_message: str) -> None:
    paths = _result_paths(_TEST_FINALIZE_RESULT_PATH_ENVS)
    sibling = read_tekton_results_at_paths(paths) if paths else read_tekton_task_result_files()
    sibling["TASK_MESSAGE"] = task_message
    test_output = sibling.get("TEST_OUTPUT", "").strip()
    if test_output:
        sibling["TEST_OUTPUT"] = slim_test_output_for_tekton(test_output)

    gate_summaries = _build_publish_gate_summaries_for_sibling(sibling)

    fitted_core = fit_tekton_task_results(
        {key: value for key, value in sibling.items() if key not in _PUBLISH_GATE_RESULT_NAMES},
        priority=("TEST_OUTPUT", "TASK_MESSAGE"),
    )
    _write_fitted_task_results(fitted_core, paths)

    authoritative = {
        "TEST_OUTPUT": sibling.get("TEST_OUTPUT", ""),
        "TASK_MESSAGE": task_message,
        **gate_summaries,
    }
    if paths:
        write_tekton_results_at_paths(authoritative, paths)
    else:
        write_tekton_task_result_files(authoritative)

    size = tekton_results_termination_payload_size({**fitted_core, **authoritative})
    print(
        f"test-finalize Tekton results payload: {size}/{_TEKTON_TASK_RESULTS_BUDGET_BYTES} bytes",
        flush=True,
    )


def main() -> int:
    path = os.environ.get("TASK_MESSAGE_PATH", "").strip()
    if not path:
        print("TASK_MESSAGE_PATH missing", file=sys.stderr)
        return 1
    task_label = (os.environ.get("PIPELINE_TASK", "")).strip()
    message = build_task_message(pipeline_task=task_label)
    write_result(path, message)
    print(message, flush=True)
    if task_label == "publish-results":
        _finalize_publish_results()
    elif task_label == "test-finalize":
        _finalize_test_finalize(task_message=message)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
