"""Shared PipelineRun / TaskRun context for publish-results and UI collectors."""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any

from runners.report.pipelinerun_summary import (
    get_pipelinerun_json,
    namespace_from_env,
    pipeline_aggregate_status,
    pipeline_run_name_from_env,
    pipelinerun_param_value,
    task_result,
)
from k8s.probe_operator_version import resolve_operator_version
from suite.constants import DEFAULT_ARTIFACT_BROWSER_REPO_PATH, DEFAULT_ARTIFACT_BROWSER_URL
from suite.its_trigger_params import external_kubeconfig_secret_name
from runners.report.test_artifacts import resolve_artifacts_url_for_ui
from steps.tekton_incluster import list_taskruns_in_cluster


@dataclass(frozen=True)
class PublishContext:
    pipeline_run: str
    namespace: str
    prj: dict[str, Any]
    taskruns: list[dict[str, Any]]
    test_gates: str
    pipeline_status: str
    fbcf_image: str
    operator_version: str
    artifacts_url: str


def load_publish_context() -> PublishContext | None:
    """Load shared facts; returns None when PIPELINE_RUN_NAME or namespace is missing."""
    pr_name = pipeline_run_name_from_env()
    if not pr_name:
        return None
    ns = namespace_from_env()
    if not ns:
        return None

    prj = get_pipelinerun_json(pr_name, ns)
    taskruns = list_taskruns_in_cluster(pr_name, ns)
    cluster_source = os.environ.get("CLUSTER_SOURCE", "").strip() or pipelinerun_param_value(
        prj, "CLUSTER_SOURCE", ""
    )
    test_gates = os.environ.get("TEST_GATES", "").strip() or pipelinerun_param_value(
        prj, "TEST_GATES", pipelinerun_param_value(prj, "TESTS", "")
    )
    pipeline_status = os.environ.get("PIPELINE_TASK_STATUS", "").strip() or pipeline_aggregate_status(prj)
    fbcf_fallback = (
        os.environ.get("RHOAI_FBC_NAME", "").strip()
        or os.environ.get("FBCF_COMPONENT_NAME", "").strip()
    )
    fbcf_image = task_result(taskruns, "extract-fbcf-image", "FBCF_IMAGE") or fbcf_fallback or "(unknown)"
    operator_name = os.environ.get("OPERATOR_NAME", "").strip() or pipelinerun_param_value(
        prj, "OPERATOR_NAME", "rhods-operator"
    )
    op_ver = resolve_operator_version(
        taskruns,
        pipeline_run=pr_name,
        namespace=ns,
        external_kubeconfig_secret=external_kubeconfig_secret_name(cluster_source),
        operator_namespace=pipelinerun_param_value(prj, "OPERATOR_NAMESPACE", "redhat-ods-operator"),
        operator_name=operator_name,
        product=pipelinerun_param_value(prj, "PRODUCT", ""),
    )
    browser_base = pipelinerun_param_value(prj, "ARTIFACT_BROWSER_URL", "")
    repo_path = pipelinerun_param_value(prj, "ARTIFACT_BROWSER_REPO_PATH", "odh-ci-artifacts")
    artifacts = resolve_artifacts_url_for_ui(
        tests_csv=test_gates,
        pipeline_run=pr_name,
        taskruns=taskruns,
        browser_base=browser_base or DEFAULT_ARTIFACT_BROWSER_URL,
        repo_path=repo_path or DEFAULT_ARTIFACT_BROWSER_REPO_PATH,
    )
    return PublishContext(
        pipeline_run=pr_name,
        namespace=ns,
        prj=prj,
        taskruns=taskruns,
        test_gates=test_gates,
        pipeline_status=pipeline_status,
        fbcf_image=fbcf_image,
        operator_version=op_ver or "(unknown)",
        artifacts_url=artifacts,
    )
