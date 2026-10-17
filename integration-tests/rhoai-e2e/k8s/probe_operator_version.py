"""Resolve installed operator CSV version when install tasks did not run."""

from __future__ import annotations

import os
import tempfile
import time
from pathlib import Path
from typing import Any

from k8s.probe_fbcf_image import _kubeconfig_env
from steps.prepare_diagnostics_kubeconfig import _fetch_external_kubeconfig, _namespace
from runners.report.pipelinerun_summary import task_result
from steps.tekton_incluster import list_taskruns_in_cluster, namespace_from_env, pipeline_run_name_from_env, result_map, task_name, task_reason
from steps.tekton_util import write_result

_UNKNOWN_VERSIONS = frozenset({"(unknown)", "(see pipeline run logs)", "n/a"})


def probe_installed_operator_version(
    operator_namespace: str,
    operator_name: str,
    *,
    timeout: int = 20,
) -> str:
    """Read the Succeeded CSV version from the cluster using the current KUBECONFIG."""
    if not (operator_namespace or "").strip():
        return ""
    try:
        from install.install_and_verify import pick_succeeded_csv_version

        ver = pick_succeeded_csv_version(
            operator_namespace.strip(),
            (operator_name or "rhods-operator").strip(),
            timeout=timeout,
        )
        return ver.strip() if ver else ""
    except Exception:
        return ""


def resolve_display_operator_version(
    *,
    installed_version: str = "",
    rhoai_version_param: str = "",
    fbcf_image: str = "",
) -> tuple[str, str]:
    """Return ``(operator_version, rhoai_version)`` for Tekton results and UI."""
    from suite.pipelinerun_naming import version_placeholder
    from suite.snapshot_catalog_line import catalog_version_for_install

    installed = (installed_version or "").strip()
    catalog = catalog_version_for_install(
        rhoai_version_param=rhoai_version_param,
        fbcf_image=fbcf_image,
    )
    operator_ver = installed or catalog
    rhoai_ver = installed or catalog
    param = (rhoai_version_param or "").strip()
    if not rhoai_ver and param and not version_placeholder(param):
        rhoai_ver = param
    return operator_ver, rhoai_ver


def publish_operator_version_results(
    *,
    installed_version: str = "",
    rhoai_version_param: str = "",
    fbcf_image: str = "",
    patch_pipelinerun: bool = True,
) -> str:
    """Write OPERATOR_VERSION / RHOAI_VERSION Tekton results and optional PLR annotation."""
    from suite.constants import ANNOTATION_OPERATOR_VERSION

    operator_ver, rhoai_ver = resolve_display_operator_version(
        installed_version=installed_version,
        rhoai_version_param=rhoai_version_param,
        fbcf_image=fbcf_image,
    )
    for env_key, value in (
        ("OPERATOR_VERSION_PATH", operator_ver),
        ("RHOAI_VERSION_PATH", rhoai_ver),
    ):
        path = os.environ.get(env_key, "").strip()
        if path and value:
            write_result(path, value)

    resolved = operator_ver or rhoai_ver
    if patch_pipelinerun and resolved and resolved not in _UNKNOWN_VERSIONS:
        pr_name = pipeline_run_name_from_env()
        ns = namespace_from_env()
        if pr_name and ns:
            from runners.report.pipelinerun_summary import merge_patch_pipelinerun_annotations

            merge_patch_pipelinerun_annotations(
                pr_name,
                ns,
                {ANNOTATION_OPERATOR_VERSION: resolved},
            )
    return resolved


def _poll_collect_diagnostics_version(
    pipeline_run: str,
    namespace: str,
    *,
    timeout_s: float = 90.0,
    interval_s: float = 5.0,
) -> str:
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        taskruns = list_taskruns_in_cluster(pipeline_run, namespace)
        ver = task_result(taskruns, "collect-diagnostics", "OPERATOR_VERSION")
        if ver and ver not in ("(unknown)", "(see pipeline run logs)"):
            return ver.strip()
        finished = False
        for tr in taskruns:
            if "collect-diagnostics" not in task_name(tr).lower():
                continue
            reason = task_reason(tr)
            if reason in ("Succeeded", "Failed", "Completed", "Skipped", "TaskRunSkipped"):
                finished = True
                ver = result_map(tr).get("OPERATOR_VERSION", "").strip()
                if ver and ver not in ("(unknown)", "(see pipeline run logs)"):
                    return ver
                break
        if finished:
            return ""
        time.sleep(interval_s)
    return ""


def _probe_from_external_secret(
    secret_name: str,
    operator_namespace: str,
    operator_name: str,
) -> str:
    ns = _namespace()
    if not (secret_name and ns and operator_namespace):
        return ""
    try:
        content = _fetch_external_kubeconfig(secret_name, ns)
    except (OSError, ValueError, RuntimeError):
        return ""
    tmp_fd, tmp_path = tempfile.mkstemp(suffix="-kubeconfig")
    os.close(tmp_fd)
    tmp = Path(tmp_path)
    try:
        tmp.write_text(content, encoding="utf-8")
        with _kubeconfig_env(tmp):
            try:
                from install.install_and_verify import pick_succeeded_csv_version

                return probe_installed_operator_version(operator_namespace, operator_name, timeout=20)
            except Exception:
                return ""
    finally:
        tmp.unlink(missing_ok=True)


def resolve_operator_version(
    taskruns: list[dict[str, Any]],
    *,
    pipeline_run: str = "",
    namespace: str = "",
    external_kubeconfig_secret: str = "",
    operator_namespace: str = "",
    operator_name: str = "rhods-operator",
    poll_collect_diagnostics: bool = True,
    product: str = "",
) -> str:
    """Best-effort CSV version from install tasks, verify-operator-ready, collect-diagnostics, or cluster probe."""
    for task in (
        "install-rhoai-external",
        "install-odh-external",
        "install-rhoai",
        "install-odh",
        "install-operator",
        "install-operator-external",
        "verify-operator-ready",
        "collect-diagnostics",
    ):
        ver = task_result(taskruns, task, "OPERATOR_VERSION")
        if ver and ver not in _UNKNOWN_VERSIONS:
            return ver.strip()
    for task in ("verify-operator-ready", "collect-diagnostics"):
        ver = task_result(taskruns, task, "RHOAI_VERSION")
        if ver and ver not in _UNKNOWN_VERSIONS:
            return ver.strip()

    if poll_collect_diagnostics and pipeline_run and namespace:
        ver = _poll_collect_diagnostics_version(pipeline_run, namespace)
        if ver:
            return ver

    if external_kubeconfig_secret.strip():
        ver = _probe_from_external_secret(
            external_kubeconfig_secret.strip(),
            operator_namespace.strip(),
            operator_name.strip() or "rhods-operator",
        )
        if ver:
            return ver
    return ""
