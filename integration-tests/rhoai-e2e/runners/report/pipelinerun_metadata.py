"""PipelineRun labels and rhoai-e2e.* annotations (filterable, minimal set)."""

from __future__ import annotations

import json
import re
import ssl
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

from suite.constants import (
    ANNOTATION_BUILD_COMMIT_SHA,
    ANNOTATION_BUILD_REPO,
    ANNOTATION_CLUSTER,
    ANNOTATION_CLUSTER_KEY,
    ANNOTATION_FBCF_IMAGE,
    ANNOTATION_OPERATOR_VERSION,
    ANNOTATION_PRODUCT,
    ANNOTATION_REFERENCE,
    ANNOTATION_RUN_OWNER,
    ANNOTATION_SHA_URL,
    ANNOTATION_TARGET_BRANCH,
    ANNOTATION_TESTS,
    ANNOTATION_TEST_RESULTS_URL,
    ANNOTATION_TRIGGER_COMMAND,
    ANNOTATION_TRIGGER_TYPE,
    EVENT_TYPE_INCOMING,
    EVENT_TYPE_PUSH,
    LABEL_CLUSTER,
    LABEL_OUTCOME,
    LABEL_PRODUCT,
    LABEL_RUN_OWNER,
    LABEL_TARGET,
    LABEL_TEST_SHA,
    LABEL_TEST_URL_ORG,
    LABEL_TEST_URL_REPOSITORY,
    LABEL_KONFLUX_APPLICATION,
    LABEL_TRIGGER_EVENT_TYPE,
    TRIGGER_TYPE_MANUAL,
    DEFAULT_ARTIFACT_BROWSER_REPO_PATH,
    DEFAULT_ARTIFACT_BROWSER_URL,
)
from suite.its_trigger_params import CLUSTER_SOURCE_EPHC, external_kubeconfig_secret_name, is_external_cluster_source
from k8s.github_url import normalize_https_git_url, parse_github_org_repo
from runners.report.konflux_pac_metadata import (
    build_pull_request_pac_metadata,
    extract_pac_metadata_from_resource,
    find_upstream_pull_request,
    resolve_branch_head_sha,
    snapshot_has_pull_request_pac,
)
from k8s.probe_operator_version import resolve_operator_version
from runners.report.pipelinerun_summary import (
    get_pipelinerun_json,
    pipelinerun_param_value,
    pipeline_succeeded_status_label,
    task_result,
)
from steps.tekton_incluster import kubernetes_api_base_url, list_taskruns_in_cluster, validate_kubernetes_api_url
from runners.report.test_artifacts import resolve_artifacts_url_for_ui

_K8S_LABEL_VALUE_RE = re.compile(r"^[a-zA-Z0-9]([-a-zA-Z0-9_.]*[a-zA-Z0-9])?$")
_MAJOR_MINOR_RE = re.compile(r"^(\d+)\.(\d+)")


def sanitize_k8s_label_value(raw: str, *, max_len: int = 63) -> str:
    """DNS-1123 label value safe for ``oc label``."""
    text = (raw or "").strip().lower()
    if not text:
        return ""
    text = text.replace(":", "-").replace("/", "-").replace(",", "-")
    text = re.sub(r"[^a-z0-9._-]+", "-", text)
    text = re.sub(r"-+", "-", text).strip("-._")
    candidate = text[:max_len].strip("-._")
    if not candidate or not _K8S_LABEL_VALUE_RE.match(candidate):
        # Fall back to alnum-only chunk
        candidate = re.sub(r"[^a-z0-9]+", "-", (raw or "").strip().lower()).strip("-")[:max_len].strip("-._")
    return candidate if candidate else ""


def infer_installed_product(operator_name: str, operator_version: str) -> str:
    """Cluster truth: rhoai, odh, or unknown (not install intent)."""
    op = (operator_name or "").strip().lower()
    ver = (operator_version or "").strip()
    if ver in ("", "(unknown)", "(see pipeline run logs)"):
        ver = ""
    if op == "opendatahub-operator":
        return "odh" if ver else "unknown"
    if op in ("rhods-operator", "rhoai-operator", "odh-rhoai-operator"):
        return "rhoai" if ver else "unknown"
    if ver:
        m = _MAJOR_MINOR_RE.match(ver)
        if m and int(m.group(1)) >= 3:
            return "rhoai"
        if m:
            return "odh"
    return "unknown"


