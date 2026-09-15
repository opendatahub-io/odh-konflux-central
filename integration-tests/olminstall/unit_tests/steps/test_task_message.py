"""Unit tests for per-task TASK_MESSAGE Tekton result wiring and formatting."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml

from unit_tests._paths import OLMINSTALL_ROOT

_OLMINSTALL = OLMINSTALL_ROOT
_TASKS_DIR = _OLMINSTALL / "tekton" / "tasks"
_PIPELINE = _OLMINSTALL / "tekton" / "pipelines" / "olminstall-pipeline.yaml"
_WRITE_RUNNER = "run_write_task_message.sh"
_WRITE_SCRIPT = "write_task_message.py"
_STEP_SUMMARY = "write-konflux-task-summary"
_FORBIDDEN = "Inline TASK_MESSAGE emitter for Tekton finally steps"

def _assert_task_message_wiring(text: str, *, label: str) -> None:
    assert "TASK_MESSAGE" in text, label
    assert _STEP_SUMMARY in text, label
    assert _WRITE_RUNNER in text, label
    assert _FORBIDDEN not in text, label
    assert "wire_task_message" not in text, label
    assert "displayName: Emit task summary" not in text, label
    assert 'exec python3 "${SCRIPTS_REPO_ROOT}/steps/write_task_message.py"' not in text, label

def test_task_message_wiring_present() -> None:
    for path in sorted(_TASKS_DIR.glob("*.yaml")):
        text = path.read_text(encoding="utf-8")
        doc = yaml.safe_load(text)
        if doc.get("kind") == "Task":
            assert "finally:" not in text, path.name
        _assert_task_message_wiring(text, label=path.name)
    pipe = _PIPELINE.read_text(encoding="utf-8")
    # parse-pipeline-tests: inline print-run-context; resolve-component-run-flags: no TASK_MESSAGE.
    _skip_task_message_specs = 2
    assert pipe.count("      taskSpec:") - _skip_task_message_specs == pipe.count(_STEP_SUMMARY)
    _assert_task_message_wiring(pipe, label="olminstall-pipeline.yaml")

def test_build_task_message_success_with_hint() -> None:
    from steps.write_task_message import build_task_message

    msg = build_task_message(
        pipeline_task="provision-ephc-space",
        results={"secretRef": "my-space-secret"},
    )
    assert msg == (
        "provision-ephc-space: Succeeded.\n"
        "secretRef=my-space-secret."
    )

def test_build_task_message_wait_for_conforma_preserves_prewritten_skip_label() -> None:
    from steps.write_task_message import build_task_message

    msg = build_task_message(
        pipeline_task="wait-for-conforma",
        results={
            "CONFORMA_GATE": "pass",
            "TASK_MESSAGE": (
                "wait-for-conforma: Succeeded\n"
                "CONFORMA_GATE=skip.\n"
                "bypassed (gate_disabled)."
            ),
        },
    )
    assert "CONFORMA_GATE=skip" in msg
    assert "bypassed (gate_disabled)" in msg
    assert "CONFORMA_GATE=pass" not in msg

def test_build_task_message_prepare_includes_version_skipped(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    from steps import write_task_message

    manifest = tmp_path / "version_skipped.json"
    manifest.write_text(
        json.dumps(
            {
                "summary": "version-skipped 2 component(s) on RHOAI 3.5.0-ea.2: "
                "llama_stack (maxRhoai=3.4), ai_safety (maxRhoai=3.4)",
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("VERSION_SKIPPED_JSON", str(manifest))
    msg = write_task_message.build_task_message(
        pipeline_task="opendatahub-tests-prepare",
        results={},
    )
    assert "opendatahub-tests-prepare: Succeeded" in msg
    assert "version-skipped 2 component(s)" in msg

def test_build_task_message_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    from steps import write_task_message

    monkeypatch.setattr(
        write_task_message,
        "_read_termination",
        lambda: ("step-prepare-kubeconfig", "Error"),
    )
    msg = write_task_message.build_task_message(
        pipeline_task="install-ocp-cluster",
        results={},
    )
    assert msg == "install-ocp-cluster: Failed - step-prepare-kubeconfig - Error."

def test_build_task_message_failure_includes_test_output(monkeypatch: pytest.MonkeyPatch) -> None:
    from steps import write_task_message

    monkeypatch.setattr(
        write_task_message,
        "_read_termination",
        lambda: ("run-component-pytest", "Error"),
    )
    raw = (
        '{"note":"MaaS Billing: 0 passed, 20 failed, 0 skipped","result":"FAILURE",'
        '"successes":0,"failures":20,"warnings":0}'
    )
    msg = write_task_message.build_task_message(
        pipeline_task="test-maas-billing",
        results={"TEST_OUTPUT": raw},
    )
    assert msg.startswith("test-maas-billing: Failed - run-component-pytest - Error.\n")
    assert "MaaS Billing: 0 passed, 20 failed, 0 skipped" in msg

def test_build_task_message_test_output_json() -> None:
    from steps.write_task_message import build_task_message

    raw = '{"note":"smoke: 98% pass rate (43 passed, 1 failed, 0 skipped)","result":"SUCCESS"}'
    msg = build_task_message(pipeline_task="test-finalize", results={"TEST_OUTPUT": raw})
    assert msg == (
        "test-finalize: Succeeded.\n"
        "smoke: 98% pass rate (43 passed, 1 failed, 0 skipped)."
    )

def test_build_task_message_test_output_multiline_components() -> None:
    from steps.write_task_message import build_task_message

    raw = (
        '{"note":"smoke: 89% pass rate (8 passed, 1 failed, 0 skipped)",'
        '"suites":[{"id":"workbenches-smoke","passed":4,"failed":1,"skipped":0,"total":5},'
        '{"id":"model-registry-smoke","passed":4,"failed":0,"skipped":0,"total":4}]}'
    )
    msg = build_task_message(pipeline_task="test-workbench-images", results={"TEST_OUTPUT": raw})
    assert msg.startswith("test-workbench-images: Succeeded.\n")
    assert "smoke: 89% pass rate" in msg
    assert "workbenches: 4 passed, 1 failed, 0 skipped" in msg
    assert "model_registry: 4 passed, 0 failed, 0 skipped" in msg

def test_build_task_message_single_component_skips_duplicate_suite_line() -> None:
    from steps.write_task_message import build_task_message

    raw = (
        '{"result":"SUCCESS","note":"AI Pipelines: 5 passed, 0 failed, 0 skipped",'
        '"suites":[{"id":"ai_pipelines-smoke","name":"AI Pipelines","passed":5,"failed":0,"skipped":0,"total":5}]}'
    )
    msg = build_task_message(pipeline_task="test-ai-pipelines", results={"TEST_OUTPUT": raw})
    assert msg == (
        "test-ai-pipelines: Succeeded.\n"
        "AI Pipelines: 5 passed, 0 failed, 0 skipped."
    )

def test_build_task_message_propagated_plain_text() -> None:
    from steps.write_task_message import build_task_message

    raw = json.dumps(
        {
            "result": "SUCCESS",
            "successes": 7,
            "failures": 0,
            "skipped": 0,
            "note": (
                "bvt: 100% pass rate (5 passed, 0 failed, 0 skipped)\n"
                "smoke: 100% pass rate (2 passed, 0 failed, 0 skipped)"
            ),
        },
        separators=(",", ":"),
    )
    msg = build_task_message(
        pipeline_task="publish-results",
        results={"TEST_OUTPUT": raw, "OPERATOR_VERSION": "3.5.0-ea.2"},
    )
    assert msg == (
        "publish-results: Succeeded.\n"
        "bvt: 100% pass rate (5 passed, 0 failed, 0 skipped).\n"
        "smoke: 100% pass rate (2 passed, 0 failed, 0 skipped)."
    )
    assert "OPERATOR_VERSION" not in msg

def test_build_task_message_publish_results_warning() -> None:
    from steps.write_task_message import build_task_message

    raw = json.dumps(
        {
            "result": "WARNING",
            "successes": 161,
            "failures": 15,
            "skipped": 135,
            "note": "smoke: 93% pass rate (209 passed, 15 failed, 1 skipped)",
        },
        separators=(",", ":"),
    )
    msg = build_task_message(pipeline_task="publish-results", results={"TEST_OUTPUT": raw})
    assert msg == (
        "publish-results: Succeeded.\n"
        "smoke: 93% pass rate (209 passed, 15 failed, 1 skipped)."
    )

def test_format_stats_line_and_gate_summaries() -> None:
    from runners.report.junit_suite_report import (
        build_publish_results_gate_summaries,
        format_stats_line,
    )

    assert format_stats_line(passed=9, failed=0, skipped=0) == (
        "9 passed, 0 failed, 0 skipped, 9 total (100% pass rate)"
    )
    assert format_stats_line(passed=143, failed=15, skipped=135) == (
        "143 passed, 15 failed, 135 skipped, 293 total (49% pass rate)"
    )
    combined = json.dumps(
        {"result": "WARNING", "successes": 161, "failures": 15, "skipped": 135},
        separators=(",", ":"),
    )
    bvt = json.dumps({"successes": 9, "failures": 0, "skipped": 0}, separators=(",", ":"))
    smoke = json.dumps({"successes": 143, "failures": 15, "skipped": 135}, separators=(",", ":"))
    summaries = build_publish_results_gate_summaries(
        combined_raw=combined,
        bvt_raw=bvt,
        smoke_raw=smoke,
        test_gates="bvt,smoke",
    )
    assert summaries["TESTS_SUMMARY"] == "152 passed, 15 failed, 135 skipped, 302 total (50% pass rate)"
    assert summaries["BVT_GATE"] == "9 passed, 0 failed, 0 skipped, 9 total (100% pass rate)"
    assert summaries["SMOKE_GATE"] == "143 passed, 15 failed, 135 skipped, 293 total (49% pass rate)"

def test_build_task_message_skips_diagnostics_manifest_hint() -> None:
    from steps.write_task_message import build_task_message

    huge = "x" * 5000
    msg = build_task_message(
        pipeline_task="collect-diagnostics",
        results={"DIAGNOSTICS_MANIFEST": huge, "OPERATOR_VERSION": "3.5.0-ea.2"},
    )
    assert "DIAGNOSTICS_MANIFEST" not in msg
    assert "3.5.0-ea.2" in msg
    assert len(msg.encode("utf-8")) <= 480

def test_build_task_message_backfills_from_junit_when_test_output_missing(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    from steps import write_task_message

    monkeypatch.setattr(write_task_message, "_read_termination", lambda: ("", ""))
    written: list[tuple[str, str]] = []
    monkeypatch.setattr(
        write_task_message,
        "write_result",
        lambda path, value: written.append((str(path), value)),
    )
    plan = tmp_path / "plan.json"
    plan.write_text(
        '{"components":[{"id":"maas_billing","artifact_prefix":"maas_billing-smoke"}]}',
        encoding="utf-8",
    )
    (tmp_path / "maas_billing-smoke.xml").write_text(
        """<?xml version="1.0" encoding="UTF-8"?>
