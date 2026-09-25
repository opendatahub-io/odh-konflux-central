#!/usr/bin/env python3
"""Write skip TEST_OUTPUT when RUN_SMOKE=false so Konflux still creates a TaskRun."""

from __future__ import annotations

import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

from _bootstrap import ensure_rhoai_e2e_path

ensure_rhoai_e2e_path()

from steps.tekton_util import require_env, write_result


def _skip_reason(component_id: str, plan_path: Path) -> str:
    if not plan_path.is_file():
        return "not selected for this run"
    try:
        plan = json.loads(plan_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return "not selected for this run"
    if not isinstance(plan, dict):
        return "not selected for this run"
    for item in plan.get("components") or []:
        if not isinstance(item, dict):
            continue
        if str(item.get("id", "")).strip() != component_id:
            continue
        reason = str(item.get("version_skip_reason", "")).strip()
        if reason:
            op_ver = str(plan.get("operator_version", "")).strip()
            if op_ver:
                return f"{reason} (RHOAI {op_ver})"
            return reason
        return "not selected in COMPONENTS for this run"
    return "not selected for this run"


def main() -> int:
    component_id = require_env("COMPONENT_ID")
    plan_raw = os.environ.get("COMPONENT_TEST_PLAN_JSON", "").strip()
    plan_path = Path(plan_raw) if plan_raw else Path()
    reason = _skip_reason(component_id, plan_path)
    note = f"Skipped: {reason}"
    payload = {
        "result": "SUCCESS",
        "timestamp": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "failures": 0,
        "warnings": 0,
        "successes": 0,
        "skipped": 1,
        "note": note,
    }
    write_result(require_env("TEST_OUTPUT_PATH"), json.dumps(payload, separators=(",", ":")))
    print(note, flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