def resolve_target_type(prj: dict[str, Any]) -> str:
    """external | ephc | stub from PipelineRun params."""
    source = pipelinerun_param_value(prj, "CLUSTER_SOURCE", "").strip()
    product = pipelinerun_param_value(prj, "PRODUCT", "").strip().lower()
    if is_external_cluster_source(source):
        return "external"
    if source == CLUSTER_SOURCE_EPHC or (not source and product in ("rhoai", "odh")):
        return "ephc"
    return "stub"


def outcome_label(prj: dict[str, Any], *, aggregate_tasks_status: str = "") -> str:
    """succeeded | failed | running for rhoai-e2e.outcome label."""
    agg = (aggregate_tasks_status or "").strip()
    if agg == "Succeeded":
        return "succeeded"
    if agg in ("Failed", "Completed"):
        return "failed"
    status = pipeline_succeeded_status_label(prj)
    if status == "Succeeded":
        return "succeeded"
    if status in ("Failed", "Completed"):
        return "failed"
    if status in ("Running", "Pending", "PipelineRunPending", "ResolvingPipelineRef"):
        return "running"
    return "failed" if status else "running"


def _read_annotation(prj: dict[str, Any], key: str) -> str:
    ann = (prj.get("metadata") or {}).get("annotations") or {}
    return (ann.get(key) or "").strip()


def _read_label(prj: dict[str, Any], key: str) -> str:
    labels = (prj.get("metadata") or {}).get("labels") or {}
    return (labels.get(key) or "").strip()


def cluster_label_from_cluster_source(cluster_source: str) -> str:
    """Derive a short cluster label from a tenant kubeconfig Secret name."""
    secret = (cluster_source or "").strip()
    if not secret or secret == CLUSTER_SOURCE_EPHC:
        return ""
    for prefix in ("rhoai-e2e-kubeconfig-", "kubeconfig-"):
        if secret.startswith(prefix):
            return secret[len(prefix) :].strip("-") or secret
    return secret


def _k8s_merge_patch(
    pipeline_run: str,
    namespace: str,
    *,
    annotations: dict[str, str] | None = None,
    labels: dict[str, str] | None = None,
) -> bool:
    if not annotations and not labels:
        return True
    token_path = Path("/var/run/secrets/kubernetes.io/serviceaccount/token")
    ca_path = Path("/var/run/secrets/kubernetes.io/serviceaccount/ca.crt")
    base = kubernetes_api_base_url()
    if not (pipeline_run and namespace and token_path.is_file() and ca_path.is_file() and base):
        return False
    meta: dict[str, Any] = {}
    if annotations:
        meta["annotations"] = annotations
    if labels:
        meta["labels"] = labels
    token = token_path.read_text(encoding="utf-8")
    url = (
        f"{base}/apis/tekton.dev/v1/namespaces/"
        f"{urllib.parse.quote(namespace)}/pipelineruns/{urllib.parse.quote(pipeline_run)}"
    )
    body = {"metadata": meta}
    ctx = ssl.create_default_context(cafile=str(ca_path))
    data = json.dumps(body).encode("utf-8")
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/merge-patch+json",
    }
    req = urllib.request.Request(url, data=data, headers=headers, method="PATCH")
    try:
        validate_kubernetes_api_url(url)
        with urllib.request.urlopen(req, context=ctx, timeout=60) as resp:
            resp.read()
        return True
    except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, OSError, ValueError) as exc:
        print(f"WARN: could not patch PipelineRun metadata: {exc}", file=sys.stderr)
        return False


def merge_patch_pipelinerun_labels(
    pipeline_run: str,
    namespace: str,
    labels: dict[str, str],
) -> bool:
    clean: dict[str, str] = {}
    for key, value in labels.items():
        sanitized = sanitize_k8s_label_value(value)
        if sanitized:
            clean[key] = sanitized
    return _k8s_merge_patch(pipeline_run, namespace, labels=clean)


