#!/usr/bin/env python3
"""Re-write RUN_SMOKE_<id> Tekton results after export_component_plan version gates.

Runs in **opendatahub-tests-prepare** after ``export_component_plan`` (same task declares
``RUN_SMOKE_<id>`` results; ``TASK_MESSAGE`` is capped to preserve Tekton result budget).
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

from _bootstrap import ensure_rhoai_e2e_path

ensure_rhoai_e2e_path()

from suite.component_catalog import default_components_smoke_config_path, load_components_smoke_catalog
from suite.component_smoke_flag_refresh import (
    catalog_ids_with_run_smoke_result,
    compute_version_aware_run_smoke_flags,
    format_version_skipped_summary,
    version_skipped_entries,
    write_run_smoke_tekton_results,
    write_version_skipped_manifest,
)
from suite.component_smoke_results import component_smoke_result_name


def _truthy(raw: str) -> bool:
    return (raw or "").strip().lower() in ("1", "true", "yes")


def _load_plan(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def _collect_result_paths(catalog_ids: tuple[str, ...]) -> dict[str, str]:
    out: dict[str, str] = {}
    for cid in catalog_ids:
        key = component_smoke_result_name(cid)
        path_var = f"{key}_PATH"
        raw = os.environ.get(path_var, "").strip()
        if raw:
            out[key] = raw
    return out


def main() -> int:
    run_component_tests = _truthy(os.environ.get("RUN_COMPONENT_TESTS", ""))
    plan_path = os.environ.get("COMPONENT_TEST_PLAN_JSON", "").strip()
    version_skipped_path = os.environ.get("VERSION_SKIPPED_JSON", "").strip()
    run_smoke_flags_path = os.environ.get("RUN_SMOKE_FLAGS_JSON", "").strip()
    results_base = Path(os.environ.get("RESULTS_DIR", "/tekton/results")).resolve()

    catalog = load_components_smoke_catalog(
        Path(os.environ.get("COMPONENTS_CONFIG", "")).resolve()
        if os.environ.get("COMPONENTS_CONFIG", "").strip()
        else default_components_smoke_config_path()
    )
    catalog_ids = catalog_ids_with_run_smoke_result(catalog.component_ids)
    result_paths = _collect_result_paths(catalog_ids)
    if not result_paths:
        print("ERROR: no RUN_SMOKE_<id>_PATH env vars for refresh", file=sys.stderr)
        return 1

    if not run_component_tests:
        flags = {cid: False for cid in catalog_ids}
        if write_run_smoke_tekton_results(
            flags, result_paths=result_paths, results_base=results_base
        ):
            return 1
        if version_skipped_path:
            Path(version_skipped_path).parent.mkdir(parents=True, exist_ok=True)
            Path(version_skipped_path).write_text(
                json.dumps({"operator_version": "", "components": [], "summary": ""}, indent=2)
                + "\n",
                encoding="utf-8",
            )
        if run_smoke_flags_path:
            Path(run_smoke_flags_path).parent.mkdir(parents=True, exist_ok=True)
            Path(run_smoke_flags_path).write_text(
                json.dumps(flags, indent=2, sort_keys=True) + "\n",
                encoding="utf-8",
            )
        print("RUN_SMOKE_* refreshed (component tests disabled — all false)", flush=True)
        return 0

    if not plan_path:
        print("COMPONENT_TEST_PLAN_JSON is required when RUN_COMPONENT_TESTS=true", file=sys.stderr)
        return 1
    plan_file = Path(plan_path)
    if not plan_file.is_file():
        print(
            f"ERROR: component test plan missing at {plan_path}; "
            "opendatahub-tests-prepare must export it first",
            file=sys.stderr,
        )
        return 1
    plan = _load_plan(plan_file)
    flags = compute_version_aware_run_smoke_flags(
        plan,
        run_component_tests=True,
        catalog_component_ids=catalog_ids,
    )
    if write_run_smoke_tekton_results(
        flags, result_paths=result_paths, results_base=results_base
    ):
        return 1

    entries = version_skipped_entries(plan)
    if version_skipped_path:
        write_version_skipped_manifest(Path(version_skipped_path), plan)
    if run_smoke_flags_path:
        Path(run_smoke_flags_path).parent.mkdir(parents=True, exist_ok=True)
        Path(run_smoke_flags_path).write_text(
            json.dumps(flags, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )

    skipped = [cid for cid, val in flags.items() if not val]
    runnable = [cid for cid, val in flags.items() if val]
    summary = format_version_skipped_summary(
        entries, operator_version=str(plan.get("operator_version", ""))
    )
    if summary:
        print(summary, flush=True)
    print(
        f"RUN_SMOKE_* refreshed: {len(runnable)} runnable, {len(skipped)} grey-skip",
        flush=True,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
