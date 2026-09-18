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
from steps.tekton_incluster import list_taskruns_in_cluster, result_map, task_name, task_reason


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

                ver = pick_succeeded_csv_version(operator_namespace, operator_name, timeout=20)
                return ver.strip() if ver else ""
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
    """Best-effort CSV version from install tasks, collect-diagnostics, or cluster probe."""
    for task in (
        "install-rhoai-external",
        "install-odh-external",
        "install-rhoai",
        "install-odh",
        "install-operator",
        "install-operator-external",
        "collect-diagnostics",
    ):
        ver = task_result(taskruns, task, "OPERATOR_VERSION")
        if ver and ver not in ("(unknown)", "(see pipeline run logs)"):
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
