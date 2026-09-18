#!/usr/bin/env python3
"""Write resolved component smoke plan JSON for the opendatahub-tests pytest step.

Konflux-test image has PyYAML/yq; the test image may not. The pytest step reads
COMPONENT_TEST_PLAN_JSON instead of parsing YAML in-cluster.

Env (required):
    COMPONENTS_CSV, COMPONENTS_CONFIG, COMPONENT_TEST_PLAN_JSON (output path)
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

from _bootstrap import ensure_rhoai_e2e_path

ensure_rhoai_e2e_path()

from suite.component_phases import parse_component_test_phases
from suite.component_catalog import load_components_smoke_catalog
from suite.component_catalog_models import CypressRunnerConfig, SmokeComponent
from suite.component_plan import parse_components_selection
from suite.component_version_gate import (
    component_enabled_for_version,
    resolve_operator_version_for_gates,
)


def _cypress_to_dict(cy: CypressRunnerConfig) -> dict[str, object]:
    return {
        "skip_tags": cy.skip_tags,
        "test_timeout_seconds": cy.test_timeout_seconds,
        "parallel_stagger_sec": cy.parallel_stagger_sec,
        "max_parallel": cy.max_parallel,
        "display_base": cy.display_base,
        "run_config": cy.run_config,
        "gates": {
            gate: [
                {"grep_tag": item.grep_tag, "results_subdir": item.results_subdir}
                for item in sets
            ]
            for gate, sets in cy.gates.items()
        },
    }


def _component_to_dict(comp: SmokeComponent) -> dict[str, object]:
    out: dict[str, object] = {
        "id": comp.id,
        "pytest_marker": comp.pytest_marker,
        "pytest_extra_args": comp.pytest_extra_args,
        "tests_subdir": comp.tests_subdir,
        "artifact_prefix": comp.artifact_prefix,
    }
    if comp.min_pass_rate_for_success is not None:
        out["min_pass_rate_for_success"] = comp.min_pass_rate_for_success
    if comp.non_blocking_on_timeout:
        out["non_blocking_on_timeout"] = True
    if comp.component_test_timeout:
        out["component_test_timeout"] = comp.component_test_timeout
    if comp.component_test_timeout_by_gate:
        out["component_test_timeout_by_gate"] = dict(comp.component_test_timeout_by_gate)
    if comp.phase_markers:
        out["phase_markers"] = dict(comp.phase_markers)
    if comp.runner is not None:
        runner_out: dict[str, object] = {
            "type": comp.runner.type,
            "image": comp.runner.image,
            "working_dir": comp.runner.working_dir,
            "results_dir": comp.runner.results_dir,
            "phase_commands": dict(comp.runner.phase_commands),
            "vault_secret_key": comp.runner.vault_secret_key,
        }
        if comp.runner.env_defaults:
            runner_out["env_defaults"] = dict(comp.runner.env_defaults)
        if comp.runner.source_repo:
            runner_out["source_repo"] = comp.runner.source_repo
            runner_out["source_ref"] = comp.runner.source_ref
        if comp.runner.cypress is not None:
            runner_out["cypress"] = _cypress_to_dict(comp.runner.cypress)
        out["runner"] = runner_out
    return out


def main() -> int:
    csv = os.environ.get("COMPONENTS_CSV", "").strip()
    cfg_path = os.environ.get("COMPONENTS_CONFIG", "").strip()
    out_path = os.environ.get("COMPONENT_TEST_PLAN_JSON", "").strip()
    if not csv or not cfg_path or not out_path:
        print("COMPONENTS_CSV, COMPONENTS_CONFIG, and COMPONENT_TEST_PLAN_JSON are required", file=sys.stderr)
        return 1

    catalog = load_components_smoke_catalog(Path(cfg_path))
    selected = parse_components_selection(csv, catalog)
    ordered = [c for c in catalog.component_ids if c in selected]
    phases = parse_component_test_phases(os.environ.get("TEST_GATES", os.environ.get("COMPONENT_TEST_PHASES", "")))
    product = os.environ.get("PRODUCT", "").strip().lower()
    operator_version = resolve_operator_version_for_gates()
    plan_components: list[dict[str, object]] = []
    for cid in ordered:
        comp = catalog.components[cid]
        entry = _component_to_dict(comp)
        gate = component_enabled_for_version(comp, operator_version, product=product)
        if not gate.enabled:
            entry["version_skip_reason"] = gate.reason
            print(f"Version gate: skip {cid} — {gate.reason}", flush=True)
        plan_components.append(entry)
    for comp in plan_components:
        phase_markers = comp.get("phase_markers") or {}
        if not isinstance(phase_markers, dict):
            phase_markers = {}
        if comp.get("version_skip_reason"):
            continue
        for phase in phases:
            if phase not in phase_markers:
                print(
                    f"ERROR: component {comp.get('id')!r} missing qualityGatesMap.default.{phase} in catalog",
                    file=sys.stderr,
                )
                return 1
    plan: dict[str, object] = {
        "component_test_phases": list(phases),
        "operator_version": operator_version or "(unknown)",
        "components": plan_components,
    }
    if catalog.default_component_test_timeout_by_gate:
        plan["default_component_test_timeout_by_gate"] = dict(
            catalog.default_component_test_timeout_by_gate
        )
    out = Path(out_path)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(plan, indent=2) + "\n", encoding="utf-8")
    print(f"Wrote component test plan ({len(ordered)} component(s)) to {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