<testsuite name="maas_billing" tests="17" failures="1" errors="3" skipped="1">
  <testcase classname="a" name="t1"/>
</testsuite>
""",
        encoding="utf-8",
    )
    test_output_path = tmp_path / "test-output.json"
    monkeypatch.setenv("ARTIFACTS_DIR", str(tmp_path))
    monkeypatch.setenv("COMPONENT_ID", "maas_billing")
    monkeypatch.setenv("COMPONENT_TEST_PLAN_JSON", str(plan))
    monkeypatch.setenv("TEST_OUTPUT_PATH", str(test_output_path))

    msg = write_task_message.build_task_message(
        pipeline_task="test-maas-billing",
        results={},
    )
    assert msg.startswith("test-maas-billing: Partial pass.\n")
    assert "passed" in msg.lower()
    assert written
    payload = json.loads(written[0][1])
    assert payload["result"] == "WARNING"

def test_build_task_message_failure_from_test_output_without_termination(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from steps import write_task_message

    monkeypatch.setattr(write_task_message, "_read_termination", lambda: ("", ""))
    raw = (
        '{"note":"MaaS Billing: 15 passed, 1 failed, 1 skipped","result":"WARNING",'
        '"successes":15,"failures":1,"warnings":0,"skipped":1}'
    )
    msg = write_task_message.build_task_message(
        pipeline_task="test-maas-billing",
        results={"TEST_OUTPUT": raw},
    )
    assert msg.startswith("test-maas-billing: Partial pass.\n")
    assert "15 passed, 1 failed, 1 skipped" in msg

def test_collect_diagnostics_task_results_fit_tekton_budget() -> None:
    from steps.collect_diagnostics import _MANIFEST_MAX
    from steps.write_task_message import _COLLECT_DIAGNOSTICS_TASK_MESSAGE_MAX_BYTES
    from steps.tekton_util import _TEKTON_TASK_RESULTS_BUDGET_BYTES

    manifest = "x" * _MANIFEST_MAX
    task_message = "y" * _COLLECT_DIAGNOSTICS_TASK_MESSAGE_MAX_BYTES
    kubeconfig = "/credentials/kubeconfig"
    operator_version = "3.5.0-ea.2"
    total = sum(
        len(part.encode("utf-8"))
        for part in (manifest, task_message, kubeconfig, operator_version)
    )
    assert total < _TEKTON_TASK_RESULTS_BUDGET_BYTES

def test_parse_pipeline_tests_has_trigger_context_results() -> None:
    doc = yaml.safe_load(_PIPELINE.read_text(encoding="utf-8"))
    parse = next(t for t in doc["spec"]["tasks"] if t["name"] == "parse-pipeline-tests")
    names = [r.get("name") for r in parse["taskSpec"]["results"] if isinstance(r, dict)]
    for key in (
        "TRIGGER",
        "KONFLUX_EVENT",
        "SNAPSHOT",
        "FBC",
        "CLUSTER",
        "RUN",
        "TRIGGER_CMD",
    ):
        assert key in names
    assert "TASK_MESSAGE" not in names

def test_test_finalize_declares_gate_summary_results() -> None:
    doc = yaml.safe_load((_TASKS_DIR / "task-test-finalize.yaml").read_text(encoding="utf-8"))
    names = [r.get("name") for r in doc["spec"]["results"] if isinstance(r, dict)]
    assert names == ["TEST_OUTPUT", "TASK_MESSAGE", "TESTS_SUMMARY", "BVT_GATE", "SMOKE_GATE"]
    summary = next(s for s in doc["spec"]["steps"] if s["name"] == "write-konflux-task-summary")
    env_names = {e["name"] for e in summary["env"] if isinstance(e, dict)}
    assert {"BVT_GATE_PATH", "SMOKE_GATE_PATH", "TESTS_SUMMARY_PATH", "TEST_GATES"} <= env_names
    step_names = [s.get("name") for s in doc["spec"]["steps"] if isinstance(s, dict)]
    gate_idx = step_names.index("check-pipeline-test-gate")
    summary_idx = step_names.index("write-konflux-task-summary")
    assert summary_idx < gate_idx, "write-konflux-task-summary must run before gate check so Results survive gate failure"

def test_finalize_test_finalize_writes_gate_summary_results(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    from steps import write_task_message

    combined = {
        "result": "WARNING",
        "successes": 40,
        "failures": 2,
        "skipped": 0,
        "note": (
            "bvt: 100% pass rate (9 passed, 0 failed, 0 skipped)\n"
            "smoke: 95% pass rate (40 passed, 2 failed, 0 skipped)"
        ),
    }
    bvt_sidecar = {"successes": 9, "failures": 0, "skipped": 0}
    smoke_sidecar = {"successes": 40, "failures": 2, "skipped": 0}
    test_output_path = tmp_path / "test-output.json"
    task_message_path = tmp_path / "task-message.txt"
    tests_summary_path = tmp_path / "tests-summary.txt"
    bvt_gate_path = tmp_path / "bvt-gate.txt"
    smoke_gate_path = tmp_path / "smoke-gate.txt"
    bvt_sidecar_path = tmp_path / "bvt-sidecar.json"
    smoke_sidecar_path = tmp_path / "smoke-sidecar.json"
    test_output_path.write_text(json.dumps(combined, separators=(",", ":")), encoding="utf-8")
    bvt_sidecar_path.write_text(json.dumps(bvt_sidecar, separators=(",", ":")), encoding="utf-8")
    smoke_sidecar_path.write_text(json.dumps(smoke_sidecar, separators=(",", ":")), encoding="utf-8")

    monkeypatch.setenv("TEST_OUTPUT_PATH", str(test_output_path))
    monkeypatch.setenv("TASK_MESSAGE_PATH", str(task_message_path))
    monkeypatch.setenv("TESTS_SUMMARY_PATH", str(tests_summary_path))
    monkeypatch.setenv("BVT_GATE_PATH", str(bvt_gate_path))
    monkeypatch.setenv("SMOKE_GATE_PATH", str(smoke_gate_path))
    monkeypatch.setenv("BVT_TEST_OUTPUT_PATH", str(bvt_sidecar_path))
    monkeypatch.setenv("SMOKE_TEST_OUTPUT_PATH", str(smoke_sidecar_path))
    monkeypatch.setenv("TEST_GATES", "bvt,smoke")
    monkeypatch.setenv("TEKTON_RESULTS_DIR", str(tmp_path))

    write_task_message._finalize_test_finalize(
        task_message="smoke: 95% pass rate (40 passed, 2 failed, 0 skipped).",
    )

    assert bvt_gate_path.read_text(encoding="utf-8").strip() == (
        "9 passed, 0 failed, 0 skipped, 9 total (100% pass rate)"
    )
    assert smoke_gate_path.read_text(encoding="utf-8").strip() == (
        "40 passed, 2 failed, 0 skipped, 42 total (95% pass rate)"
    )
    assert "passed" in tests_summary_path.read_text(encoding="utf-8")
    assert task_message_path.read_text(encoding="utf-8").startswith(
        "smoke: 95% pass rate (40 passed, 2 failed, 0 skipped)."
    )


def test_finalize_test_finalize_without_test_output_writes_gate_summaries(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    from steps import write_task_message

    bvt_sidecar = {"successes": 9, "failures": 0, "skipped": 0}
    task_message_path = tmp_path / "task-message.txt"
    tests_summary_path = tmp_path / "tests-summary.txt"
    bvt_gate_path = tmp_path / "bvt-gate.txt"
    smoke_gate_path = tmp_path / "smoke-gate.txt"
    bvt_sidecar_path = tmp_path / "bvt-sidecar.json"
    (tmp_path / "bvt-sidecar.json").write_text(
        json.dumps(bvt_sidecar, separators=(",", ":")), encoding="utf-8"
    )

    monkeypatch.setenv("TASK_MESSAGE_PATH", str(task_message_path))
    monkeypatch.setenv("TESTS_SUMMARY_PATH", str(tests_summary_path))
    monkeypatch.setenv("BVT_GATE_PATH", str(bvt_gate_path))
    monkeypatch.setenv("SMOKE_GATE_PATH", str(smoke_gate_path))
    monkeypatch.setenv("BVT_TEST_OUTPUT_PATH", str(bvt_sidecar_path))
    monkeypatch.setenv("TEST_GATES", "bvt")
    monkeypatch.setenv("TEKTON_RESULTS_DIR", str(tmp_path))

    write_task_message._finalize_test_finalize(task_message="bvt: 100% pass rate (9 passed, 0 failed, 0 skipped).")

    assert bvt_gate_path.read_text(encoding="utf-8").strip() == (
        "9 passed, 0 failed, 0 skipped, 9 total (100% pass rate)"
    )
    assert task_message_path.read_text(encoding="utf-8").startswith("bvt: 100% pass rate")


def test_publish_results_pipeline_results_reference_publish_task() -> None:
    doc = yaml.safe_load(_PIPELINE.read_text(encoding="utf-8"))
    publish = next(t for t in doc["spec"]["finally"] if t["name"] == "publish-results")
    names = [r["name"] for r in publish["taskSpec"]["results"]]
    assert "TEST_OUTPUT" in names
    assert "TESTS_SUMMARY" in names
    plr_results = {r["name"]: r["value"] for r in doc["spec"]["results"]}
    assert plr_results["TEST_OUTPUT"] == "$(tasks.test-finalize.results.TEST_OUTPUT)"
    assert plr_results["TESTS_SUMMARY"] == "$(finally.publish-results.results.TESTS_SUMMARY)"
    assert plr_results["BVT_GATE"] == "$(finally.publish-results.results.BVT_GATE)"
    assert plr_results["SMOKE_GATE"] == "$(finally.publish-results.results.SMOKE_GATE)"
    assert plr_results["OLMINSTALL_SUMMARY_TEST_OUTPUT"] == "$(finally.publish-results.results.TESTS_SUMMARY)"
    finally_names = [t["name"] for t in doc["spec"]["finally"]]
    assert "emit-pipeline-test-output" not in finally_names
    emit_step = next(
        s for s in publish["taskSpec"]["steps"] if s["name"] == "emit-workspace-test-output"
    )
    assert "emit_pipeline_test_output_from_workspace" in emit_step["script"]

def test_publish_results_declares_compact_tekton_results() -> None:
    doc = yaml.safe_load(_PIPELINE.read_text(encoding="utf-8"))
    publish = next(t for t in doc["spec"]["finally"] if t["name"] == "publish-results")
    names = [r["name"] for r in publish["taskSpec"]["results"]]
    assert names == [
        "CLUSTER",
        "OPERATOR_VERSION",
        "TESTS_SUMMARY",
        "BVT_GATE",
        "SMOKE_GATE",
        "ARTIFACTS_URL",
        "TEST_OUTPUT",
        "TASK_MESSAGE",
    ]
    assert len(names) <= 8
    assert "TEST_OUTPUT" in names
    assert "TASK_MESSAGE" in names
    seed = next(s for s in publish["taskSpec"]["steps"] if s["name"] == "seed-ui-results")
    assert "RUN_SUMMARY.path" not in seed["script"]
    assert "TEST_OUTPUT.path" not in seed["script"]
    assert "BVT_GATE.path" in seed["script"]
    assert "SMOKE_GATE.path" in seed["script"]
    assert "TESTS_SUMMARY.path" not in seed["script"]
    assert "TIER1_GATE.path" not in seed["script"]

def test_publish_results_writes_summary_before_gate_check() -> None:
    doc = yaml.safe_load(_PIPELINE.read_text(encoding="utf-8"))
    publish = next(t for t in doc["spec"]["finally"] if t["name"] == "publish-results")
    step_names = [s.get("name") for s in publish["taskSpec"]["steps"] if isinstance(s, dict)]
    summary_idx = step_names.index("write-konflux-task-summary")
    notify_idx = step_names.index("send-notification")
    gate_idx = step_names.index("check-requested-gates-ran")
    assert summary_idx < notify_idx < gate_idx, (
        "write-konflux-task-summary and send-notification must run before gate check "
        "so TEST_OUTPUT records hollow-green and Slack still sends"
    )
    gate_step = next(s for s in publish["taskSpec"]["steps"] if s["name"] == "check-requested-gates-ran")
    assert gate_step.get("onError") != "continue", (
        "gate check must fail the TaskRun when requested gates were skipped"
    )

def test_publish_results_task_results_fit_tekton_budget() -> None:
    from runners.report.junit_suite_report import (
        build_publish_results_gate_summaries,
        format_run_summary_block,
    )
    from steps.tekton_util import (
        _TEKTON_TASK_RESULTS_BUDGET_BYTES,
        fit_tekton_task_results,
        tekton_results_termination_payload_size,
    )

    combined = json.dumps(
        {
            "result": "WARNING",
            "successes": 575,
            "failures": 35,
            "warnings": 0,
            "skipped": 236,
            "note": "bvt: 100% (0 passed, 0 failed, 0 skipped)\nsmoke: 68% (57 passed, 35 failed, 236 skipped)",
        },
        separators=(",", ":"),
    )
    from runners.report.pipeline_test_outputs import build_publish_task_test_output

    publish_test_output = json.dumps(
        build_publish_task_test_output(json.loads(combined)),
        separators=(",", ":"),
    )
    gate_summaries = build_publish_results_gate_summaries(combined_raw=combined)
    oversized_summary = format_run_summary_block(
        pipeline_run_name="olminstall-ods-qe-psi-07-bvt-smoke-nmanos-dfzbn",
        cluster="ods-qe-psi-07",
        operator_version="3.5.0-ea.2",
        test_status="Failed",
        test_output="bvt: 100% pass rate\n" + ("smoke: line with extra detail\n" * 400),
        smoke_component_lines=[f"component-{i}: 10 passed, 0 failed, 0 skipped" for i in range(18)],
    )
    assert len(oversized_summary.encode("utf-8")) > _TEKTON_TASK_RESULTS_BUDGET_BYTES

    results = {
        "TEST_OUTPUT": publish_test_output,
        "TASK_MESSAGE": (
            "publish-results: Succeeded\n"
            "bvt: 100% pass rate\n"
            "smoke: 68% pass rate"
        ),
        "CLUSTER": "ods-qe-psi-07",
        "OPERATOR_VERSION": "3.5.0-ea.2",
        "ARTIFACTS_URL": "https://artifacts.example/odh-ci-artifacts/run/",
        "FBCF_IMAGE": "quay.io/example/catalog:latest",
        **gate_summaries,
    }
    fitted = fit_tekton_task_results(results)
    assert tekton_results_termination_payload_size(fitted) <= _TEKTON_TASK_RESULTS_BUDGET_BYTES
    assert fitted["CLUSTER"] == "ods-qe-psi-07"
    assert fitted["TEST_OUTPUT"].lstrip().startswith("{")
    assert '"result":"SUCCESS"' in fitted["TEST_OUTPUT"]
    assert "publish-results: Succeeded" in fitted["TASK_MESSAGE"]
    assert "TESTS_SUMMARY" in fitted
    assert "seed-ui-results" not in fitted.get("TEST_OUTPUT", "")

def test_build_task_message_test_finalize_omits_component_suites() -> None:
    from steps.write_task_message import build_task_message

    raw = json.dumps(
        {
            "note": "bvt: 100% pass rate (9 passed, 0 failed, 0 skipped)\n"
            "smoke: 61% pass rate (208 passed, 16 failed, 115 skipped)",
            "result": "WARNING",
            "suites": [
                {"id": f"component-{i}-smoke", "passed": 10, "failed": 1, "skipped": 0, "total": 11}
                for i in range(20)
            ],
        },
        separators=(",", ":"),
    )
    msg = build_task_message(pipeline_task="test-finalize", results={"TEST_OUTPUT": raw})
    assert msg.startswith("bvt: 100% pass rate")
    assert "smoke: 61% pass rate" in msg
    assert "test-finalize:" not in msg
    assert "Partial pass" not in msg
    assert "component-0" not in msg

def test_test_finalize_and_publish_results_fit_tgl8h_like_payload() -> None:
    from steps.tekton_util import (
        _TEKTON_TASK_RESULTS_BUDGET_BYTES,
        fit_tekton_task_results,
        tekton_results_termination_payload_size,
    )

    suites = [
        {
            "id": f"{name}_smoke",
            "name": name.replace("_", " ").title(),
            "passed": 21,
            "failed": 4,
            "skipped": 0,
            "total": 25,
        }
        for name in (
            "workbenches",
            "model_registry",
            "model_server",
            "model_runtime",
            "platform",
            "spark_operator",
            "dashboard_cypress",
            "mlflow",
            "ogx",
            "kuberay",
        )
    ]
    combined = json.dumps(
        {
            "result": "WARNING",
            "successes": 217,
            "failures": 16,
            "warnings": 0,
            "skipped": 115,
            "note": "bvt: 100% pass rate (9 passed, 0 failed, 0 skipped)\n"
            "smoke: 61% pass rate (208 passed, 16 failed, 115 skipped)",
            "suites": suites,
        },
        separators=(",", ":"),
    )
    finalize_msg = (
        "bvt: 100% pass rate (9 passed, 0 failed, 0 skipped)\n"
        "smoke: 61% pass rate (208 passed, 16 failed, 115 skipped)"
    )
    finalize_fitted = fit_tekton_task_results(
        {"TEST_OUTPUT": combined, "TASK_MESSAGE": finalize_msg},
        priority=("TEST_OUTPUT", "TASK_MESSAGE"),
    )
    assert tekton_results_termination_payload_size(finalize_fitted) <= _TEKTON_TASK_RESULTS_BUDGET_BYTES
    assert "suites" not in finalize_fitted["TEST_OUTPUT"]

    combined_note = (
        "bvt: 100% pass rate (9 passed, 0 failed, 0 skipped)\n"
        "smoke: 61% pass rate (208 passed, 16 failed, 115 skipped)"
    )
    publish_fitted = fit_tekton_task_results(
        {
            "TEST_OUTPUT": json.dumps(
                {
                    "result": "SUCCESS",
                    "successes": 217,
                    "failures": 16,
                    "skipped": 115,
                    "note": combined_note,
                },
                separators=(",", ":"),
            ),
            "TASK_MESSAGE": (
                "publish-results: Succeeded\n"
                "bvt: 100% pass rate (9 passed, 0 failed, 0 skipped)\n"
                "smoke: 61% pass rate (208 passed, 16 failed, 115 skipped)"
            ),
            "TESTS_SUMMARY": "217 passed, 16 failed, 115 skipped, 348 total (62% pass rate)",
            "BVT_GATE": "9 passed, 0 failed, 0 skipped, 9 total (100% pass rate)",
            "SMOKE_GATE": "208 passed, 16 failed, 115 skipped, 339 total (61% pass rate)",
            "CLUSTER": "nmanos-konflux1",
            "OPERATOR_VERSION": "3.5.0-ea.1",
            "ARTIFACTS_URL": "https://artifacts.example/odh-ci-artifacts/tgl8h/",
        },
    )
    assert tekton_results_termination_payload_size(publish_fitted) <= _TEKTON_TASK_RESULTS_BUDGET_BYTES
    assert "suites" not in publish_fitted["TEST_OUTPUT"]

def test_finalize_publish_results_sets_success_and_gate_summaries(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    from steps import write_task_message

    combined = {
        "result": "WARNING",
        "successes": 14,
        "failures": 1,
        "skipped": 0,
        "note": (
            "bvt: 100% pass rate (9 passed, 0 failed, 0 skipped)\n"
            "smoke: 56% pass rate (5 passed, 1 failed, 0 skipped)"
        ),
    }
    test_output_path = tmp_path / "test-output.json"
    task_message_path = tmp_path / "task-message.txt"
    tests_summary_path = tmp_path / "tests-summary.txt"
    bvt_gate_path = tmp_path / "bvt-gate.txt"
    smoke_gate_path = tmp_path / "smoke-gate.txt"
    test_output_path.write_text(json.dumps(combined, separators=(",", ":")), encoding="utf-8")
    tests_summary_path.write_text("no tests", encoding="utf-8")
    bvt_gate_path.write_text("no tests", encoding="utf-8")
    smoke_gate_path.write_text("no tests", encoding="utf-8")

    monkeypatch.setenv("TEST_OUTPUT_PATH", str(test_output_path))
    monkeypatch.setenv("TASK_MESSAGE_PATH", str(task_message_path))
    monkeypatch.setenv("TESTS_SUMMARY_PATH", str(tests_summary_path))
    monkeypatch.setenv("BVT_GATE_PATH", str(bvt_gate_path))
    monkeypatch.setenv("SMOKE_GATE_PATH", str(smoke_gate_path))
    monkeypatch.setenv("TEKTON_RESULTS_DIR", str(tmp_path))
    monkeypatch.setenv("PIPELINE_TASK", "publish-results")
    monkeypatch.setattr(write_task_message, "_read_termination", lambda: ("", ""))

    write_task_message._finalize_publish_results()

    payload = json.loads(test_output_path.read_text(encoding="utf-8"))
    assert payload["result"] == "SUCCESS"
    assert "bvt:" in payload["note"]
    assert tests_summary_path.read_text(encoding="utf-8").strip() != "no tests"
    msg = task_message_path.read_text(encoding="utf-8")
    assert msg.startswith("publish-results: Succeeded.\n")
    assert "smoke: 56% pass rate" in msg


def test_finalize_publish_results_sets_smoke_not_run_when_smoke_skipped(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    from steps import write_task_message

    combined = {
        "result": "WARNING",
        "successes": 4,
        "failures": 1,
        "skipped": 0,
        "note": "bvt: 80% pass rate (4 passed, 1 failed, 0 skipped)",
    }
    test_output_path = tmp_path / "test-output.json"
    task_message_path = tmp_path / "task-message.txt"
    tests_summary_path = tmp_path / "tests-summary.txt"
    bvt_gate_path = tmp_path / "bvt-gate.txt"
    smoke_gate_path = tmp_path / "smoke-gate.txt"
    test_output_path.write_text(json.dumps(combined, separators=(",", ":")), encoding="utf-8")

    monkeypatch.setenv("TEST_OUTPUT_PATH", str(test_output_path))
    monkeypatch.setenv("TASK_MESSAGE_PATH", str(task_message_path))
    monkeypatch.setenv("TESTS_SUMMARY_PATH", str(tests_summary_path))
    monkeypatch.setenv("BVT_GATE_PATH", str(bvt_gate_path))
    monkeypatch.setenv("SMOKE_GATE_PATH", str(smoke_gate_path))
    monkeypatch.setenv("TEKTON_RESULTS_DIR", str(tmp_path))
    monkeypatch.setenv("PIPELINE_TASK", "publish-results")
    monkeypatch.setenv("TEST_GATES", "bvt,smoke")
    monkeypatch.setattr(write_task_message, "_read_termination", lambda: ("", ""))

    write_task_message._finalize_publish_results()

    payload = json.loads(test_output_path.read_text(encoding="utf-8"))
    assert payload["result"] == "FAILURE"
    assert "hollow green" in payload.get("note", "").lower()
    assert smoke_gate_path.read_text(encoding="utf-8").strip() == "N/A (not run)"
    msg = task_message_path.read_text(encoding="utf-8")
    assert "Failed" in msg or "hollow" in msg.lower()


def test_finalize_publish_results_reports_install_blocked_gates(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    from steps import write_task_message

    test_output_path = tmp_path / "test-output.json"
    task_message_path = tmp_path / "task-message.txt"
    tests_summary_path = tmp_path / "tests-summary.txt"
    bvt_gate_path = tmp_path / "bvt-gate.txt"
    smoke_gate_path = tmp_path / "smoke-gate.txt"
    test_output_path.write_text("{}", encoding="utf-8")
    bvt_gate_path.write_text("N/A (not run)", encoding="utf-8")
    smoke_gate_path.write_text("N/A (not run)", encoding="utf-8")

    monkeypatch.setenv("TEST_OUTPUT_PATH", str(test_output_path))
    monkeypatch.setenv("TASK_MESSAGE_PATH", str(task_message_path))
    monkeypatch.setenv("TESTS_SUMMARY_PATH", str(tests_summary_path))
    monkeypatch.setenv("BVT_GATE_PATH", str(bvt_gate_path))
    monkeypatch.setenv("SMOKE_GATE_PATH", str(smoke_gate_path))
    monkeypatch.setenv("TEKTON_RESULTS_DIR", str(tmp_path))
    monkeypatch.setenv("PIPELINE_TASK", "publish-results")
    monkeypatch.setenv("TEST_GATES", "bvt,smoke")
    monkeypatch.setattr(write_task_message, "_read_termination", lambda: ("", ""))
    monkeypatch.setattr(
        "runners.report.check_requested_gates_ran.upstream_blocked_test_gates",
        lambda **kwargs: ["install-rhoai: failed"],
    )
    monkeypatch.setattr(
        "runners.report.check_requested_gates_ran.collect_hollow_green_failures",
        lambda **kwargs: [],
    )

    write_task_message._finalize_publish_results()

    payload = json.loads(test_output_path.read_text(encoding="utf-8"))
    assert payload["result"] == "FAILURE"
    assert "install-rhoai: failed" in payload.get("note", "")
    assert "bvt: N/A (not run)" in payload.get("note", "")
    msg = task_message_path.read_text(encoding="utf-8")
    assert msg.startswith("publish-results: Succeeded")
    assert "install-rhoai: failed" in msg


def test_finalize_publish_results_ignores_prior_step_termination(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    from steps import write_task_message

    combined = {
        "result": "WARNING",
        "successes": 14,
        "failures": 1,
        "skipped": 0,
        "note": "smoke: 93% pass rate (209 passed, 15 failed, 1 skipped)",
    }
    test_output_path = tmp_path / "test-output.json"
    task_message_path = tmp_path / "task-message.txt"
    test_output_path.write_text(json.dumps(combined, separators=(",", ":")), encoding="utf-8")

    monkeypatch.setenv("TEST_OUTPUT_PATH", str(test_output_path))
    monkeypatch.setenv("TASK_MESSAGE_PATH", str(task_message_path))
    monkeypatch.setenv("TEKTON_RESULTS_DIR", str(tmp_path))
    monkeypatch.setenv("PIPELINE_TASK", "publish-results")
    monkeypatch.setenv("OLMINSTALL_TASK_ALWAYS_SUCCEED", "1")
    monkeypatch.setattr(
        write_task_message,
        "_read_termination",
        lambda: ("collect-ui-context", "Refusing to write Tekton result outside allowed directories"),
    )

    write_task_message._finalize_publish_results()

    payload = json.loads(test_output_path.read_text(encoding="utf-8"))
    assert payload["result"] == "SUCCESS"
    assert "smoke: 93% pass rate" in payload["note"]
