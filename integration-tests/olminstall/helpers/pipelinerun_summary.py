"""Collect olminstall summary fields and patch them onto the PipelineRun for Konflux UI."""

from __future__ import annotations

import json
import os
import ssl
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

from helpers.bvt_artifacts import (
    bvt_unpublished_reason,
    published_artifacts_url_from_taskruns,
    tests_include_bvt,
)
from helpers.tekton_incluster import (
    in_cluster_get,
    kubernetes_api_base_url,
    list_taskruns_in_cluster,
    namespace_from_env,
    pipeline_run_name_from_env,
    result_map,
    task_name,
    validate_kubernetes_api_url,
)
from helpers.constants import (
    DEFAULT_ARTIFACT_BROWSER_REPO_PATH,
    DEFAULT_ARTIFACT_BROWSER_URL,
    OLMINSTALL_CTX_PRINT_KEYS,
)

SUMMARY_ANNOTATION_KEYS: tuple[str, ...] = (
    "olminstall.fbcf-image",
    "olminstall.operator-version",
    "olminstall.ephemeral-cluster",
    "olminstall.test-results-url",
    "olminstall.artifacts-status",
    "olminstall.pipeline-test-output",
)

SUMMARY_ANNOTATION_LABELS: dict[str, str] = {
    "olminstall.fbcf-image": "FBCF image",
    "olminstall.operator-version": "Operator version",
    "olminstall.ephemeral-cluster": "Ephemeral CTI",
    "olminstall.test-results-url": "Test Results",
    "olminstall.artifacts-status": "Artifacts status",
    "olminstall.pipeline-test-output": "Pipeline test output",
    "olminstall.run-owner": "Run owner",
    "olminstall.product": "Product",
    "olminstall.update-channel": "Update channel",
    "olminstall.rhoai-version": "RHOAI version",
    "olminstall.ocp-version": "OCP version (ephemeral)",
    "olminstall.scripts-repo-url": "Scripts repo",
    "olminstall.scripts-repo-revision": "Scripts branch/revision",
    "olminstall.tests": "Test phases (TESTS)",
    "olminstall.slack-channel-id": "Slack channel ID",
    "olminstall.bvt-env-only": "BVT env-only",
}


def _k8s_request(
    method: str,
    url: str,
    token: str,
    ca_path: Path,
    *,
    body: dict[str, Any] | None = None,
) -> dict[str, Any]:
    validate_kubernetes_api_url(url)
    ctx = ssl.create_default_context(cafile=str(ca_path))
    data = None
    headers = {"Authorization": f"Bearer {token}"}
    if body is not None:
        data = json.dumps(body).encode("utf-8")
        headers["Content-Type"] = "application/merge-patch+json"
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    with urllib.request.urlopen(req, context=ctx, timeout=60) as resp:
        raw = resp.read()
    if not raw:
        return {}
    parsed = json.loads(raw.decode("utf-8"))
    return parsed if isinstance(parsed, dict) else {}


def get_pipelinerun_json(pipeline_run: str, namespace: str) -> dict[str, Any]:
    token_path = Path("/var/run/secrets/kubernetes.io/serviceaccount/token")
    ca_path = Path("/var/run/secrets/kubernetes.io/serviceaccount/ca.crt")
    base = kubernetes_api_base_url()
    if not (pipeline_run and namespace and token_path.is_file() and ca_path.is_file() and base):
        return {}
    token = token_path.read_text(encoding="utf-8")
    url = (
        f"{base}/apis/tekton.dev/v1/namespaces/"
        f"{urllib.parse.quote(namespace)}/pipelineruns/{urllib.parse.quote(pipeline_run)}"
    )
    try:
        return in_cluster_get(url, token, ca_path)
    except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, OSError, ValueError):
        return {}


def pipelinerun_param_value(prj: dict[str, Any], name: str, default: str = "") -> str:
    for p in prj.get("spec", {}).get("params", []) or []:
        if p.get("name") != name:
            continue
        val = p.get("value")
        if isinstance(val, str) and val.strip():
            return val.strip()
    return default


def predicted_artifacts_browser_url(prj: dict[str, Any], pipeline_run: str) -> str:
    base = pipelinerun_param_value(prj, "ARTIFACT_BROWSER_URL", DEFAULT_ARTIFACT_BROWSER_URL).rstrip("/")
    repo = pipelinerun_param_value(prj, "ARTIFACT_BROWSER_REPO_PATH", DEFAULT_ARTIFACT_BROWSER_REPO_PATH).strip(
        "/"
    )
    pr_name = (pipeline_run or "").strip()
    if not pr_name:
        return f"{base}/{repo}/<pipelinerun>-bvt/"
    return f"{base}/{repo}/{pr_name}-bvt/"


def task_result(taskruns: list[dict[str, Any]], task_substr: str, result_name: str) -> str:
    needle = task_substr.lower()
    for tr in taskruns:
        if needle not in task_name(tr).lower():
            continue
        val = result_map(tr).get(result_name, "").strip()
        if val:
            return val
    return ""


def pick_pipeline_test_output(taskruns: list[dict[str, Any]]) -> str:
    for prefer in ("bvt-health-checks-with-eaas", "bvt-health-checks-no-eaas"):
        val = task_result(taskruns, prefer, "TEST_OUTPUT")
        if val:
            return val
    return task_result(taskruns, "install-operator", "INSTALL_STATUS")


