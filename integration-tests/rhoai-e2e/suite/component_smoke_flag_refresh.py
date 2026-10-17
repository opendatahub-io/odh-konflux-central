"""Version-aware RUN_SMOKE_<id> flags after operator version is known."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from suite.component_smoke_results import component_smoke_result_name

# RHOAI 3.5+ granular ai_safety tasks get version-aware flags on resolve only
# (not parse-pipeline-tests — Tekton result budget).
AI_SAFETY_GRANULAR_IDS = frozenset(
    {
        "ai_safety_evalhub",
        "ai_safety_guardrails",
        "ai_safety_lmeval",
        "ai_safety_trustyai_operator",
        "ai_safety_trustyai_service",
    }
)

_RESOLVE_PIPELINE_TASK = "resolve-component-run-flags"


def catalog_ids_with_run_smoke_result(catalog_component_ids: tuple[str, ...]) -> tuple[str, ...]:
    """Catalog ids that declare a dedicated RUN_SMOKE_<id> on resolve-component-run-flags."""
    return catalog_component_ids


def parse_pipeline_run_smoke_result_ids(catalog_component_ids: tuple[str, ...]) -> tuple[str, ...]:
    """Per-component RUN_SMOKE_<id> live on resolve-component-run-flags only (Tekton budget)."""
    return ()


def run_smoke_when_expression(component_id: str) -> str:
    """PipelineTask ``when`` input for a catalog component smoke task."""
    return f"$(tasks.{_RESOLVE_PIPELINE_TASK}.results.RUN_SMOKE_{component_id})"


def compute_version_aware_run_smoke_flags(
    plan: dict[str, Any],
    *,
    run_component_tests: bool,
    catalog_component_ids: tuple[str, ...],
) -> dict[str, bool]:
    """Return RUN_SMOKE_<id> after applying version_skip_reason from the component plan."""
    if not run_component_tests:
        return {cid: False for cid in catalog_ids_with_run_smoke_result(catalog_component_ids)}

    by_id: dict[str, dict[str, Any]] = {}
    for item in plan.get("components") or []:
        if isinstance(item, dict) and item.get("id"):
            by_id[str(item["id"])] = item

    flags: dict[str, bool] = {}
    for cid in catalog_ids_with_run_smoke_result(catalog_component_ids):
        if cid not in by_id:
            flags[cid] = False
            continue
        skip = str(by_id[cid].get("version_skip_reason") or "").strip()
        flags[cid] = not skip
    return flags


def version_skipped_entries(plan: dict[str, Any]) -> list[dict[str, str]]:
    """Components in the plan with version_skip_reason (for run-config + summaries)."""
    out: list[dict[str, str]] = []
    operator_version = str(plan.get("operator_version", "")).strip()
    for item in plan.get("components") or []:
        if not isinstance(item, dict):
            continue
        cid = str(item.get("id", "")).strip()
        reason = str(item.get("version_skip_reason") or "").strip()
        if cid and reason:
            out.append(
                {
                    "id": cid,
                    "reason": reason,
                    "operator_version": operator_version,
                }
            )
    return out


def format_version_skipped_summary(
    entries: list[dict[str, str]],
    *,
    operator_version: str = "",
) -> str:
    if not entries:
        return ""
    ver = operator_version.strip() or entries[0].get("operator_version", "").strip()
    head = f"version-skipped {len(entries)} component(s)"
    if ver:
        head = f"{head} on RHOAI {ver}"
    parts = [f"{e['id']} ({e['reason']})" for e in entries]
    return f"{head}: " + ", ".join(parts)


def write_version_skipped_manifest(
    path: Path,
    plan: dict[str, Any],
) -> list[dict[str, str]]:
    entries = version_skipped_entries(plan)
    payload = {
        "operator_version": str(plan.get("operator_version", "")).strip(),
        "components": entries,
        "summary": format_version_skipped_summary(
            entries,
            operator_version=str(plan.get("operator_version", "")),
        ),
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    return entries


def write_run_smoke_tekton_results(
    flags: dict[str, bool],
    *,
    result_paths: dict[str, str],
    results_base: Path,
) -> int:
    """Write true/false for each RUN_SMOKE_<id> Tekton result file."""
    for cid, selected in flags.items():
        key = component_smoke_result_name(cid)
        raw_path = result_paths.get(key, "").strip()
        if not raw_path:
            print(f"Missing env {key}_PATH for RUN_SMOKE refresh", flush=True)
            return 1
        result_path = Path(raw_path).resolve()
        resolved_base = results_base.resolve()
        if not result_path.is_relative_to(resolved_base):
            print(
                f"ERROR: {key}_PATH={raw_path!r} outside results dir {resolved_base}",
                flush=True,
            )
            return 1
        try:
            result_path.parent.mkdir(parents=True, exist_ok=True)
            result_path.write_text("true" if selected else "false", encoding="utf-8")
        except OSError as exc:
            print(f"ERROR: could not write {key} result: {exc}", flush=True)
            return 1
    return 0
