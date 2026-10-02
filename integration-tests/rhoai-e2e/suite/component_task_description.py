"""Build Konflux/Tekton task descriptions from the smoke component catalog."""

from __future__ import annotations

import re

from .component_catalog_models import SmokeComponent

_TASKS_DIR = "integration-tests/rhoai-e2e/tekton/tasks"
_GENERATED_DIR = f"{_TASKS_DIR}/generated"
_DEFAULT_MIN_PASS_RATE_FOR_SUCCESS = 0.9

_RUNNER_FRAMEWORK_LABELS: dict[str, str] = {
    "golang-ginkgo": "Test framework: golang ginkgo suite",
    "golang-test-tier": "Test framework: golang tiered tests",
    "external-pytest": "Test framework: pytest (dedicated test image)",
    "playwright": "Test framework: Playwright browser tests",
    "cypress-dashboard": "Test framework: Cypress E2E (dashboard)",
}


def pipeline_task_name(component_id: str) -> str:
    return f"test-{component_id.replace('_', '-')}"


def component_tekton_task_base(comp: SmokeComponent) -> str:
    """Base task YAML filename under tekton/tasks/ (not generated)."""
    if comp.is_pending_runner:
        return "task-component-pending.yaml"
    if comp.runner is None:
        return "task-component-pytest.yaml"
    rtype = comp.runner.type
    if rtype in ("golang-ginkgo", "golang-test-tier", "external-pytest"):
        return "task-component-golang.yaml"
    if rtype == "playwright":
        return "task-component-playwright.yaml"
    if rtype == "cypress-dashboard":
        return "task-component-dashboard-cypress.yaml"
    raise ValueError(f"unsupported runner type {rtype!r} for component {comp.id!r}")


def generated_task_path_in_repo(component_id: str) -> str:
    return f"{_GENERATED_DIR}/component-{component_id}.yaml"


def generated_task_metadata_name(component_id: str) -> str:
    return f"rhoai-e2e-component-{component_id.replace('_', '-')}"


def _format_description(parts: list[str]) -> str:
    """Intro sentence plus bullet list for Konflux task Details."""
    cleaned = [p.strip().rstrip(".") for p in parts if p and p.strip()]
    if not cleaned:
        return ""
    intro = cleaned[0]
    bullets = cleaned[1:]
    lines = [f"{intro}."]
    if bullets:
        lines.append("")
        lines.extend(f"- {item}." for item in bullets)
    return "\n".join(lines)


def _framework_label(comp: SmokeComponent) -> str:
    if comp.runner is None:
        return "Test framework: opendatahub-tests (pytest)"
    return _RUNNER_FRAMEWORK_LABELS.get(comp.runner.type, f"Test framework: {comp.runner.type}")


def _pytest_command(comp: SmokeComponent) -> str:
    cmd = f"pytest {comp.tests_subdir} -m '{comp.pytest_marker}'"
    if comp.pytest_extra_args.strip():
        cmd = f"{cmd} {comp.pytest_extra_args.strip()}"
    return cmd


def _needs_tenant_credentials(comp: SmokeComponent) -> bool:
    if comp.requires_shift_left_env:
        return True
    runner = comp.runner
    return bool(runner and runner.vault_secret_key)


def build_component_task_description(comp: SmokeComponent) -> str:
    """Human-readable Task description for Konflux task Details panel."""
    parts: list[str] = [comp.description.strip().rstrip(".")]

    if comp.is_pending_runner:
        msg = (comp.runner.phase_commands.get("smoke") if comp.runner else "") or "pending"
        parts.append(f"Konflux runner not implemented yet ({msg})")
    else:
        parts.append(_framework_label(comp))
        if comp.runner is None:
            parts.append(f"Command: {_pytest_command(comp)}")
        else:
            runner = comp.runner
            if runner.image:
                parts.append(f"Image: {runner.image}")
            smoke_cmd = runner.phase_commands.get("smoke", "")
            if smoke_cmd:
                parts.append(f"Smoke command: {smoke_cmd}")
        if comp.opendatahub_tests_image:
            parts.append("Uses a catalog-pinned opendatahub-tests image")

    if _needs_tenant_credentials(comp):
        parts.append("Requires tenant credentials supplied by the pipeline")

    if comp.requires_minimal_deps:
        deps = comp.setup_dependencies_args or "-M"
        parts.append(f"Requires install-dep-operators minimal deps ({deps})")

    if comp.component_test_timeout_by_gate:
        timeout_bits = ", ".join(
            f"{gate} {value}" for gate, value in sorted(comp.component_test_timeout_by_gate.items())
        )
        parts.append(f"Timeouts: {timeout_bits}")
    elif comp.component_test_timeout:
        parts.append(f"Timeout: {comp.component_test_timeout}")

    if (
        comp.min_pass_rate_for_success is not None
        and comp.min_pass_rate_for_success != _DEFAULT_MIN_PASS_RATE_FOR_SUCCESS
    ):
        pct = comp.min_pass_rate_for_success * 100.0
        parts.append(f"Min pass rate for Tekton success: {pct:.0f}%")

    version_bits: list[str] = []
    if comp.min_rhoai:
        version_bits.append(f"minRhoai {comp.min_rhoai}")
    if comp.max_rhoai:
        version_bits.append(f"maxRhoai {comp.max_rhoai}")
    if version_bits:
        parts.append(f"Version gate: {', '.join(version_bits)}")

    runner = comp.runner
    if runner is not None and runner.cypress is not None:
        sets = runner.cypress.gates.get("smoke") or ()
        parts.append(f"Cypress runs {len(sets)} parallel smoke set(s)")

    if comp.run_order == "last":
        parts.append("Runs last in the component test chain")

    if comp.non_blocking_on_timeout:
        parts.append("Non-blocking on pytest timeout (records failure without failing Tekton when configured)")

    return _format_description(parts)


def format_pipeline_task_description(description: str) -> str:
    """YAML block for PipelineTask description (shown when the task is skipped)."""
    body = description.rstrip("\n")
    if not body:
        return '      description: ""\n'
    lines = ["      description: |", *[f"        {line}" if line else "" for line in body.split("\n")]]
    return "\n".join(lines) + "\n"


def extract_pipeline_task_description_from_block(block: str) -> str:
    """Parse PipelineTask description body from a pipeline task YAML block."""
    if re.search(r'^      description: ""\s*$', block, re.MULTILINE):
        return ""
    desc_start = block.find("      description: |")
    if desc_start < 0:
        raise ValueError("missing pipeline task description block")
    body_lines: list[str] = []
    for line in block[desc_start:].split("\n")[1:]:
        if re.match(r"^      \w+:", line):
            break
        if line.startswith("        "):
            body_lines.append(line[8:])
        elif line == "":
            body_lines.append("")
        else:
            break
    return "\n".join(body_lines)
