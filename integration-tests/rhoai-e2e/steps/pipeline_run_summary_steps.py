#!/usr/bin/env python3
"""Dispatch pipeline-run-summary sub-steps via RHOAI_E2E_SUMMARY_STEP."""

from __future__ import annotations

import os
import sys

from _bootstrap import ensure_rhoai_e2e_path

ensure_rhoai_e2e_path()

from runners.report.collect_post_results_ui import main as collect_main  # noqa: E402
from runners.report.gather_publish_context import main as gather_main  # noqa: E402
from runners.report.patch_pipelinerun_summary import main as patch_main  # noqa: E402

STEPS = {
    "gather": gather_main,
    "collect-ui": collect_main,
    "patch": patch_main,
}


def main() -> int:
    step = os.environ.get("RHOAI_E2E_SUMMARY_STEP", "").strip()
    if not step:
        print(
            "ERROR: RHOAI_E2E_SUMMARY_STEP is required (gather, collect-ui, patch)",
            file=sys.stderr,
        )
        return 1
    fn = STEPS.get(step)
    if fn is None:
        print(
            f"ERROR: unknown RHOAI_E2E_SUMMARY_STEP {step!r}; "
            f"expected one of: {', '.join(sorted(STEPS))}",
            file=sys.stderr,
        )
        return 1
    return fn()


if __name__ == "__main__":
    raise SystemExit(main())
