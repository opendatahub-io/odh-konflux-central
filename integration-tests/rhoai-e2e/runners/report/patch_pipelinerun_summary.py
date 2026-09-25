#!/usr/bin/env python3
"""Patch rhoai-e2e summary onto the PipelineRun and print it for Konflux UI.

Writes PipelineRun annotations, Tekton task results (Results panel), and a log block
visible under publish-results → patch-summary-annotations in the Konflux UI.

Env:
    PIPELINE_RUN_NAME  -- Tekton PipelineRun (default: /etc/tekton/pipelineRunName)
    TEST_GATES         -- comma-separated test gates (optional; read from PipelineRun params)
    REPORT_PORTAL_URL, JIRA_URL -- optional URLs from publish-results (included in summary)
    ARTIFACTS_URL_PATH, TEST_OUTPUT_PATH, CLUSTER_PATH
        -- optional Tekton result file paths (set by the pipeline step)
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

from _bootstrap import ensure_rhoai_e2e_path

ensure_rhoai_e2e_path()

from runners.report.pipelinerun_metadata import build_runtime_metadata, merge_patch_pipelinerun_labels  # noqa: E402
from runners.report.pipelinerun_summary import (  # noqa: E402
    format_summary_log_block,
    get_pipelinerun_json,
    merge_patch_pipelinerun_annotations,
    namespace_from_env,
    pipeline_run_name_from_env,
    pipelinerun_param_value,
    write_summary_tekton_results,
)


def _has_writable_tekton_result_paths() -> bool:
    """True when this step has local Tekton result file paths (not unresolved $(tasks.*) refs)."""
    for key in (
        "TEST_OUTPUT_PATH",
        "ARTIFACTS_URL_PATH",
        "CLUSTER_PATH",
        "OPERATOR_VERSION_PATH",
        "FBCF_IMAGE_PATH",
    ):
        path = os.environ.get(key, "").strip()
        if not path or "$(" in path:
            continue
        if path.startswith("/tekton/"):
            return True
    return False


def main() -> int:
    pr_name = pipeline_run_name_from_env()
    if not pr_name:
        print("PIPELINE_RUN_NAME missing", file=sys.stderr)
        return 1
    ns = namespace_from_env()
    if not ns:
        print("namespace missing", file=sys.stderr)
        return 1

    prj = get_pipelinerun_json(pr_name, ns)
    tests_csv = os.environ.get("TEST_GATES", "").strip() or pipelinerun_param_value(
        prj, "TEST_GATES", pipelinerun_param_value(prj, "TESTS", "")
    )
    aggregate_status = (
        os.environ.get("PIPELINE_TASK_STATUS", "").strip()
        or os.environ.get("AGGREGATE_TASKS_STATUS", "").strip()
    )
    ann, labels = build_runtime_metadata(
        pipeline_run=pr_name,
        namespace=ns,
        tests_csv=tests_csv,
        prj=prj,
        aggregate_tasks_status=aggregate_status,
    )

    ann_patched = True
    if ann:
        if not merge_patch_pipelinerun_annotations(pr_name, ns, ann):
            ann_patched = False
            print(
                "WARN: could not patch rhoai-e2e summary annotations on PipelineRun "
                "(continuing with log output only)",
                file=sys.stderr,
            )
        else:
            prj = get_pipelinerun_json(pr_name, ns) or prj
    else:
        print("No rhoai-e2e summary annotations collected from TaskRuns")

    if labels:
        if merge_patch_pipelinerun_labels(pr_name, ns, labels):
            print(f"Patched {len(labels)} rhoai-e2e label(s) on PipelineRun/{pr_name}")
        else:
            print("WARN: could not patch rhoai-e2e labels on PipelineRun", file=sys.stderr)

    if _has_writable_tekton_result_paths():
        write_summary_tekton_results(ann)
    else:
        print("Skipping Tekton result rewrite (no local result paths; publish-results owns UI results)")
    print(
        format_summary_log_block(
            pipeline_run=pr_name,
            prj=prj,
            summary_annotations=ann,
            report_portal_url=os.environ.get("REPORT_PORTAL_URL", "").strip(),
            jira_url=os.environ.get("JIRA_URL", "").strip(),
        )
    )
    if ann and ann_patched:
        print(f"\nPatched {len(ann)} rhoai-e2e summary annotation(s) on PipelineRun/{pr_name}")
    elif ann:
        print(
            f"\nWARN: rhoai-e2e summary annotations were not patched on PipelineRun/{pr_name}",
            file=sys.stderr,
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
