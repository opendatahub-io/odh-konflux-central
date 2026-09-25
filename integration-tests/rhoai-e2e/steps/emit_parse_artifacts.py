#!/usr/bin/env python3
"""Tekton step: publish parse run-config artifacts and trim task results to Tekton budget."""

from __future__ import annotations

import os
import sys
from pathlib import Path

from _bootstrap import ensure_rhoai_e2e_path

ensure_rhoai_e2e_path()

from steps.tekton_util import (
    _TEKTON_STEP_TERMINATION_BUDGET_BYTES,
    _TEKTON_TASK_RESULTS_BUDGET_BYTES,
    fit_tekton_task_results,
    read_tekton_task_result_files,
    sync_tekton_task_result_files,
    tekton_results_termination_payload_size,
    tekton_step_termination_payload_size,
    write_result,
)

_PARSE_RESULT_PRIORITY: tuple[str, ...] = (
    "COMPONENTS_CSV",
    "RUN_INSTALL_DEP_OPERATORS",
    "RUN_OPENDATAHUB_TESTS",
    "RUN_COMPONENT_TESTS",
    "RUN_SMOKE",
    "RUN_BVT",
    "RUN_MINIMAL_DEPS",
    "SETUP_DEPENDENCIES_ARGS",
    "SMOKE_AWS_SECRET",
    "RUN_DISTRIBUTED_WORKLOADS_TESTS",
    "RUN_COMPONENT_CLUSTER_PREP_IN_DEP_OPERATORS",
    "RUN_BVT_PLACEHOLDER_ONLY",
    "RUN_TIER1",
    "CLUSTER",
    "RUN",
    "FBC",
    "TRIGGER",
    "KONFLUX_EVENT",
    "SNAPSHOT",
    "TRIGGER_CMD",
)

_ARTIFACTS: tuple[tuple[str, str], ...] = (
    ("COMPONENTS_CSV_PATH", "COMPONENTS_CSV"),
    ("SETUP_DEPENDENCIES_ARGS_PATH", "SETUP_DEPENDENCIES_ARGS"),
    ("SMOKE_AWS_SECRET_PATH", "SMOKE_AWS_SECRET"),
)


def main() -> int:
    run_config_raw = os.environ.get("RUN_CONFIG_DIR", "").strip()
    if not run_config_raw:
        print("RUN_CONFIG_DIR is required", file=sys.stderr)
        return 1
    config_dir = Path(run_config_raw)
    if not config_dir.is_dir():
        print(f"RUN_CONFIG_DIR is not a directory: {config_dir}", file=sys.stderr)
        return 1

    for result_env, filename in _ARTIFACTS:
        dst = os.environ.get(result_env, "").strip()
        if not dst:
            print(f"missing Tekton result path env {result_env}", file=sys.stderr)
            return 1
        src = config_dir / filename
        if not src.is_file():
            print(f"missing run-config file {src}", file=sys.stderr)
            return 1
        write_result(dst, src.read_text(encoding="utf-8"))

    results = read_tekton_task_result_files()
    # TRIGGER_CMD is UI-only; drop before fit so COMPONENTS_CSV fits the step budget.
    results.pop("TRIGGER_CMD", None)
    # Konflux enforces ~2048 B per-step termination JSON; leave headroom for StartedAt metadata.
    step_budget = min(_TEKTON_STEP_TERMINATION_BUDGET_BYTES - 128, _TEKTON_TASK_RESULTS_BUDGET_BYTES)
    fitted = fit_tekton_task_results(results, priority=_PARSE_RESULT_PRIORITY, budget=step_budget)
    if not (fitted.get("COMPONENTS_CSV") or "").strip():
        print("ERROR: COMPONENTS_CSV missing after Tekton result fit", file=sys.stderr)
        return 1

    sync_tekton_task_result_files(fitted)
    step_size = tekton_step_termination_payload_size(fitted)
    task_size = tekton_results_termination_payload_size(fitted)
    dropped = sorted(set(results) - set(fitted))
    if dropped:
        print(
            f"parse Tekton results trimmed to step={step_size}B task={task_size}B "
            f"(step budget {step_budget}B); dropped {dropped}",
            flush=True,
        )
    else:
        print(f"parse Tekton results fit (step={step_size}B task={task_size}B)", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
