"""Resolve Tekton PipelineTask execution state from in-cluster PipelineRun / TaskRun API."""

from __future__ import annotations

import json
from typing import Any

from steps.tekton_incluster import (
    fetch_pipelinerun_in_cluster,
    list_taskruns_in_cluster,
    namespace_from_env,
    pipeline_run_name_from_env,
    task_name,
)


def _taskrun_succeeded_condition(taskrun: dict[str, Any]) -> tuple[str, str]:
    """Return ``(status, reason)`` for the TaskRun Succeeded condition."""
    status = taskrun.get("status")
    if not isinstance(status, dict):
        return "", ""
    conds = status.get("conditions")
    if not isinstance(conds, list):
        return "", ""
    for cond in conds:
        if not isinstance(cond, dict):
            continue
        if str(cond.get("type", "")).strip().lower() == "succeeded":
            return (
                str(cond.get("status", "") or "").strip(),
                str(cond.get("reason", "") or "").strip(),
            )
    return "", ""


def _taskrun_condition_reason(taskrun: dict[str, Any]) -> str:
    status, reason = _taskrun_succeeded_condition(taskrun)
    return reason or status


def _taskrun_state(taskrun: dict[str, Any]) -> str:
    status, reason = _taskrun_succeeded_condition(taskrun)
    status_l = status.lower()
    if status_l == "true":
        return "succeeded"
    if status_l == "false":
        return "failed"
    if status_l == "unknown":
        return "running"
    reason_l = reason.lower()
    if reason_l in ("succeeded", "completed"):
        return "succeeded"
    if reason_l in ("failed", "error", "pipelineruntimeout", "taskrunvalidationfailed"):
        return "failed"
    if reason_l in ("running", "pending", "started"):
        return "running"
    tr_status = taskrun.get("status")
    if isinstance(tr_status, dict):
        phase = str(tr_status.get("completionTime", "")).strip()
        start = str(tr_status.get("startTime", "")).strip()
        if start and not phase:
            return "running"
    return "unknown"


def _skipped_task_detail(pr: dict[str, Any], pipeline_task: str) -> str:
    status = pr.get("status")
    if not isinstance(status, dict):
        return ""
    skipped = status.get("skippedTasks")
    if not isinstance(skipped, list):
        return ""
    for entry in skipped:
        if not isinstance(entry, dict):
            continue
        if str(entry.get("name", "")).strip() != pipeline_task:
            continue
        reason = str(entry.get("reason", "")).strip()
        when = entry.get("whenExpressions")
        when_text = ""
        if when:
            try:
                when_text = json.dumps(when, sort_keys=True)
            except TypeError:
                when_text = str(when)
        parts = [part for part in (reason, when_text) if part]
        return "; ".join(parts)
    return ""


def pipeline_task_execution_state(
    pipeline_task: str,
    *,
    pipeline_run: str = "",
    namespace: str = "",
    taskruns: list[dict[str, Any]] | None = None,
    pr_doc: dict[str, Any] | None = None,
) -> tuple[str, str]:
    """Return ``(state, detail)`` for a PipelineTask name.

    States: ``succeeded``, ``failed``, ``skipped``, ``missing``, ``running``, ``unknown``.
    """
    task = (pipeline_task or "").strip()
    if not task:
        return "missing", "empty pipeline task name"

    pr_name = (pipeline_run or pipeline_run_name_from_env()).strip()
    ns = (namespace or namespace_from_env()).strip()
    pr = pr_doc
    if pr is None and pr_name and ns:
        pr = fetch_pipelinerun_in_cluster(pr_name, ns)

    runs = taskruns
    if runs is None and pr_name and ns:
        runs = list_taskruns_in_cluster(pr_name, ns)

    matches: list[dict[str, Any]] = []
    for tr in runs or []:
        if task_name(tr) == task:
            matches.append(tr)

    if matches:
        state = _taskrun_state(matches[-1])
        reason = _taskrun_condition_reason(matches[-1])
        return state, reason or state

    if pr is not None:
        skip_detail = _skipped_task_detail(pr, task)
        if skip_detail:
            return "skipped", skip_detail

    return "missing", "no TaskRun and not listed in skippedTasks"


def require_pipeline_tasks_ran(
    required: tuple[str, ...],
    *,
    pipeline_run: str = "",
    namespace: str = "",
    allow_failed: bool = True,
) -> list[str]:
    """Return human-readable errors when required PipelineTasks did not execute."""
    pr_name = (pipeline_run or pipeline_run_name_from_env(required=True)).strip()
    ns = (namespace or namespace_from_env(required=True)).strip()
    pr = fetch_pipelinerun_in_cluster(pr_name, ns)
    taskruns = list_taskruns_in_cluster(pr_name, ns)
    errors: list[str] = []
    for task in required:
        state, detail = pipeline_task_execution_state(
            task,
            pipeline_run=pr_name,
            namespace=ns,
            taskruns=taskruns,
            pr_doc=pr,
        )
        if state == "succeeded":
            continue
        if state == "failed" and allow_failed:
            continue
        if state == "running":
            errors.append(f"{task}: still running (unexpected at gate)")
            continue
        if state == "skipped":
            errors.append(f"{task}: skipped ({detail or 'when false'})")
            continue
        if state == "missing":
            errors.append(f"{task}: never scheduled ({detail or 'missing TaskRun'})")
            continue
        errors.append(f"{task}: {state} ({detail})")
    return errors
