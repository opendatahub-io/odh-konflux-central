#!/usr/bin/env python3
"""Rewrite test-finalize TEST_OUTPUT with combined bvt + smoke UI summary."""

from __future__ import annotations

import json
import os
import sys

from _bootstrap import ensure_rhoai_e2e_path

ensure_rhoai_e2e_path()

from runners.report.pipeline_test_outputs import (
    apply_test_finalize_display_result,
    build_finalize_test_output_from_taskruns,
    collect_bvt_smoke_outputs,
    gates_from_test_gates_csv,
)
from runners.report.pipelinerun_summary import list_pipeline_test_outputs
from runners.report.pipelinerun_summary import namespace_from_env, pipeline_run_name_from_env
from steps.tekton_incluster import list_taskruns_in_cluster
from steps.tekton_util import require_env, write_result


def main() -> int:
    test_gates = os.environ.get("TEST_GATES", "").strip()
    if not gates_from_test_gates_csv(test_gates):
        print("No bvt/smoke gates in TEST_GATES; keeping smoke-only TEST_OUTPUT")
        return 0

    pr_name = pipeline_run_name_from_env(required=True)
    ns = namespace_from_env(required=True)
    taskruns = list_taskruns_in_cluster(pr_name, ns)
    smoke_path = os.environ.get("SMOKE_TEST_OUTPUT_PATH", "").strip()
    bvt_path = os.environ.get("BVT_TEST_OUTPUT_PATH", "").strip()
    by_gate = collect_bvt_smoke_outputs(
        taskruns,
        list_from_taskruns=list_pipeline_test_outputs,
        smoke_path=smoke_path,
        bvt_path=bvt_path,
    )
    payload = build_finalize_test_output_from_taskruns(
        taskruns,
        test_gates=test_gates,
        smoke_path=smoke_path,
        bvt_path=bvt_path,
    )
    if payload is None:
        print("WARN: no gate TEST_OUTPUT available to combine", file=sys.stderr)
        return 0

    payload = apply_test_finalize_display_result(
        payload,
        by_gate=by_gate,
        test_gates=test_gates,
    )
    write_result(require_env("TEST_OUTPUT_PATH"), json.dumps(payload, separators=(",", ":")))
    print(str(payload.get("note", "")), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