_TRIGGER_COMMAND_MAX_LEN = 4096
_DIGEST_SHA256_RE = re.compile(r"sha256:([0-9a-f]{64})", re.IGNORECASE)


def short_digest_from_image(image: str) -> str:
    """12-char digest prefix or tag tail for Konflux Reference commit chip."""
    text = (image or "").strip()
    if not text:
        return ""
    m = _DIGEST_SHA256_RE.search(text)
    if m:
        return m.group(1)[:12]
    if "@" in text:
        return text.rsplit("@", 1)[-1][:12]
    if ":" in text:
        return text.rsplit(":", 1)[-1][:12]
    return text[-12:] if len(text) > 12 else text


def quay_repository_web_url(pullspec: str) -> str:
    """``https://quay.io/repository/org/repo`` from a container pullspec."""
    text = (pullspec or "").strip()
    if not text.startswith("quay.io/"):
        return ""
    path = text.removeprefix("quay.io/").split("@", 1)[0].split(":", 1)[0]
    if not path or "/" not in path:
        return ""
    return f"https://quay.io/repository/{path}"


def quay_manifest_web_url(pullspec: str) -> str:
    """Quay manifest page for a digest pullspec."""
    text = (pullspec or "").strip()
    m = _DIGEST_SHA256_RE.search(text)
    repo_url = quay_repository_web_url(text)
    if not repo_url or not m:
        return ""
    return f"{repo_url}/manifest/sha256:{m.group(1).lower()}"


def build_konflux_activity_metadata(
    *,
    fbcf_image: str,
    scripts_git_url: str,
    scripts_git_revision: str,
    upstream_git_url: str = "",
    fbc_snapshot_meta: dict[str, Any] | None = None,
    local_git_repo: Path | None = None,
) -> tuple[dict[str, str], dict[str, str]]:
    """Annotations and labels for Konflux Activity Trigger / Reference columns."""
    if fbc_snapshot_meta:
        pac_labels, pac_ann = extract_pac_metadata_from_resource(fbc_snapshot_meta)
        if snapshot_has_pull_request_pac(pac_labels):
            return pac_ann, pac_labels

    upstream = normalize_https_git_url(upstream_git_url) or normalize_https_git_url(scripts_git_url)
    head_url = normalize_https_git_url(scripts_git_url)
    branch = (scripts_git_revision or "").strip()

    pr = find_upstream_pull_request(head_git_url=head_url, branch=branch, upstream_git_url=upstream)
    if pr:
        return build_pull_request_pac_metadata(pr=pr, repo_git_url=f"https://github.com/{pr.pr_org}/{pr.pr_repo}.git")

    annotations: dict[str, str] = {}
    labels: dict[str, str] = {LABEL_TRIGGER_EVENT_TYPE: EVENT_TYPE_PUSH}

    git_url = head_url or upstream
    git_rev = branch
    org, repo = parse_github_org_repo(git_url)
    if org:
        labels[LABEL_TEST_URL_ORG] = org
    if repo:
        labels[LABEL_TEST_URL_REPOSITORY] = repo

    head_sha = (
        resolve_branch_head_sha(git_url=head_url or upstream, branch=branch, local_repo=local_git_repo)
        if branch
        else ""
    )
    if head_sha:
        annotations[ANNOTATION_BUILD_COMMIT_SHA] = head_sha
        labels[LABEL_TEST_SHA] = head_sha
        if git_url:
            annotations[ANNOTATION_BUILD_REPO] = f"{git_url}?rev={head_sha}"
            annotations[ANNOTATION_SHA_URL] = f"{git_url}/commit/{head_sha}"
        if branch:
            annotations[ANNOTATION_TARGET_BRANCH] = branch
    elif git_url and git_rev:
        annotations[ANNOTATION_BUILD_REPO] = f"{git_url}?rev={urllib.parse.quote(git_rev, safe='')}"
        annotations[ANNOTATION_TARGET_BRANCH] = git_rev
        annotations[ANNOTATION_SHA_URL] = f"{git_url}/tree/{urllib.parse.quote(git_rev, safe='')}"

    img = (fbcf_image or "").strip()
    use_incoming = not head_sha and not branch
    if use_incoming:
        labels[LABEL_TRIGGER_EVENT_TYPE] = EVENT_TYPE_INCOMING
    digest = short_digest_from_image(img)
    if digest and use_incoming:
        annotations[ANNOTATION_BUILD_COMMIT_SHA] = digest
        labels[LABEL_TEST_SHA] = digest
        manifest_url = quay_manifest_web_url(img)
        quay_repo = quay_repository_web_url(img)
        if manifest_url:
            annotations[ANNOTATION_SHA_URL] = manifest_url
        elif quay_repo:
            annotations[ANNOTATION_SHA_URL] = quay_repo

    return annotations, labels


