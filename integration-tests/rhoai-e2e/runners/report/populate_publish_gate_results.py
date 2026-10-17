#!/usr/bin/env python3
"""Write publish-results BVT_GATE / SMOKE_GATE / TESTS_SUMMARY from workspace sidecars."""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

from _bootstrap import ensure_rhoai_e2e_path

ensure_rhoai_e2e_path()

from runners.report.junit_suite_report import (
    augment_publish_gate_note,
    build_publish_results_gate_summaries,
    is_gate_summary_placeholder,
    read_gate_sidecar,
)
from steps.tekton_util import (
    PUBLISH_GATE_SUMMARY_PATH_ENVS,
    read_tekton_results_at_paths,
    tekton_result_paths_from_env,
    write_tekton_results_at_paths,
)


def _read_optional(path: str) -> str:
    raw = (path or "").strip()
    if not raw or "$(" in raw:
        return ""
    try:
        return Path(raw).read_text(encoding="utf-8").strip()
    except OSError:
        return ""


def main() -> int:
    combined_raw = _read_optional(os.environ.get("TEST_OUTPUT_PATH", ""))
    bvt_raw = read_gate_sidecar(os.environ.get("BVT_TEST_OUTPUT_PATH", ""))
    smoke_raw = read_gate_sidecar(os.environ.get("SMOKE_TEST_OUTPUT_PATH", ""))
    combined_obj = None
    if combined_raw.lstrip().startswith("{"):
        try:
            parsed = json.loads(combined_raw)
            if isinstance(parsed, dict):
                combined_obj = parsed
        except json.JSONDecodeError:
            combined_obj = None

    summaries = build_publish_results_gate_summaries(
        combined_raw=combined_raw,
        combined_obj=combined_obj,
        bvt_raw=bvt_raw,
        smoke_raw=smoke_raw,
        test_gates=os.environ.get("TEST_GATES", "").strip(),
    )
    paths = tekton_result_paths_from_env(PUBLISH_GATE_SUMMARY_PATH_ENVS)
    existing = read_tekton_results_at_paths(paths) if paths else {}
    to_write: dict[str, str] = {}
    for name, path in paths.items():
        new_val = summaries.get(name, "").strip()
        if not new_val or is_gate_summary_placeholder(new_val):
            continue
        cur = existing.get(name, "").strip()
        if is_gate_summary_placeholder(cur) or cur != new_val:
            to_write[name] = new_val
    write_tekton_results_at_paths(to_write, paths)

    print(
        f"Gate summaries: {', '.join(sorted(to_write)) or 'none'}",
        flush=True,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
