"""Resolve test artifact browser URLs from PipelineRun / TaskRun state."""

from __future__ import annotations

from typing import Any

from steps.tekton_incluster import (
    list_taskruns_in_cluster,
    namespace_from_env,
    result_map,
    task_name,
    task_reason,
    task_succeeded_detail,
)

# Task name substrings for pytest + OCI upload tasks.
BVT_TASK_SUBSTR = "bvt-health-checks"
SMOKE_TASK_SUBSTR = "test-finalize"
PUBLISH_RESULTS_TASK_SUBSTR = "publish-results"


def artifacts_browser_run_url(
    pipeline_run: str,
    *,
    browser_base: str = "https://app-artifact-browser.apps.rosa.konflux-qe.zmr9.p3.openshiftapps.com",
    repo_path: str = "odh-ci-artifacts",
) -> str:
    """Single artifact browser URL for a PipelineRun (gates live in subfolders)."""
    pr = pipeline_run.strip()
    if not pr:
        return ""
    return f"{browser_base.rstrip('/')}/{repo_path.strip('/')}/{pr}/"


def artifacts_browser_gate_url(
    pipeline_run: str,
    *,
    gate: str,
    browser_base: str = "https://app-artifact-browser.apps.rosa.konflux-qe.zmr9.p3.openshiftapps.com",
    repo_path: str = "odh-ci-artifacts",
) -> str:
    """Deep link to one test gate under the run artifact root (e.g. ``…/run/bvt/``)."""
    base = artifacts_browser_run_url(
        pipeline_run, browser_base=browser_base, repo_path=repo_path
    )
    gate_id = gate.strip().strip("/")
    if not base or not gate_id:
        return base
    return f"{base}{gate_id}/"


def tests_include_bvt(tests_csv: str) -> bool:
    return "bvt" in {p.strip().lower() for p in (tests_csv or "").split(",") if p.strip()}


def tests_include_smoke(tests_csv: str) -> bool:
    return "smoke" in {p.strip().lower() for p in (tests_csv or "").split(",") if p.strip()}


def published_artifacts_url_from_taskruns(
    taskruns: list[dict[str, Any]],
    *,
    task_substr: str,
) -> str:
    needle = task_substr.lower()
    for tr in taskruns:
        if needle not in task_name(tr).lower():
            continue
        url = result_map(tr).get("ARTIFACTS_URL", "").strip()
        if url:
            return url
    return ""


def unpublished_reason(taskruns: list[dict[str, Any]], *, task_substr: str, label: str) -> str:
    tasks = [task_name(tr) for tr in taskruns if task_substr.lower() in task_name(tr).lower()]
    if not tasks:
        return f"{label} did not run (pipeline failed or was skipped before {task_substr})"
    for tr in taskruns:
        task = task_name(tr)
        if task_substr.lower() not in task.lower():
            continue
        reason = task_reason(tr)
        if reason in ("Succeeded", "Completed"):
            if not result_map(tr).get("ARTIFACTS_URL", "").strip():
                return f"{label} finished without publishing artifacts (OCI upload failed or was skipped)"
            continue
        if reason in ("Failed", "PipelineRunFailed", "TaskRunFailed", "TaskRunImagePullFailed"):
            _status, _cond_reason, message = task_succeeded_detail(tr)
            compact = " ".join((message or "").split())
            if compact:
                if len(compact) > 200:
                    compact = compact[:199] + "…"
                return f"{task} failed — {compact}"
            return f"{task} failed — see TaskRun logs"
        if reason in ("Cancelled", "TaskRunCancelled", "PipelineRunCancelled"):
            return f"{task} was cancelled"
        if reason in ("Skipped", "TaskRunSkipped", "PipelineRunSkipped"):
            return f"{task} was skipped"
    return f"{label} did not publish JUnit to the artifact browser"


def artifacts_browser_run_url_for_pipeline_run(
    pipeline_run: str,
    *,
    browser_base: str = "https://app-artifact-browser.apps.rosa.konflux-qe.zmr9.p3.openshiftapps.com",
    repo_path: str = "odh-ci-artifacts",
) -> str:
    """Artifact browser root URL for a PipelineRun (gate folders live underneath)."""
    return artifacts_browser_run_url(
        pipeline_run, browser_base=browser_base, repo_path=repo_path
    )


def resolve_artifacts_url_for_ui(
    *,
    tests_csv: str,
    pipeline_run: str,
    taskruns: list[dict[str, Any]],
    browser_base: str = "https://app-artifact-browser.apps.rosa.konflux-qe.zmr9.p3.openshiftapps.com",
    repo_path: str = "odh-ci-artifacts",
) -> str:
    """Return artifact browser URL when a publish/finalize TaskRun recorded ARTIFACTS_URL."""
    for task_substr in (PUBLISH_RESULTS_TASK_SUBSTR, SMOKE_TASK_SUBSTR, BVT_TASK_SUBSTR):
        url = published_artifacts_url_from_taskruns(taskruns, task_substr=task_substr)
        if url:
            return url
    if not (tests_include_bvt(tests_csv) or tests_include_smoke(tests_csv)):
        return ""
    return artifacts_browser_run_url_for_pipeline_run(
        pipeline_run, browser_base=browser_base, repo_path=repo_path
    )


def resolve_artifacts_notification_lines(
    *,
    tests_csv: str,
    pipeline_run: str,
    taskruns: list[dict[str, Any]] | None = None,
) -> list[str]:
    """Return notification lines for published test artifacts (BVT and/or smoke)."""
    lines: list[str] = []
    runs = taskruns if taskruns is not None else []
    if not runs and pipeline_run:
        ns = namespace_from_env()
        if ns:
            try:
                runs = list_taskruns_in_cluster(pipeline_run, ns)
            except Exception:
                runs = []

    run_url = artifacts_browser_run_url(pipeline_run) if pipeline_run else ""
    gates: list[str] = []
    if tests_include_bvt(tests_csv):
        if published_artifacts_url_from_taskruns(runs, task_substr=BVT_TASK_SUBSTR) or run_url:
            gates.append("bvt")
        else:
            lines.append(f"BVT artifacts: (none — {unpublished_reason(runs, task_substr=BVT_TASK_SUBSTR, label='BVT')})")

    if tests_include_smoke(tests_csv):
        if (
            published_artifacts_url_from_taskruns(runs, task_substr=PUBLISH_RESULTS_TASK_SUBSTR)
            or published_artifacts_url_from_taskruns(runs, task_substr=SMOKE_TASK_SUBSTR)
            or run_url
        ):
            gates.append("smoke")
        else:
            lines.append(
                f"Smoke artifacts: (none — {unpublished_reason(runs, task_substr=SMOKE_TASK_SUBSTR, label='Smoke')})"
            )

    if gates and run_url:
        lines.insert(0, f"Artifacts: {run_url}test-payload-results/")

    return lines