def shell_quote_arg(arg: str) -> str:
    """Quote a single argv element for replay in a shell command."""
    text = arg or ""
    if not text:
        return "''"
    if re.fullmatch(r"[\w@%+=:,./-]+", text):
        return text
    return "'" + text.replace("'", "'\"'\"'") + "'"


def format_rhoai_e2e_trigger_command(script_dir: Path, argv: list[str]) -> str:
    """Replayable ``python3 …/rhoai_e2e.py …`` string from trigger argv."""
    repo_root = script_dir.parent.parent
    prog = script_dir / "rhoai_e2e.py"
    try:
        rel = prog.relative_to(repo_root)
        prog_text = f"python3 {rel.as_posix()}"
    except ValueError:
        prog_text = f"python3 {prog}"
    if not argv:
        return prog_text
    cmd = prog_text + " " + " ".join(shell_quote_arg(a) for a in argv)
    if len(cmd) <= _TRIGGER_COMMAND_MAX_LEN:
        return cmd
    return cmd[: _TRIGGER_COMMAND_MAX_LEN - 3] + "..."


def fbcf_image_from_snapshot_spec(spec: dict[str, Any] | None) -> str:
    if not spec:
        return ""
    for comp in spec.get("components") or []:
        img = ((comp or {}).get("containerImage") or "").strip()
        if img:
            return img
    return ""


def build_reference_text(*, fbcf_image: str, ocp_version: str = "") -> str:
    """Human-readable Reference: FBC catalog pullspec and optional OCP minor."""
    img = (fbcf_image or "").strip()
    if not img:
        return ""
    ocp = (ocp_version or "").strip()
    if ocp:
        return f"{img} · OCP {ocp}"
    return img


def build_cli_trigger_metadata(
    *,
    script_dir: Path,
    trigger_argv: list[str],
    product: str,
    tests: str = "",
    cluster: str = "",
    cluster_key: str = "",
    fbcf_image: str = "",
    ocp_version: str = "",
    scripts_git_url: str = "",
    scripts_git_revision: str = "",
    upstream_git_url: str = "",
    fbc_snapshot_meta: dict[str, Any] | None = None,
    local_git_repo: Path | None = None,
    trigger_type: str = TRIGGER_TYPE_MANUAL,
) -> dict[str, str]:
    """Annotations for CLI-triggered rhoai-e2e Snapshot / PipelineRun."""
    out = build_trigger_annotations(
        product=product, tests=tests, cluster=cluster, cluster_key=cluster_key
    )
    out[ANNOTATION_TRIGGER_TYPE] = (trigger_type or TRIGGER_TYPE_MANUAL).strip() or TRIGGER_TYPE_MANUAL
    cmd = format_rhoai_e2e_trigger_command(script_dir, trigger_argv)
    if cmd:
        out[ANNOTATION_TRIGGER_COMMAND] = cmd
    img = (fbcf_image or "").strip()
    if img:
        out[ANNOTATION_FBCF_IMAGE] = img
    ref = build_reference_text(fbcf_image=img, ocp_version=ocp_version)
    if ref:
        out[ANNOTATION_REFERENCE] = ref
    ui_ann, _ui_labels = build_konflux_activity_metadata(
        fbcf_image=img,
        scripts_git_url=scripts_git_url,
        scripts_git_revision=scripts_git_revision,
        upstream_git_url=upstream_git_url,
        fbc_snapshot_meta=fbc_snapshot_meta,
        local_git_repo=local_git_repo,
    )
    out.update(ui_ann)
    return out


