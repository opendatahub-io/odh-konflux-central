"""Collect rhoai-e2e summary fields and patch them onto the PipelineRun for Konflux UI."""

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

from runners.report.junit_suite_report import (
    format_test_output_for_ui,
    format_test_outputs_for_ui,
    suite_lines_from_test_output,
)
from runners.report.test_artifacts import (
    BVT_TASK_SUBSTR,
    SMOKE_TASK_SUBSTR,
    published_artifacts_url_from_taskruns,
    resolve_artifacts_url_for_ui,
    tests_include_bvt,
    tests_include_smoke,
    unpublished_reason,
)
from steps.tekton_incluster import (
    in_cluster_get,
    kubernetes_api_base_url,
    list_taskruns_in_cluster,
    namespace_from_env,
    pipeline_run_name_from_env,
    result_map,
    task_name,
    validate_kubernetes_api_url,
)
from suite.constants import (
    ANNOTATION_CLUSTER,
    ANNOTATION_OPERATOR_VERSION,
    ANNOTATION_PRODUCT,
    ANNOTATION_RUN_OWNER,
    ANNOTATION_TESTS,
    ANNOTATION_TEST_RESULTS_URL,
    RHOAI_E2E_ANNOTATION_LABELS,
    RHOAI_E2E_CTX_PRINT_KEYS,
    RHOAI_E2E_SUMMARY_ANNOTATION_KEYS,
)

SUMMARY_ANNOTATION_KEYS: tuple[str, ...] = RHOAI_E2E_SUMMARY_ANNOTATION_KEYS

SUMMARY_ANNOTATION_LABELS: dict[str, str] = {
    k: RHOAI_E2E_ANNOTATION_LABELS[k]
    for k in RHOAI_E2E_SUMMARY_ANNOTATION_KEYS
    if k in RHOAI_E2E_ANNOTATION_LABELS
}
SUMMARY_ANNOTATION_LABELS[ANNOTATION_PRODUCT] = "Product (installed)"


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


def task_result(taskruns: list[dict[str, Any]], task_substr: str, result_name: str) -> str:
    needle = task_substr.lower()
    for tr in taskruns:
        if needle not in task_name(tr).lower():
            continue
        val = result_map(tr).get(result_name, "").strip()
        if val:
            return val
    return ""


def component_pipeline_task_name(component_id: str) -> str:
    return f"test-{component_id.replace('_', '-')}"


_TASKRUN_SKIPPED_REASONS = frozenset(
    {"TaskRunSkipped", "PipelineRunSkipped", "Skipped", "WhenExpressionsEvaluatedToFalse"}
)


def component_smoke_task_status_lines(
    taskruns: list[dict[str, Any]],
    component_ids: list[str],
) -> list[str]:
    """Per-component pipeline task status for RUN_SUMMARY (selected / skipped / ran)."""
    from steps.tekton_incluster import task_reason, task_succeeded_detail

    by_task: dict[str, dict[str, Any]] = {}
    for tr in taskruns:
        name = task_name(tr).lower()
        if name.startswith("test-") and name != "test-finalize":
            by_task[name] = tr

    lines: list[str] = []
    for cid in component_ids:
        task_key = component_pipeline_task_name(cid).lower()
        selected = task_result(taskruns, "parse-pipeline-tests", f"RUN_SMOKE_{cid}").lower()
        if selected == "false":
            lines.append(f"component_{cid}: not selected")
            continue
        tr = by_task.get(task_key)
        if tr is None:
            lines.append(f"component_{cid}: no TaskRun")
            continue
        reason = task_reason(tr)
        if reason in _TASKRUN_SKIPPED_REASONS:
            lines.append(f"component_{cid}: skipped ({reason})")
            continue
        status, _, message = task_succeeded_detail(tr)
        if status == "True":
            lines.append(f"component_{cid}: ran (Succeeded)")
        elif reason:
            detail = message.strip() or reason
            lines.append(f"component_{cid}: ran (Failed — {detail[:120]})")
        else:
            lines.append(f"component_{cid}: ran ({reason or 'unknown'})")
    return lines


def pick_pipeline_test_output(taskruns: list[dict[str, Any]]) -> str:
    """Return raw TEST_OUTPUT JSON from the highest-priority phase that ran."""
    for prefer in _ALL_TEST_OUTPUT_TASKS:
        val = task_result(taskruns, prefer, "TEST_OUTPUT")
        if val:
            return val
    for install_task in (
        "install-rhoai-external",
        "install-odh-external",
        "install-rhoai",
        "install-odh",
        "install-operator",
        "install-operator-external",
    ):
        val = task_result(taskruns, install_task, "INSTALL_STATUS")
        if val:
            return val
    return ""


_CONFORMA_GATE_TEST_OUTPUT_TASKS: tuple[str, ...] = ("wait-for-conforma",)

_BVT_TEST_OUTPUT_TASKS: tuple[str, ...] = ("bvt-health-checks",)

_SMOKE_TEST_OUTPUT_TASKS: tuple[str, ...] = ("test-finalize",)

_ALL_TEST_OUTPUT_TASKS: tuple[str, ...] = (
    _CONFORMA_GATE_TEST_OUTPUT_TASKS + _BVT_TEST_OUTPUT_TASKS + _SMOKE_TEST_OUTPUT_TASKS
)