def collect_summary_annotations(
    *,
    pipeline_run: str,
    namespace: str,
    tests_csv: str,
    prj: dict[str, Any] | None = None,
    taskruns: list[dict[str, Any]] | None = None,
) -> dict[str, str]:
    """Build ``olminstall.*`` annotations for Konflux UI and CLI summaries."""
    doc = prj if prj is not None else get_pipelinerun_json(pipeline_run, namespace)
    runs = taskruns if taskruns is not None else list_taskruns_in_cluster(pipeline_run, namespace)
    out: dict[str, str] = {}

    fbcf = task_result(runs, "extract-fbcf-image", "FBCF_IMAGE")
    if fbcf:
        out["olminstall.fbcf-image"] = fbcf

    op_ver = task_result(runs, "install-operator", "OPERATOR_VERSION")
    if op_ver and op_ver not in ("(see pipeline run logs)", "(unknown)"):
        out["olminstall.operator-version"] = op_ver

    cluster = task_result(runs, "provision-cluster", "clusterName")
    if cluster:
        out["olminstall.ephemeral-cluster"] = cluster

    if tests_include_bvt(tests_csv):
        published = published_artifacts_url_from_taskruns(runs)
        if published:
            out["olminstall.test-results-url"] = published
        else:
            out["olminstall.test-results-url"] = predicted_artifacts_browser_url(doc, pipeline_run)
            out["olminstall.artifacts-status"] = bvt_unpublished_reason(runs)

    test_output = pick_pipeline_test_output(runs)
    if test_output:
        out["olminstall.pipeline-test-output"] = test_output[:500]

    return out


def pipeline_succeeded_status_label(prj: dict[str, Any]) -> str:
    conds = (prj.get("status") or {}).get("conditions", []) or []
    cond = next(
        (c for c in conds if isinstance(c, dict) and c.get("type") == "Succeeded"),
        {},
    )
    if not isinstance(cond, dict):
        return "Unknown"
    st = cond.get("status", "")
    reason = (cond.get("reason") or "").strip()
    if st == "True":
        return "Succeeded"
    if st == "False":
        return reason or "Failed"
    return reason or "Unknown"


def format_summary_log_block(
    *,
    pipeline_run: str,
    prj: dict[str, Any],
    summary_annotations: dict[str, str],
) -> str:
    """Human-readable block for post-results step logs (Konflux task log panel)."""
    merged = dict((prj.get("metadata") or {}).get("annotations") or {})
    merged.update(summary_annotations)
    status = pipeline_succeeded_status_label(prj)
    lines = [
        "===========================================================",
        " Olminstall run summary (post-results)",
        "===========================================================",
        f"  PipelineRun  : {pipeline_run}  [{status}]",
    ]
    for key in (
        "olminstall.operator-version",
        "olminstall.fbcf-image",
        "olminstall.ephemeral-cluster",
        "olminstall.test-results-url",
        "olminstall.artifacts-status",
    ):
        val = (merged.get(key) or "").strip()
        if val:
            label = SUMMARY_ANNOTATION_LABELS.get(key, key)
            lines.append(f"  {label + ':':16} {val}")
    lines.append("")
    lines.append("Trigger context (PipelineRun annotations):")
    ctx_any = False
    for key in OLMINSTALL_CTX_PRINT_KEYS:
        val = (merged.get(key) or "").strip()
        if not val:
            continue
        ctx_any = True
        label = SUMMARY_ANNOTATION_LABELS.get(key, key)
        lines.append(f"  {label}: {val}")
    if not ctx_any:
        lines.append("  (no olminstall.* annotations on this PipelineRun)")
    test_out = (merged.get("olminstall.pipeline-test-output") or "").strip()
    if test_out:
        lines.append("")
        lines.append(f"  Pipeline test output: {test_out}")
    lines.append("===========================================================")
    return "\n".join(lines)


def write_summary_tekton_results(summary_annotations: dict[str, str]) -> None:
    """Expose key fields as Tekton task results (Konflux PipelineRun → Results panel)."""
    from helpers.tekton_util import write_result

    mapping = {
        "ARTIFACTS_URL_PATH": "olminstall.test-results-url",
        "TEST_OUTPUT_PATH": "olminstall.pipeline-test-output",
        "OPERATOR_VERSION_PATH": "olminstall.operator-version",
        "EPHEMERAL_CLUSTER_PATH": "olminstall.ephemeral-cluster",
    }
    for env_key, ann_key in mapping.items():
        path = os.environ.get(env_key, "").strip()
        if path:
            write_result(path, summary_annotations.get(ann_key, ""))


def merge_patch_pipelinerun_annotations(
    pipeline_run: str,
    namespace: str,
    annotations: dict[str, str],
) -> bool:
    if not annotations:
        return True
    token_path = Path("/var/run/secrets/kubernetes.io/serviceaccount/token")
    ca_path = Path("/var/run/secrets/kubernetes.io/serviceaccount/ca.crt")
    base = kubernetes_api_base_url()
    if not (pipeline_run and namespace and token_path.is_file() and ca_path.is_file() and base):
        return False
    token = token_path.read_text(encoding="utf-8")
    url = (
        f"{base}/apis/tekton.dev/v1/namespaces/"
        f"{urllib.parse.quote(namespace)}/pipelineruns/{urllib.parse.quote(pipeline_run)}"
    )
    body = {"metadata": {"annotations": annotations}}
    try:
        _k8s_request("PATCH", url, token, ca_path, body=body)
        return True
    except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, OSError, ValueError) as exc:
        print(f"WARN: could not patch PipelineRun annotations: {exc}", file=sys.stderr)
        return False
