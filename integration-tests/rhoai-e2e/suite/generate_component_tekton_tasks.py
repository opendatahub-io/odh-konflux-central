#!/usr/bin/env python3
"""Generate per-component Tekton Task YAMLs with catalog-specific descriptions.

Konflux task Details read ``Task.spec.description`` from the git-resolved Task, not
the PipelineTask description. This script copies the shared component task template
for each catalog entry, sets a rich description, and updates ``rhoai-e2e-pipeline.yaml``
``pathInRepo`` + ``description`` fields.

Usage (from integration-tests/rhoai-e2e):

    python3 -m suite.generate_component_tekton_tasks
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

import yaml

from suite.component_catalog import default_components_smoke_config_path, load_components_smoke_catalog
from suite.component_smoke_flag_refresh import run_smoke_when_expression
from suite.component_test_timeout import pipeline_task_timeout_from_smoke, resolve_component_test_timeout_raw
from suite.component_task_description import (
    build_component_task_description,
    component_tekton_task_base,
    format_pipeline_task_description,
    generated_task_metadata_name,
    generated_task_path_in_repo,
    pipeline_task_name,
)

_RHOAI_E2E = Path(__file__).resolve().parent.parent
_TASKS = _RHOAI_E2E / "tekton" / "tasks"
_GENERATED = _TASKS / "generated"
_PIPELINE = _RHOAI_E2E / "tekton" / "pipelines" / "rhoai-e2e-pipeline.yaml"
_GENERATOR = "suite/generate_component_tekton_tasks.py"

_RUN_COMPONENT_TESTS_WHEN = (
    '        - input: "$(tasks.parse-pipeline-tests.results.RUN_COMPONENT_TESTS)"\n'
    '          operator: in\n'
    '          values: ["true"]\n'
)
_RUN_SMOKE_PARAM_RE = re.compile(
    r"\n        - name: RUN_SMOKE\n          value: [^\n]+\n",
)
_BROKEN_PARAM_MERGE_RE = re.compile(
    r"(          value: [^\n]+?)        - name: ",
)
_RUN_SMOKE_WHEN_RE = re.compile(
    r"\n        - input: \"\$\(tasks\.(?:resolve-component-run-flags|opendatahub-tests-prepare|parse-pipeline-tests)"
    r"\.results\.RUN_SMOKE_[^\"]+\)\"\n          operator: in\n          values: \[\"true\"\]"
)


class _LiteralStr(str):
    pass


def _literal_str_representer(dumper: yaml.SafeDumper, data: str) -> yaml.nodes.ScalarNode:
    return dumper.represent_scalar("tag:yaml.org,2002:str", data, style="|")


yaml.SafeDumper.add_representer(_LiteralStr, _literal_str_representer)


def _write_generated_task(*, base_name: str, comp_id: str, description: str, out_path: Path) -> None:
    base_path = _TASKS / base_name
    doc = yaml.safe_load(base_path.read_text(encoding="utf-8"))
    doc["metadata"]["name"] = generated_task_metadata_name(comp_id)
    doc["spec"]["description"] = _LiteralStr(description.rstrip() + "\n")
    header = (
        f"# Generated from ../{base_name} by {_GENERATOR}; do not edit.\n"
        f"# Regenerate: python3 -m {_GENERATOR}\n"
    )
    body = yaml.safe_dump(doc, sort_keys=False, allow_unicode=True, width=120)
    out_path.write_text(header + body, encoding="utf-8")


def _pipeline_task_block_bounds(pipeline_text: str, task_name: str) -> tuple[int, int]:
    marker = f"    - name: {task_name}\n"
    start = pipeline_text.find(marker)
    if start < 0:
        raise RuntimeError(f"pipeline task {task_name!r} not found")
    next_task = pipeline_text.find("\n    - name:", start + len(marker))
    end = next_task if next_task >= 0 else len(pipeline_text)
    return start, end


def _sync_pipeline_task_run_smoke(pipeline_text: str, task_name: str, comp_id: str) -> str:
    """Grey-skip unselected components via pipeline when; drop in-task RUN_SMOKE param."""
    start, end = _pipeline_task_block_bounds(pipeline_text, task_name)
    block = pipeline_text[start:end]
    new_block, param_removed = _RUN_SMOKE_PARAM_RE.subn("", block)
    new_block, _ = _BROKEN_PARAM_MERGE_RE.subn(r"\1\n        - name: ", new_block)
    new_block, when_removed = _RUN_SMOKE_WHEN_RE.subn("", new_block)
    _ = when_removed
    if _RUN_SMOKE_PARAM_RE.search(block) and not param_removed:
        raise RuntimeError(f"missing RUN_SMOKE param for pipeline task {task_name!r}")
    expr = run_smoke_when_expression(comp_id)
    run_smoke_when = (
        f'        - input: "{expr}"\n'
        '          operator: in\n'
        '          values: ["true"]\n'
    )
    if f'input: "{expr}"' not in new_block:
        if _RUN_COMPONENT_TESTS_WHEN not in new_block:
            raise RuntimeError(f"missing RUN_COMPONENT_TESTS when for pipeline task {task_name!r}")
        new_block = new_block.replace(_RUN_COMPONENT_TESTS_WHEN, _RUN_COMPONENT_TESTS_WHEN + run_smoke_when, 1)
    return pipeline_text[:start] + new_block + pipeline_text[end:]


def _replace_task_path_in_repo(pipeline_text: str, task_name: str, new_path: str) -> str:
    start, end = _pipeline_task_block_bounds(pipeline_text, task_name)
    block = pipeline_text[start:end]
    new_block, count = re.subn(
        r"(          - name: pathInRepo\n            value: ).+",
        rf"\1{new_path}",
        block,
        count=1,
    )
    if count != 1:
        raise RuntimeError(f"failed to update pathInRepo for pipeline task {task_name!r}")
    return pipeline_text[:start] + new_block + pipeline_text[end:]


def _replace_task_description(pipeline_text: str, task_name: str, description: str) -> str:
    start, end = _pipeline_task_block_bounds(pipeline_text, task_name)
    block = pipeline_text[start:end]
    lines = block.split("\n")
    out: list[str] = []
    i = 0
    replaced = False
    while i < len(lines):
        if lines[i].startswith("      description:"):
            out.append(format_pipeline_task_description(description).rstrip("\n"))
            replaced = True
            i += 1
            while i < len(lines) and not re.match(r"^      \w+:", lines[i]):
                i += 1
            continue
        out.append(lines[i])
        i += 1
    if not replaced:
        raise RuntimeError(f"failed to update description for pipeline task {task_name!r}")
    return pipeline_text[:start] + "\n".join(out) + pipeline_text[end:]


def _replace_task_timeout(pipeline_text: str, task_name: str, timeout: str) -> str:
    start, end = _pipeline_task_block_bounds(pipeline_text, task_name)
    block = pipeline_text[start:end]
    new_block, count = re.subn(
        r"      timeout: .+",
        f"      timeout: {timeout}",
        block,
        count=1,
    )
    if count != 1:
        raise RuntimeError(f"failed to update timeout for pipeline task {task_name!r}")
    return pipeline_text[:start] + new_block + pipeline_text[end:]


def _pipeline_task_timeout_raw(comp, catalog_gate_defaults: dict[str, str]) -> str:
    by_gate = comp.component_test_timeout_by_gate or {}
    phases = tuple(sorted(set(by_gate.keys()) | set(catalog_gate_defaults.keys())))
    return resolve_component_test_timeout_raw(
        phases=phases or ("smoke",),
        component_default=comp.component_test_timeout or "",
        component_by_gate=by_gate,
        catalog_gate_defaults=catalog_gate_defaults,
    )


def generate(*, rhoai_e2e_root: Path | None = None, write_pipeline: bool = True) -> list[str]:
    root = rhoai_e2e_root or _RHOAI_E2E
    catalog = load_components_smoke_catalog(default_components_smoke_config_path())
    generated_dir = root / "tekton" / "tasks" / "generated"
    generated_dir.mkdir(parents=True, exist_ok=True)

    descriptions = {
        comp_id: build_component_task_description(comp)
        for comp_id, comp in catalog.components.items()
    }

    written: list[str] = []
    for comp_id, comp in catalog.components.items():
        base = component_tekton_task_base(comp)
        out_path = generated_dir / f"component-{comp_id}.yaml"
        _write_generated_task(
            base_name=base,
            comp_id=comp_id,
            description=descriptions[comp_id],
            out_path=out_path,
        )
        written.append(str(out_path.relative_to(root)))

    if write_pipeline:
        pipeline_path = root / "tekton" / "pipelines" / "rhoai-e2e-pipeline.yaml"
        pipeline_text = pipeline_path.read_text(encoding="utf-8")
        for comp_id in catalog.component_ids:
            task_name = pipeline_task_name(comp_id)
            path_in_repo = generated_task_path_in_repo(comp_id)
            pipeline_text = _replace_task_path_in_repo(pipeline_text, task_name, path_in_repo)
            pipeline_text = _replace_task_description(
                pipeline_text, task_name, descriptions[comp_id]
            )
            comp = catalog.components[comp_id]
            gate_defaults = dict(catalog.default_component_test_timeout_by_gate or {})
            timeout_raw = _pipeline_task_timeout_raw(comp, gate_defaults)
            pipeline_text = _replace_task_timeout(
                pipeline_text,
                task_name,
                pipeline_task_timeout_from_smoke(timeout_raw) if timeout_raw else "45m0s",
            )
            pipeline_text = _sync_pipeline_task_run_smoke(pipeline_text, task_name, comp_id)
        pipeline_path.write_text(pipeline_text, encoding="utf-8")

    stale = sorted(
        p.name
        for p in generated_dir.glob("component-*.yaml")
        if p.stem.removeprefix("component-") not in catalog.components
    )
    if stale:
        raise RuntimeError(f"stale generated task files (remove manually): {stale}")
    return written


def main() -> int:
    try:
        paths = generate()
    except (OSError, RuntimeError, KeyError, ValueError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    print(f"Generated {len(paths)} component task YAML(s) and updated {_PIPELINE.name}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