def build_cli_trigger_labels(
    *,
    fbcf_image: str = "",
    scripts_git_url: str = "",
    scripts_git_revision: str = "",
    upstream_git_url: str = "",
    fbc_snapshot_meta: dict[str, Any] | None = None,
    local_git_repo: Path | None = None,
) -> dict[str, str]:
    """Labels for Konflux Activity Trigger / Reference columns."""
    _ann, labels = build_konflux_activity_metadata(
        fbcf_image=fbcf_image,
        scripts_git_url=scripts_git_url,
        scripts_git_revision=scripts_git_revision,
        upstream_git_url=upstream_git_url,
        fbc_snapshot_meta=fbc_snapshot_meta,
        local_git_repo=local_git_repo,
    )
    return labels


def build_manual_snapshot_trigger_labels(
    *,
    application: str,
    run_owner: str,
    product: str,
    target_type: str,
    cluster: str = "",
    fbcf_image: str = "",
    scripts_git_url: str = "",
    scripts_git_revision: str = "",
    upstream_git_url: str = "",
    fbc_snapshot_meta: dict[str, Any] | None = None,
    local_git_repo: Path | None = None,
) -> dict[str, str]:
    """Konflux + rhoai-e2e labels on CLI-created Snapshots so Integration Service binds the ITS."""
    app = (application or "").strip()
    labels = build_cli_trigger_labels(
        fbcf_image=fbcf_image,
        scripts_git_url=scripts_git_url,
        scripts_git_revision=scripts_git_revision,
        upstream_git_url=upstream_git_url,
        fbc_snapshot_meta=fbc_snapshot_meta,
        local_git_repo=local_git_repo,
    )
    # Manual ``oc create`` snapshots lack PAC metadata; Integration Service matches ``push`` context.
    labels[LABEL_TRIGGER_EVENT_TYPE] = EVENT_TYPE_PUSH
    if app:
        labels[LABEL_KONFLUX_APPLICATION] = app
    labels.update(
        build_trigger_labels(
            run_owner=run_owner,
            product=product,
            target_type=target_type,
            cluster=cluster,
        )
    )
    return labels


def build_trigger_annotations(
    *,
    product: str,
    tests: str = "",
    cluster: str = "",
    cluster_key: str = "",
) -> dict[str, str]:
    """Annotations set at Snapshot / PipelineRun trigger (CLI)."""
    out: dict[str, str] = {ANNOTATION_PRODUCT: (product or "").strip().lower()}
    if (tests or "").strip():
        out[ANNOTATION_TESTS] = tests.strip()
    for key, val in ((ANNOTATION_CLUSTER, cluster), (ANNOTATION_CLUSTER_KEY, cluster_key)):
        if (val or "").strip():
            out[key] = val.strip()
    return out


def build_konflux_test_pipelinerun_type_labels() -> dict[str, str]:
    """Konflux Activity ``Type`` column (distinct from Trigger ``event-type``)."""
    from suite.constants import LABEL_KONFLUX_PIPELINE_TYPE, PIPELINE_TYPE_TEST

    return {LABEL_KONFLUX_PIPELINE_TYPE: PIPELINE_TYPE_TEST}


def build_trigger_labels(
    *,
    run_owner: str,
    product: str,
    target_type: str,
    cluster: str = "",
) -> dict[str, str]:
    """Labels set at trigger; outcome patched when the run finishes."""
    out: dict[str, str] = {}
    owner = sanitize_k8s_label_value(run_owner)
    if owner:
        out[LABEL_RUN_OWNER] = owner
    prod = sanitize_k8s_label_value((product or "").strip().lower())
    if prod:
        out[LABEL_PRODUCT] = prod
    tgt = sanitize_k8s_label_value(target_type)
    if tgt:
        out[LABEL_TARGET] = tgt
    cl = sanitize_k8s_label_value(cluster)
    if cl:
        out[LABEL_CLUSTER] = cl
    elif target_type == "ephc":
        out[LABEL_CLUSTER] = "ephc-pending"
    return out


