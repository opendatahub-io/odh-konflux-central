#!/usr/bin/env python3
"""Write pipeline-level TEST_OUTPUT from TaskRun results (Tekton finally task).

Reads the current PipelineRun via the in-cluster API so pipeline results need not
reference $(tasks.install-rhoai|install-odh.results.*) when install was skipped.

Reuses ``pipeline_test_outputs`` (same helpers as test-finalize write-combined step).

Env:
    RESULT_PATH -- Tekton result file path to write (required)
Optional:
    PIPELINE_RUN_NAME -- default: /etc/tekton/pipelineRunName (Tekton-injected)
    TEST_GATES -- comma-separated phase ids (bvt, smoke, …)
    SMOKE_TEST_OUTPUT_PATH -- workspace copy from test-finalize
    BVT_TEST_OUTPUT_PATH -- workspace copy from bvt-health-checks summarize step
"""
from __future__ import annotations

import os
import sys
import urllib.error
import urllib.parse
from pathlib import Path

from _bootstrap import ensure_rhoai_e2e_path

ensure_rhoai_e2e_path()

from runners.report.pipeline_test_outputs import (
    konflux_failure_test_output_json,
    publish_results_test_output_json_from_raw,
    resolve_pipeline_test_output_text,
)
from runners.report.pipelinerun_summary import pipeline_run_name_from_env
from steps.tekton_incluster import (
    in_cluster_get,
    kubernetes_api_base_url,
    list_taskruns_in_cluster,
    namespace_from_env,
)
from steps.tekton_util import write_result


def _workspace_paths() -> tuple[str, str]:
    return (
        os.environ.get("SMOKE_TEST_OUTPUT_PATH", "").strip(),
        os.environ.get("BVT_TEST_OUTPUT_PATH", "").strip(),
    )


def _pipeline_run_condition_fallback(pr: dict[str, object], pr_name: str) -> str:
    conds = pr.get("status", {})
    if isinstance(conds, dict):
        c = conds.get("conditions")
        if isinstance(c, list) and c:
            first = c[0]
            if isinstance(first, dict):
                return konflux_failure_test_output_json(
                    note=(
                        f"PipelineRun {pr_name}: {first.get('type', 'condition')}="
                        f"{first.get('status', '')} ({first.get('reason', '')})"
                    ),
                )
    return konflux_failure_test_output_json(
        note=f"PipelineRun {pr_name}: no TEST_OUTPUT/INSTALL_STATUS found on TaskRuns",
    )


def _resolve_output(taskruns: list[dict[str, object]]) -> str | None:
    smoke_path, bvt_path = _workspace_paths()
    return resolve_pipeline_test_output_text(
        taskruns,
        test_gates=os.environ.get("TEST_GATES", "").strip(),
        smoke_path=smoke_path,
        bvt_path=bvt_path,
    )


def main() -> int:
    result_path = os.environ.get("RESULT_PATH", "").strip()
    if not result_path:
        print("RESULT_PATH is required", file=sys.stderr)
        return 1

    pr_name = pipeline_run_name_from_env(required=True)
    ns = namespace_from_env(required=True)
    list_errors: list[str] = []
    taskruns = list_taskruns_in_cluster(
        pr_name,
        ns,
        include_child_pipeline_runs=False,
        error_out=list_errors,
    )
    text = _resolve_output(taskruns) if taskruns else None
    if text is None:
        text = _resolve_output([])
    if text is None:
        if not taskruns and list_errors:
            print(f"WARN: {list_errors[0]}", file=sys.stderr)
        try:
            token = Path("/var/run/secrets/kubernetes.io/serviceaccount/token").read_text(
                encoding="utf-8"
            )
            ca = Path("/var/run/secrets/kubernetes.io/serviceaccount/ca.crt")
        except OSError as exc:
            print(f"ERROR: cannot read in-cluster serviceaccount credentials: {exc}", file=sys.stderr)
            return 1
        base = kubernetes_api_base_url()
        if not base:
            print(
                "KUBERNETES_SERVICE_HOST is missing or not an allowed in-cluster API host",
                file=sys.stderr,
            )
            return 1
        pr_url = (
            f"{base}/apis/tekton.dev/v1/namespaces/{urllib.parse.quote(ns)}"
            f"/pipelineruns/{urllib.parse.quote(pr_name)}"
        )
        try:
            pr = in_cluster_get(pr_url, token, ca)
        except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, OSError, ValueError) as exc:
            print(f"WARN: get PipelineRun for fallback: {exc}", file=sys.stderr)
            text = konflux_failure_test_output_json(
                note=f"PipelineRun {pr_name}: no TEST_OUTPUT/INSTALL_STATUS found on TaskRuns",
            )
        else:
            retry_taskruns = list_taskruns_in_cluster(pr_name, ns, include_child_pipeline_runs=False)
            text = _resolve_output(retry_taskruns) if retry_taskruns else None
            if text is None:
                text = _resolve_output([])
            if text is None:
                text = _pipeline_run_condition_fallback(pr, pr_name)
    smoke_path, bvt_path = _workspace_paths()
    test_gates = os.environ.get("TEST_GATES", "").strip()
    text = publish_results_test_output_json_from_raw(
        text,
        test_gates=test_gates,
        bvt_path=bvt_path,
        smoke_path=smoke_path,
    )
    write_result(result_path, text)
    print(f"Wrote pipeline TEST_OUTPUT ({len(text)} chars) to {result_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
