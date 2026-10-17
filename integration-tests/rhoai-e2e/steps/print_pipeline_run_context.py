#!/usr/bin/env python3
"""Tekton step: print rhoai-e2e trigger context to logs and Konflux Results rows."""

from __future__ import annotations

import json
import os
import subprocess
import sys

from _bootstrap import ensure_rhoai_e2e_path

ensure_rhoai_e2e_path()

from suite.pipeline_run_context import (
    TRIGGER_CONTEXT_RESULT_NAMES,
    build_pipeline_run_context_lines,
    build_pipeline_run_context_results,
    context_from_pipelinerun_json,
    trigger_context_paths_from_environ,
)
from steps.tekton_util import write_tekton_results_at_paths


def _load_pipelinerun_json(name: str, namespace: str) -> dict:
    if not name or not namespace:
        return {}
    proc = subprocess.run(
        ["oc", "get", "pipelinerun", name, "-n", namespace, "-o", "json"],
        capture_output=True,
        text=True,
        check=False,
    )
    if proc.returncode != 0:
        print(
            f"WARN: could not read PipelineRun {name!r} in {namespace!r}: "
            f"{(proc.stderr or proc.stdout or '').strip()}",
            file=sys.stderr,
        )
        return {}
    try:
        data = json.loads(proc.stdout or "{}")
    except json.JSONDecodeError:
        return {}
    return data if isinstance(data, dict) else {}


def main() -> int:
    pr_name = os.environ.get("PIPELINE_RUN_NAME", "").strip()
    pr_ns = os.environ.get("PIPELINE_NAMESPACE", "").strip()
    snapshot_raw = os.environ.get("SNAPSHOT", "").strip()
    fbc_component = os.environ.get("RHOAI_FBC_NAME", os.environ.get("COMPONENT_NAME", "")).strip()
    cluster_source = os.environ.get("CLUSTER_SOURCE", "").strip()
    product = os.environ.get("PRODUCT", "").strip()
    test_gates = os.environ.get("TEST_GATES", os.environ.get("TESTS", "")).strip()

    prj = _load_pipelinerun_json(pr_name, pr_ns)
    ctx = context_from_pipelinerun_json(
        prj,
        snapshot_raw=snapshot_raw,
        fbc_component=fbc_component,
        cluster_source=cluster_source,
        product=product,
        test_gates=test_gates,
    )

    for line in build_pipeline_run_context_lines(**ctx):
        print(line, flush=True)

    results = build_pipeline_run_context_results(**ctx)
    paths = trigger_context_paths_from_environ()
    if paths:
        write_tekton_results_at_paths(results, paths)
        payload = sum(len(results[name].encode("utf-8")) for name in TRIGGER_CONTEXT_RESULT_NAMES if name in paths)
        print(
            f"Trigger context: {len(paths)} Tekton results written for Konflux UI ({payload}B)",
            flush=True,
        )

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