def _resolve_runtime_cluster(
    doc: dict[str, Any],
    runs: list[dict[str, Any]],
) -> str:
    """Best-effort cluster name from task results, prior metadata, or CLUSTER_SOURCE."""
    for task_name in (
        "stage-ephemeral-kubeconfig",
        "install-ocp-cluster",
        "provision-cluster",
        "external-cluster-ready",
    ):
        name = task_result(runs, task_name, "clusterName")
        if name:
            return name
    return (
        _read_annotation(doc, ANNOTATION_CLUSTER)
        or _read_label(doc, LABEL_CLUSTER)
        or cluster_label_from_cluster_source(pipelinerun_param_value(doc, "CLUSTER_SOURCE", ""))
    )


def build_runtime_metadata(
    *,
    pipeline_run: str,
    namespace: str,
    tests_csv: str,
    prj: dict[str, Any] | None = None,
    taskruns: list[dict[str, Any]] | None = None,
    aggregate_tasks_status: str = "",
) -> tuple[dict[str, str], dict[str, str]]:
    """Final annotations and labels after tasks complete (pipeline-run-summary / patch)."""
    doc = prj if prj is not None else get_pipelinerun_json(pipeline_run, namespace)
    runs = taskruns if taskruns is not None else list_taskruns_in_cluster(pipeline_run, namespace)

    annotations: dict[str, str] = {}
    labels: dict[str, str] = {}

    cluster = _resolve_runtime_cluster(doc, runs)
    if cluster:
        annotations[ANNOTATION_CLUSTER] = cluster
        cl = sanitize_k8s_label_value(cluster)
        if cl:
            labels[LABEL_CLUSTER] = cl

    operator_name = pipelinerun_param_value(doc, "OPERATOR_NAME", "rhods-operator")
    external_secret = external_kubeconfig_secret_name(pipelinerun_param_value(doc, "CLUSTER_SOURCE", ""))
    operator_ns = pipelinerun_param_value(doc, "OPERATOR_NAMESPACE", "redhat-ods-operator")
    op_ver = resolve_operator_version(
        runs,
        pipeline_run=pipeline_run,
        namespace=namespace,
        external_kubeconfig_secret=external_secret,
        operator_namespace=operator_ns,
        operator_name=operator_name,
        product=pipelinerun_param_value(doc, "PRODUCT", ""),
    )
    if op_ver:
        annotations[ANNOTATION_OPERATOR_VERSION] = op_ver

    installed = infer_installed_product(operator_name, op_ver)
    annotations[ANNOTATION_PRODUCT] = installed
    prod_label = sanitize_k8s_label_value(installed)
    if prod_label:
        labels[LABEL_PRODUCT] = prod_label

    browser_base = pipelinerun_param_value(doc, "ARTIFACT_BROWSER_URL", "")
    repo_path = pipelinerun_param_value(doc, "ARTIFACT_BROWSER_REPO_PATH", "odh-ci-artifacts")
    artifacts_url = resolve_artifacts_url_for_ui(
        tests_csv=tests_csv,
        pipeline_run=pipeline_run,
        taskruns=runs,
        browser_base=browser_base or DEFAULT_ARTIFACT_BROWSER_URL,
        repo_path=repo_path or DEFAULT_ARTIFACT_BROWSER_REPO_PATH,
    )
    if artifacts_url:
        annotations[ANNOTATION_TEST_RESULTS_URL] = artifacts_url

    tests = (
        _read_annotation(doc, ANNOTATION_TESTS)
        or pipelinerun_param_value(doc, "TEST_GATES", "")
        or pipelinerun_param_value(doc, "TESTS", "")
        or (tests_csv or "").strip()
    )
    if tests:
        annotations[ANNOTATION_TESTS] = tests

    owner = _read_annotation(doc, ANNOTATION_RUN_OWNER) or _read_label(doc, LABEL_RUN_OWNER)
    if owner:
        annotations[ANNOTATION_RUN_OWNER] = owner
        owner_label = sanitize_k8s_label_value(owner)
        if owner_label:
            labels[LABEL_RUN_OWNER] = owner_label

    target = resolve_target_type(doc)
    tgt_label = sanitize_k8s_label_value(target)
    if tgt_label:
        labels[LABEL_TARGET] = tgt_label

    labels[LABEL_OUTCOME] = outcome_label(doc, aggregate_tasks_status=aggregate_tasks_status)

    return annotations, labels