def list_pipeline_test_outputs(
    taskruns: list[dict[str, Any]],
    *,
    for_ui: bool = False,
) -> list[tuple[str, str]]:
    """Ordered (gate, raw_json) for each phase that emitted TEST_OUTPUT.

    When *for_ui* is true and ``test-finalize`` wrote combined TEST_OUTPUT, return
    that single blob so gate summaries are not duplicated with ``bvt-health-checks``.
    """
    if for_ui:
        finalize = task_result(taskruns, "test-finalize", "TEST_OUTPUT")
        if finalize and finalize.lstrip().startswith("{"):
            return [("combined", finalize)]

    out: list[tuple[str, str]] = []
    for gate, prefers in (("bvt", _BVT_TEST_OUTPUT_TASKS), ("smoke", _SMOKE_TEST_OUTPUT_TASKS)):
        for prefer in prefers:
            val = task_result(taskruns, prefer, "TEST_OUTPUT")
            if val and val.lstrip().startswith("{"):
                out.append((gate, val))
                break
    return out


def pick_smoke_test_output(taskruns: list[dict[str, Any]]) -> str:
    for prefer in _SMOKE_TEST_OUTPUT_TASKS:
        val = task_result(taskruns, prefer, "TEST_OUTPUT")
        if val:
            return val
    return ""


def pipeline_aggregate_status(prj: dict[str, Any]) -> str:
    """PipelineRun Succeeded condition mapped to Succeeded / Failed / Completed."""
    label = pipeline_succeeded_status_label(prj)
    if label in ("Succeeded", "Failed", "Completed"):
        return label
    if label in ("Running", "Pending"):
        return label
    if label in ("Unknown", ""):
        return "Unknown"
    # PipelineRun condition reason when Failed (e.g. Failed, PipelineRunTimeout)
    return "Failed"


def collect_summary_annotations(
    *,
    pipeline_run: str,
    namespace: str,
    tests_csv: str,
    prj: dict[str, Any] | None = None,
    taskruns: list[dict[str, Any]] | None = None,
) -> dict[str, str]:
    """Build final ``rhoai-e2e.*`` annotations (cluster, installed product, operator, artifacts)."""
    from runners.report.pipelinerun_metadata import build_runtime_metadata

    ann, _labels = build_runtime_metadata(
        pipeline_run=pipeline_run,
        namespace=namespace,
        tests_csv=tests_csv,
        prj=prj,
        taskruns=taskruns,
    )
    return ann


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
    report_portal_url: str = "",
    jira_url: str = "",
) -> str:
    """Human-readable block for pipeline-run-summary step logs (Konflux task log panel + CLI)."""
    merged = dict((prj.get("metadata") or {}).get("annotations") or {})
    merged.update(summary_annotations)
    status = pipeline_succeeded_status_label(prj)
    lines = [
        "===========================================================",
        " Olminstall run summary (publish-results)",
        "===========================================================",
        f"  PipelineRun  : {pipeline_run}  [{status}]",
    ]
    for key in SUMMARY_ANNOTATION_KEYS:
        val = (merged.get(key) or "").strip()
        if val:
            label = SUMMARY_ANNOTATION_LABELS.get(key, key)
            lines.append(f"  {label + ':':16} {val}")
    lines.append("")
    lines.append("Trigger context (PipelineRun annotations):")
    ctx_any = False
    for key in RHOAI_E2E_CTX_PRINT_KEYS:
        val = (merged.get(key) or "").strip()
        if not val:
            continue
        ctx_any = True
        label = SUMMARY_ANNOTATION_LABELS.get(key, key)
        lines.append(f"  {label}: {val}")
    if not ctx_any:
        lines.append("  (no rhoai-e2e.* annotations on this PipelineRun)")
    published: list[tuple[str, str]] = []
    if report_portal_url.strip():
        published.append(("Report portal", report_portal_url.strip()))
    if jira_url.strip():
        published.append(("Jira", jira_url.strip()))
    if published:
        lines.append("")
        lines.append("Published externally:")
        for label, url in published:
            lines.append(f"  {label + ':':16} {url}")
    lines.append("===========================================================")
    return "\n".join(lines)


def _existing_tekton_result(path: str) -> str:
    p = Path(path)
    if not p.is_file():
        return ""
    try:
        return p.read_text(encoding="utf-8").strip()
    except OSError:
        return ""


def write_summary_tekton_results(summary_annotations: dict[str, str]) -> None:
    """Backfill Tekton result files from summary annotations when step env paths are set."""
    from steps.tekton_util import write_result

    artifacts_url = summary_annotations.get(ANNOTATION_TEST_RESULTS_URL, "").strip()
    operator_version = summary_annotations.get(ANNOTATION_OPERATOR_VERSION, "").strip()
    cluster = summary_annotations.get(ANNOTATION_CLUSTER, "").strip()

    for env_key, value in (
        ("ARTIFACTS_URL_PATH", artifacts_url),
        ("CLUSTER_PATH", cluster),
        ("OPERATOR_VERSION_PATH", operator_version),
    ):
        path = os.environ.get(env_key, "").strip()
        if not path:
            continue
        existing = _existing_tekton_result(path)
        new_val = value or existing
        if new_val or env_key == "CLUSTER_PATH":
            write_result(path, new_val or "(unknown)")


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
