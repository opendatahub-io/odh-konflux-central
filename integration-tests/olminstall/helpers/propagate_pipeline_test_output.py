#!/usr/bin/env python3
"""Emit pipeline-level TEST_OUTPUT from TaskRun results (Tekton finally task).

Reads the current PipelineRun via the in-cluster API so pipeline results need not
reference $(tasks.install-operator.results.*) when that task was skipped.

Priority: bvt-health-checks-with-eaas.TEST_OUTPUT, then bvt-health-checks-no-eaas.TEST_OUTPUT,
then install-operator.INSTALL_STATUS, else a short status fallback.

Env:
    RESULT_PATH -- Tekton result file path to write (required)
Optional:
    PIPELINE_RUN_NAME -- default: /etc/tekton/pipelineRunName (Tekton-injected)
"""
from __future__ import annotations

import os
import sys
import urllib.error
import urllib.parse
from pathlib import Path

_OLMINSTALL = Path(__file__).resolve().parent.parent
if str(_OLMINSTALL) not in sys.path:
    sys.path.insert(0, str(_OLMINSTALL))

from helpers.pipelinerun_summary import pick_pipeline_test_output
from helpers.tekton_incluster import (
    in_cluster_get,
    kubernetes_api_base_url,
    list_taskruns_in_cluster,
    namespace_from_env,
    pipeline_run_name_from_env,
)
from helpers.tekton_util import write_result


def _pick_output(pr: dict[str, object]) -> str:
    meta = pr.get("metadata")
    if not isinstance(meta, dict):
        return "ERROR: PipelineRun missing metadata"
    pr_name = str(meta.get("name") or "")

    ns = namespace_from_env(required=True)
    list_errors: list[str] = []
    taskruns = list_taskruns_in_cluster(pr_name, ns, error_out=list_errors)
    if list_errors:
        return list_errors[0]
    if not taskruns:
        return _pipeline_run_condition_fallback(pr, pr_name)

    picked = pick_pipeline_test_output(taskruns)
    if picked:
        return picked
    return _pipeline_run_condition_fallback(pr, pr_name)


def _pipeline_run_condition_fallback(pr: dict[str, object], pr_name: str) -> str:
    conds = pr.get("status", {})
    if isinstance(conds, dict):
        c = conds.get("conditions")
        if isinstance(c, list) and c:
            first = c[0]
            if isinstance(first, dict):
                return (
                    f"PipelineRun {pr_name}: {first.get('type', 'condition')}="
                    f"{first.get('status', '')} ({first.get('reason', '')})"
                )
    return f"PipelineRun {pr_name}: no TEST_OUTPUT/INSTALL_STATUS found on TaskRuns"


def main() -> int:
    result_path = os.environ.get("RESULT_PATH", "").strip()
    if not result_path:
        print("RESULT_PATH is required", file=sys.stderr)
        return 1

    pr_name = pipeline_run_name_from_env(required=True)
    ns = namespace_from_env(required=True)
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
        print(f"ERROR: get PipelineRun: {exc}", file=sys.stderr)
        return 1

    text = _pick_output(pr)
    write_result(result_path, text)
    print(f"Wrote pipeline TEST_OUTPUT ({len(text)} chars) to {result_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
