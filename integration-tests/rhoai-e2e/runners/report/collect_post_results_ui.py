#!/usr/bin/env python3
"""Collect Konflux UI fields from TaskRuns (finally task; safe when install tasks skipped).

Writes Tekton results used by pipeline-run-summary and as fallbacks
when patch-summary-annotations cannot run.

Env:
    PIPELINE_RUN_NAME  -- Tekton PipelineRun (default: /etc/tekton/pipelineRunName)
    TEST_GATES         -- comma-separated phase ids (optional; read from PipelineRun params)
    PIPELINE_TASK_STATUS -- aggregate tasks status from $(tasks.status); preferred over API read
    RHOAI_FBC_NAME  -- ITS catalog component id fallback when extract-fbcf-image did not run
    CLUSTER_PATH, OPERATOR_VERSION_PATH, ARTIFACTS_URL_PATH (required)
    TESTS_SUMMARY_PATH, BVT_GATE_PATH, SMOKE_GATE_PATH, TIER1_GATE_PATH -- gate summaries (optional)
    FBCF_IMAGE_PATH -- Tekton result file path (optional)
    TESTS_SHARED_KUBECONFIG -- staged target-cluster kubeconfig (existing-product FBC probe)
    CLUSTER_SOURCE     -- PipelineRun CLUSTER_SOURCE param (external secret name)
    SMOKE_TEST_OUTPUT_PATH, BVT_TEST_OUTPUT_PATH -- workspace gate sidecars
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

from _bootstrap import ensure_rhoai_e2e_path

ensure_rhoai_e2e_path()

from suite.constants import ANNOTATION_CLUSTER, ANNOTATION_OPERATOR_VERSION, ANNOTATION_TEST_RESULTS_URL  # noqa: E402
from suite.component_catalog import default_components_smoke_config_path, load_components_smoke_catalog  # noqa: E402
from runners.report.pipeline_test_outputs import (  # noqa: E402
    build_finalize_test_output_from_taskruns,
)
from k8s.probe_fbcf_image import resolve_fbcf_image  # noqa: E402
from runners.report.junit_suite_report import (  # noqa: E402
    build_publish_results_gate_summaries,
    build_tier1_gate_summary,
    read_gate_sidecar,
)
from runners.report.publish_context_load import load_publish_context  # noqa: E402
from runners.report.pipelinerun_metadata import build_runtime_metadata  # noqa: E402
from runners.report.pipelinerun_summary import (  # noqa: E402
    pipelinerun_param_value,
    component_smoke_task_status_lines,
    list_pipeline_test_outputs,
)
from suite.its_trigger_params import external_kubeconfig_secret_name  # noqa: E402
from steps.tekton_util import require_env, write_result, write_result_or_path, write_tekton_results_at_paths  # noqa: E402


def _require_tekton_result_path(env_name: str) -> str:
    raw = require_env(env_name)
    if "$(" in raw:
        print(
            f"ERROR: {env_name} was not expanded by Tekton (missing task result?): {raw!r}",
            file=sys.stderr,
        )
        sys.exit(1)
    path = Path(raw)
    if not path.is_absolute():
        print(
            f"ERROR: {env_name} must be an absolute Tekton result path, got {raw!r}",
            file=sys.stderr,
        )
        sys.exit(1)
    return raw


def main() -> int:
    ctx = load_publish_context()
    if ctx is None:
        print("PIPELINE_RUN_NAME or namespace missing", file=sys.stderr)
        return 1
    pr_name = ctx.pipeline_run
    ns = ctx.namespace
    prj = ctx.prj
    taskruns = ctx.taskruns
    test_gates = ctx.test_gates
    pipeline_status = ctx.pipeline_status

    cluster_path = _require_tekton_result_path("CLUSTER_PATH")
    operator_version_path = _require_tekton_result_path("OPERATOR_VERSION_PATH")
    artifacts_url_path = _require_tekton_result_path("ARTIFACTS_URL_PATH")
    tests_summary_path = os.environ.get("TESTS_SUMMARY_PATH", "").strip()
    bvt_gate_path = os.environ.get("BVT_GATE_PATH", "").strip()
    smoke_gate_path = os.environ.get("SMOKE_GATE_PATH", "").strip()
    tier1_gate_path = os.environ.get("TIER1_GATE_PATH", "").strip()
    fbcf_image_path = os.environ.get("FBCF_IMAGE_PATH", "").strip()
    fbcf_fallback = os.environ.get("RHOAI_FBC_NAME", "").strip() or os.environ.get(
        "FBCF_COMPONENT_NAME", ""
    ).strip()

    ann, _labels = build_runtime_metadata(
        pipeline_run=pr_name,
        namespace=ns,
        tests_csv=test_gates,
        prj=prj,
        taskruns=taskruns,
        aggregate_tasks_status=pipeline_status,
    )
    cluster = ann.get(ANNOTATION_CLUSTER, "").strip()
    op_ver = ann.get(ANNOTATION_OPERATOR_VERSION, "").strip() or (
        ctx.operator_version if ctx.operator_version != "(unknown)" else ""
    )
    artifacts = ctx.artifacts_url or ann.get(ANNOTATION_TEST_RESULTS_URL, "").strip()

    payload = build_finalize_test_output_from_taskruns(
        taskruns,
        test_gates=test_gates,
        smoke_path=os.environ.get("SMOKE_TEST_OUTPUT_PATH", "").strip(),
        bvt_path=os.environ.get("BVT_TEST_OUTPUT_PATH", "").strip(),
        list_from_taskruns=lambda tr: list_pipeline_test_outputs(tr, for_ui=False),
    )
    pipeline_test_output_json = (
        json.dumps(payload, separators=(",", ":")) if payload is not None else ""
    )

    product = pipelinerun_param_value(prj, "PRODUCT", "")
    cluster_source = os.environ.get("CLUSTER_SOURCE", "").strip() or pipelinerun_param_value(
        prj, "CLUSTER_SOURCE", ""
    )
    operator_name = os.environ.get("OPERATOR_NAME", "").strip() or pipelinerun_param_value(
        prj, "OPERATOR_NAME", "rhods-operator"
    )
    operator_namespace = os.environ.get("OPERATOR_NAMESPACE", "").strip() or pipelinerun_param_value(
        prj, "OPERATOR_NAMESPACE", "redhat-ods-operator"
    )
    fbcf_image = resolve_fbcf_image(
        taskruns,
        product=product,
        external_kubeconfig_secret=external_kubeconfig_secret_name(cluster_source),
        tests_shared_kubeconfig=os.environ.get("TESTS_SHARED_KUBECONFIG", "").strip(),
        operator_namespace=operator_namespace,
        operator_name=operator_name,
    )
    if not fbcf_image or fbcf_image == "(unknown)":
        fbcf_image = fbcf_fallback or ctx.fbcf_image

    write_result(cluster_path, cluster or "(unknown)")
    write_result(operator_version_path, op_ver or "(unknown)")
    write_result(artifacts_url_path, artifacts)
    if fbcf_image_path and "$(" not in fbcf_image_path:
        write_result_or_path(fbcf_image_path, fbcf_image or "(unknown)")

    catalog = load_components_smoke_catalog(default_components_smoke_config_path())
    task_status_lines = component_smoke_task_status_lines(taskruns, list(catalog.component_ids))
    not_selected = [ln for ln in task_status_lines if ": not selected" in ln]
    if not_selected:
        print(f"Components not selected for this run: {len(not_selected)}", flush=True)

    gate_summaries = build_publish_results_gate_summaries(
        combined_obj=payload,
        bvt_raw=read_gate_sidecar(os.environ.get("BVT_TEST_OUTPUT_PATH", "")),
        smoke_raw=read_gate_sidecar(os.environ.get("SMOKE_TEST_OUTPUT_PATH", "")),
        test_gates=test_gates,
    )
    gate_paths = {
        name: path
        for name, path in (
            ("TESTS_SUMMARY", tests_summary_path),
            ("BVT_GATE", bvt_gate_path),
            ("SMOKE_GATE", smoke_gate_path),
        )
        if path and "$(" not in path
    }
    write_tekton_results_at_paths(
        {name: gate_summaries[name] for name in gate_paths if name in gate_summaries},
        gate_paths,
    )
    if tier1_gate_path and "$(" not in tier1_gate_path:
        write_result(tier1_gate_path, build_tier1_gate_summary(test_gates))

    print(
        f"UI context: cluster={cluster or '(unknown)'} "
        f"operator={op_ver or '(unknown)'} artifacts={'yes' if artifacts else 'no'} "
        f"pipeline_test_output={'yes' if pipeline_test_output_json else 'no'}"
    )
    if ann:
        print(f"Summary annotations available: {', '.join(sorted(ann))}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
