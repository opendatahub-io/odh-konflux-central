"""Resolve BVT artifact browser URLs from PipelineRun / TaskRun state."""

from __future__ import annotations

from typing import Any

from .tekton_incluster import (
    list_taskruns_in_cluster,
    namespace_from_env,
    result_map,
    task_name,
    task_reason,
)


def tests_include_bvt(tests_csv: str) -> bool:
    return "bvt" in {p.strip().lower() for p in (tests_csv or "").split(",") if p.strip()}


def published_artifacts_url_from_taskruns(taskruns: list[dict[str, Any]]) -> str:
    for tr in taskruns:
        task = task_name(tr).lower()
        if "bvt-health-checks" not in task:
            continue
        url = result_map(tr).get("ARTIFACTS_URL", "").strip()
        if url:
            return url
    return ""


def bvt_unpublished_reason(taskruns: list[dict[str, Any]]) -> str:
    """Short explanation when BVT was requested but no ARTIFACTS_URL was published."""
    bvt_tasks = [task_name(tr) for tr in taskruns if "bvt-health-checks" in task_name(tr).lower()]
    if not bvt_tasks:
        return "BVT did not run (pipeline failed or was skipped before bvt-health-checks)"
    for tr in taskruns:
        task = task_name(tr)
        if "bvt-health-checks" not in task.lower():
            continue
        reason = task_reason(tr)
        if reason == "Completed":
            if "upload-artifacts" in task.lower() or task.endswith("upload-artifacts"):
                return "upload-artifacts finished but did not publish ARTIFACTS_URL"
            return "BVT finished without publishing artifacts (upload-artifacts may have been skipped)"
        if reason in ("Failed", "PipelineRunFailed", "TaskRunFailed"):
            return f"{task} failed — see TaskRun logs"
        if reason in ("Cancelled", "TaskRunCancelled", "PipelineRunCancelled"):
            return f"{task} was cancelled"
        if reason in ("Skipped", "TaskRunSkipped", "PipelineRunSkipped"):
            return f"{task} was skipped"
    return "BVT did not publish JUnit to the artifact browser"


def resolve_artifacts_notification_line(
    *,
    tests_csv: str,
    pipeline_run: str,
    taskruns: list[dict[str, Any]] | None = None,
) -> str | None:
    """Return a single notification line, or None to omit artifacts entirely."""
    if not tests_include_bvt(tests_csv):
        return None
    runs = taskruns if taskruns is not None else []
    if not runs and pipeline_run:
        ns = namespace_from_env()
        if ns:
            runs = list_taskruns_in_cluster(pipeline_run, ns)
    published = published_artifacts_url_from_taskruns(runs)
    if published:
        return f"Artifacts: {published}"
    return f"Artifacts: (none — {bvt_unpublished_reason(runs)})"
