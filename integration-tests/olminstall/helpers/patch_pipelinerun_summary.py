#!/usr/bin/env python3
"""Patch olminstall summary onto the PipelineRun and print it for Konflux UI.

Writes PipelineRun annotations, Tekton task results (Results panel), and a log block
visible under post-results → patch-summary-annotations in the Konflux UI.

Env:
    PIPELINE_RUN_NAME  -- Tekton PipelineRun (default: /etc/tekton/pipelineRunName)
    TESTS              -- comma-separated test phases (optional; read from PipelineRun params)
    ARTIFACTS_URL_PATH, TEST_OUTPUT_PATH, OPERATOR_VERSION_PATH, EPHEMERAL_CLUSTER_PATH
        -- optional Tekton result file paths (set by the pipeline step)
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

_OLMINSTALL = Path(__file__).resolve().parent.parent
if str(_OLMINSTALL) not in sys.path:
    sys.path.insert(0, str(_OLMINSTALL))

from helpers.pipelinerun_summary import (  # noqa: E402
    collect_summary_annotations,
    format_summary_log_block,
    get_pipelinerun_json,
    merge_patch_pipelinerun_annotations,
    namespace_from_env,
    pipeline_run_name_from_env,
    pipelinerun_param_value,
    write_summary_tekton_results,
)


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
    tests_csv = os.environ.get("TESTS", "").strip() or pipelinerun_param_value(prj, "TESTS", "")
    ann = collect_summary_annotations(
        pipeline_run=pr_name,
        namespace=ns,
        tests_csv=tests_csv,
        prj=prj,
    )

    predicted_url = os.environ.get("ARTIFACTS_URL", "").strip()
    if predicted_url and not ann.get("olminstall.test-results-url"):
        ann["olminstall.test-results-url"] = predicted_url

    if ann:
        if not merge_patch_pipelinerun_annotations(pr_name, ns, ann):
            return 1
        prj = get_pipelinerun_json(pr_name, ns) or prj
    else:
        print("No olminstall summary annotations collected from TaskRuns")

    write_summary_tekton_results(ann)
    print(format_summary_log_block(pipeline_run=pr_name, prj=prj, summary_annotations=ann))
    if ann:
        print(f"\nPatched {len(ann)} olminstall summary annotation(s) on PipelineRun/{pr_name}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
