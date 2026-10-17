#!/usr/bin/env python3
"""Fail test-finalize when requested gates have FAILURE or missing TEST_OUTPUT."""

from __future__ import annotations

import json
import os
import sys

from _bootstrap import ensure_rhoai_e2e_path

ensure_rhoai_e2e_path()

from runners.report.pipeline_test_outputs import (
    COMPONENT_AGGREGATE_GATE_KEY,
    collect_bvt_smoke_outputs,
    component_aggregate_requested,
)
from runners.report.pipelinerun_summary import list_pipeline_test_outputs, namespace_from_env, pipeline_run_name_from_env
from steps.check_test_output_gate import check_test_output_json
from suite.test_output_pass_rate import gate_test_output_with_pass_rate_result
from steps.tekton_incluster import list_taskruns_in_cluster


def _workspace_gate_output_paths() -> tuple[str, str]:
    return (
        os.environ.get("SMOKE_TEST_OUTPUT_PATH", "").strip(),
        os.environ.get("BVT_TEST_OUTPUT_PATH", "").strip(),
    )


def _collect_gate_outputs(taskruns: list[dict[str, object]]) -> dict[str, str]:
    smoke_path, bvt_path = _workspace_gate_output_paths()
    return collect_bvt_smoke_outputs(
        taskruns,
        list_from_taskruns=list_pipeline_test_outputs,
        smoke_path=smoke_path,
        bvt_path=bvt_path,
    )


def _check_component_aggregate_gate(raw: str) -> tuple[int, str]:
    if not raw:
        return 1, f"{COMPONENT_AGGREGATE_GATE_KEY}: TEST_OUTPUT missing"
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        return 1, f"component tests: TEST_OUTPUT unreadable ({exc})"
    if not isinstance(data, dict):
        return 1, "component tests: TEST_OUTPUT is not a JSON object"
    gate_data = gate_test_output_with_pass_rate_result(data)
    return check_test_output_json(gate_data, gate_label="COMPONENT")


def main() -> int:
    test_gates_csv = os.environ.get("TEST_GATES", "").strip()
    gates = {part.strip().lower() for part in test_gates_csv.split(",") if part.strip()}
    check_component = component_aggregate_requested(test_gates_csv)
    check_bvt = "bvt" in gates
    if not check_bvt and not check_component:
        print("No bvt or component test gates in TEST_GATES; skipping pipeline test gate")
        return 0

    pr_name = pipeline_run_name_from_env(required=True)
    ns = namespace_from_env(required=True)
    list_errors: list[str] = []
    taskruns = list_taskruns_in_cluster(pr_name, ns, error_out=list_errors)
    if not taskruns and list_errors:
        print(f"WARN: {list_errors[0]}", file=sys.stderr)
    by_gate = _collect_gate_outputs(taskruns)
    failures: list[str] = []

    if check_bvt:
        raw = by_gate.get("bvt", "")
        if not raw:
            failures.append("bvt: TEST_OUTPUT missing")
        else:
            try:
                data = json.loads(raw)
            except json.JSONDecodeError as exc:
                failures.append(f"bvt: TEST_OUTPUT unreadable ({exc})")
            else:
                if not isinstance(data, dict):
                    failures.append("bvt: TEST_OUTPUT is not a JSON object")
                else:
                    ec, msg = check_test_output_json(
                        data,
                        gate_label="BVT",
                        allow_all_skipped_note_prefix="bvt:",
                        strict=True,
                    )
                    if ec != 0:
                        failures.append(msg)

    if check_component:
        ec, msg = _check_component_aggregate_gate(by_gate.get(COMPONENT_AGGREGATE_GATE_KEY, ""))
        if ec != 0:
            failures.append(msg)

    if failures:
        for line in failures:
            print(f"ERROR: {line}", file=sys.stderr)
        return 1

    print("Pipeline test gates passed (bvt / component TEST_OUTPUT acceptable)", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
