"""Component selection for cluster prerequisite orchestration."""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path


def _load_component_test_plan() -> dict[str, object] | None:
    plan_path = os.environ.get("COMPONENT_TEST_PLAN_JSON", "").strip()
    if not plan_path:
        return None
    try:
        plan = json.loads(Path(plan_path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        print(f"WARN: could not read smoke plan {plan_path!r}: {exc}", file=sys.stderr)
        return None
    return plan if isinstance(plan, dict) else None


def selected_component_ids() -> set[str]:
    plan = _load_component_test_plan()
    if plan is not None:
        items = plan.get("components")
        if isinstance(items, list):
            return {
                str(item.get("id", "")).strip()
                for item in items
                if isinstance(item, dict)
                and str(item.get("id", "")).strip()
                and not str(item.get("version_skip_reason", "")).strip()
            }
    csv = os.environ.get("COMPONENTS_CSV", "").strip()
    return {c.strip() for c in csv.split(",") if c.strip()}


_selected_component_ids = selected_component_ids


def components_csv_from_ids(ids: set[str]) -> str:
    return ",".join(sorted(ids))
