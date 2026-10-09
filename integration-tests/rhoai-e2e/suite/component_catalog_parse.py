"""Parse rhoai-e2e-components-smoke.yaml entries into SmokeComponent records."""

from __future__ import annotations

import re
import shlex
from pathlib import Path
from typing import Any

from .errors import AppError

from .component_catalog_models import (
    ComponentRunner,
    CypressParallelSet,
    CypressRunnerConfig,
    SmokeComponent,
)
from components.dashboard_cypress.config import normalize_cypress_run_config

_JUNIT_SUITE_RE = re.compile(r"^junit_suite_name=(.+)$", re.I)


def normalize_marker(raw: str) -> str:
    marker, _ = split_quality_gate_pytest(raw)
    return marker


def split_quality_gate_pytest(raw: str) -> tuple[str, str]:
    """Split qualityGatesMap pytest value into ``-m`` marker and trailing CLI flags."""
    text = raw.strip()
    if text.startswith("-m "):
        text = text[3:].strip()
    elif text.startswith("-m"):
        text = text[2:].lstrip()
    if not text:
        return "", ""
    try:
        parts = shlex.split(text)
    except ValueError:
        fallback = text.strip().strip('"').strip("'")
        return fallback, ""
    if not parts:
        return "", ""
    marker_tokens: list[str] = []
    extra_tokens: list[str] = []
    in_extra = False
    for part in parts:
        if not in_extra and part.startswith("-"):
            in_extra = True
        if in_extra:
            extra_tokens.append(part)
        else:
            marker_tokens.append(part)
    marker = " ".join(marker_tokens) if marker_tokens else parts[0]
    return marker, " ".join(extra_tokens)


def tests_subdir_from_args(args: list[Any]) -> str:
    for item in reversed(args):
        s = str(item).strip().rstrip("/")
        if s.startswith("tests/"):
            return s
    raise AppError("image.args must include a tests/… path", 2)


def junit_suite_from_args(args: list[Any]) -> str | None:
    for item in args:
        s = str(item).strip()
        if s.startswith("-o "):
            s = s[3:].strip()
        m = _JUNIT_SUITE_RE.match(s)
        if m:
            return m.group(1).strip()
    return None


def pytest_extra_from_args(args: list[Any], *, konflux_extra: str) -> str:
    parts = ["-svv"]
    skip: set[str] = set()
    for item in args:
        s = str(item).strip()
        if not s or s in skip:
            continue
        if s.startswith("tests/"):
            continue
        if s.startswith("-o "):
            parts.append(s)
            continue
        if _JUNIT_SUITE_RE.match(s.removeprefix("-o ").strip()):
            parts.append(f"-o {s}" if not s.startswith("-o ") else s)
            continue
        parts.append(s)
    extra = konflux_extra.strip()
    if extra:
        parts.append(extra)
    return " ".join(parts)


def parse_min_pass_rate_for_success(
    konflux: dict[str, Any], path: Path, label: str
) -> float | None:
    min_success_raw = konflux.get("minPassRateForSuccess")
    min_success: float | None = None
    if min_success_raw is not None:
        try:
            min_success = float(min_success_raw)
        except (TypeError, ValueError) as exc:
            raise AppError(f"{label}.konflux.minPassRateForSuccess must be a number in {path}", 2) from exc
        if not (0.0 < min_success <= 1.0):
            raise AppError(f"{label}.konflux.minPassRateForSuccess must be in (0, 1] in {path}", 2)

    return min_success


def parse_catalog_konflux(doc: dict[str, Any], path: Path) -> str | None:
    raw = doc.get("konflux")
    if raw is None:
        return None
    if not isinstance(raw, dict):
        raise AppError(f"konflux must be a mapping at root of {path}", 2)
    secret_raw = raw.get("shiftLeftEnvSecret")
    if secret_raw is None:
        return None
    if not isinstance(secret_raw, str) or not secret_raw.strip():
        raise AppError(f"konflux.shiftLeftEnvSecret must be a non-empty string in {path}", 2)
    return secret_raw.strip()


def parse_timeout_by_gate(raw: Any, path: Path, label: str) -> dict[str, str] | None:
    if raw is None:
        return None
    if not isinstance(raw, dict):
        raise AppError(f"{label} must be a mapping in {path}", 2)
    out: dict[str, str] = {}
    for gate_raw, timeout_raw in raw.items():
        if not isinstance(gate_raw, str) or not gate_raw.strip():
            raise AppError(f"{label} keys must be non-empty gate ids in {path}", 2)
        if not isinstance(timeout_raw, str) or not timeout_raw.strip():
            raise AppError(f"{label}.{gate_raw} must be a non-empty duration string in {path}", 2)
        out[gate_raw.strip()] = timeout_raw.strip()
    return out or None


def parse_catalog_konflux_timeout_defaults(doc: dict[str, Any], path: Path) -> dict[str, str] | None:
    raw = doc.get("konflux")
    if not isinstance(raw, dict):
        return None
    return parse_timeout_by_gate(
        raw.get("defaultComponentTestTimeoutByGate"),
        path,
        "konflux.defaultComponentTestTimeoutByGate",
    )


def _parse_cypress_parallel_set(raw: Any, label: str, path: Path, index: int) -> CypressParallelSet:
    if isinstance(raw, str) and raw.strip():
        grep_tag = raw.strip()
        return CypressParallelSet(grep_tag=grep_tag, results_subdir=grep_tag.lstrip("@"))
    if not isinstance(raw, dict):
        raise AppError(
            f"{label}[{index}] must be a grepTag string or mapping in {path}",
            2,
        )
    grep_raw = raw.get("grepTag")
    if not isinstance(grep_raw, str) or not grep_raw.strip():
        raise AppError(f"{label}[{index}].grepTag must be a non-empty string in {path}", 2)
    grep_tag = grep_raw.strip()
    results_raw = raw.get("resultsSubdir")
    if results_raw is None:
        results_subdir = grep_tag.lstrip("@")
    elif isinstance(results_raw, str) and results_raw.strip():
        results_subdir = results_raw.strip()
    else:
        raise AppError(
            f"{label}[{index}].resultsSubdir must be a non-empty string in {path}",
            2,
        )
    return CypressParallelSet(grep_tag=grep_tag, results_subdir=results_subdir)


def _parse_cypress_runner_config(raw: dict[str, Any], label: str, path: Path) -> CypressRunnerConfig:
    cy = raw.get("cypress")
    if not isinstance(cy, dict):
        raise AppError(f"{label}.konflux.runner.cypress required for cypress-dashboard in {path}", 2)
    skip_raw = cy.get("skipTags")
    if not isinstance(skip_raw, str) or not skip_raw.strip():
        raise AppError(f"{label}.konflux.runner.cypress.skipTags must be a non-empty string in {path}", 2)
    gates_raw = cy.get("gates")
    if not isinstance(gates_raw, dict) or not gates_raw:
        raise AppError(f"{label}.konflux.runner.cypress.gates must be a non-empty mapping in {path}", 2)
    gates: dict[str, tuple[CypressParallelSet, ...]] = {}
    for gate_key, sets_raw in gates_raw.items():
        if not isinstance(gate_key, str) or not gate_key.strip():
            continue
        if not isinstance(sets_raw, list) or not sets_raw:
            raise AppError(
                f"{label}.konflux.runner.cypress.gates.{gate_key} must be a non-empty list in {path}",
                2,
            )
        gate_label = f"{label}.konflux.runner.cypress.gates.{gate_key}"
        gates[gate_key.strip()] = tuple(
            _parse_cypress_parallel_set(item, gate_label, path, i) for i, item in enumerate(sets_raw)
        )
    if "smoke" not in gates:
        raise AppError(f"{label}.konflux.runner.cypress.gates.smoke required in {path}", 2)
    if "tier1" not in gates:
        gates["tier1"] = gates["smoke"]
    test_timeout = "480"
    timeout_raw = cy.get("testTimeoutSeconds")
    if timeout_raw is not None:
        if not isinstance(timeout_raw, str) or not timeout_raw.strip():
            raise AppError(
                f"{label}.konflux.runner.cypress.testTimeoutSeconds must be a non-empty string in {path}",
                2,
            )
        test_timeout = timeout_raw.strip()
    stagger = 15
    stagger_raw = cy.get("parallelStaggerSec")
    if stagger_raw is not None:
        try:
            stagger = int(stagger_raw)
        except (TypeError, ValueError) as exc:
            raise AppError(
                f"{label}.konflux.runner.cypress.parallelStaggerSec must be an integer in {path}",
                2,
            ) from exc
    display_base = 99
    display_raw = cy.get("displayBase")
    if display_raw is not None:
        try:
            display_base = int(display_raw)
        except (TypeError, ValueError) as exc:
            raise AppError(
                f"{label}.konflux.runner.cypress.displayBase must be an integer in {path}",
                2,
            ) from exc
    run_config = normalize_cypress_run_config(
        "numTestsKeptInMemory=0,experimentalMemoryManagement=true,"
        "video=false,viewportWidth=1920,viewportHeight=1080"
    )
    config_raw = cy.get("runConfig")
    if config_raw is not None:
        if not isinstance(config_raw, str) or not config_raw.strip():
            raise AppError(f"{label}.konflux.runner.cypress.runConfig must be a non-empty string in {path}", 2)
        run_config = normalize_cypress_run_config(config_raw)
    max_parallel: int | None = None
    max_raw = cy.get("maxParallel")
    if max_raw is not None:
        try:
            max_parallel = int(max_raw)
        except (TypeError, ValueError) as exc:
            raise AppError(
                f"{label}.konflux.runner.cypress.maxParallel must be an integer in {path}",
                2,
            ) from exc
        if max_parallel < 1:
            raise AppError(
                f"{label}.konflux.runner.cypress.maxParallel must be >= 1 in {path}",
                2,
            )
    return CypressRunnerConfig(
        skip_tags=skip_raw.strip(),
        gates=gates,
        test_timeout_seconds=test_timeout,
        parallel_stagger_sec=stagger,
        max_parallel=max_parallel,
        display_base=display_base,
        run_config=run_config,
    )


def parse_runner(konflux: dict[str, Any], path: Path, label: str) -> ComponentRunner | None:
    raw = konflux.get("runner")
    if raw is None:
        return None
    if not isinstance(raw, dict):
        raise AppError(f"{label}.konflux.runner must be a mapping in {path}", 2)
    rtype = raw.get("type")
    if not isinstance(rtype, str) or not rtype.strip():
        raise AppError(f"{label}.konflux.runner.type must be a non-empty string in {path}", 2)
    rtype = rtype.strip()
    if rtype == "pending":
        message_raw = raw.get("pendingMessage", "Component test not yet implemented in Konflux.")
        if not isinstance(message_raw, str) or not message_raw.strip():
            raise AppError(f"{label}.konflux.runner.pendingMessage must be a non-empty string in {path}", 2)
        message = message_raw.strip()
        return ComponentRunner(
            type="pending",
            image="",
            working_dir="",
            results_dir="",
            phase_commands={"smoke": message, "tier1": message},
        )
    image = raw.get("image")
    if not isinstance(image, str) or not image.strip():
        raise AppError(f"{label}.konflux.runner.image must be a non-empty string in {path}", 2)
    working_dir = raw.get("workingDir")
    if not isinstance(working_dir, str) or not working_dir.strip():
        raise AppError(f"{label}.konflux.runner.workingDir must be a non-empty string in {path}", 2)
    results_dir = raw.get("resultsDir")
    if not isinstance(results_dir, str) or not results_dir.strip():
        raise AppError(f"{label}.konflux.runner.resultsDir must be a non-empty string in {path}", 2)
    phase_cmds_raw = raw.get("phaseCommands")
    if not isinstance(phase_cmds_raw, dict) or not phase_cmds_raw:
        raise AppError(f"{label}.konflux.runner.phaseCommands must be a non-empty mapping in {path}", 2)
    phase_commands: dict[str, str] = {}
    for gate_key, gate_val in phase_cmds_raw.items():
        if not isinstance(gate_key, str) or not str(gate_key).strip():
            continue
        if not isinstance(gate_val, str) or not gate_val.strip():
            raise AppError(
                f"{label}.konflux.runner.phaseCommands.{gate_key} must be a non-empty string in {path}",
                2,
            )
        phase_commands[gate_key.strip()] = gate_val.strip()
    if "smoke" not in phase_commands:
        raise AppError(f"{label}.konflux.runner.phaseCommands.smoke required in {path}", 2)
    if "tier1" not in phase_commands:
        phase_commands["tier1"] = phase_commands["smoke"]
    vault_raw = raw.get("vaultSecretKey", konflux.get("vaultSecretKey", ""))
    vault_secret_key = ""
    if vault_raw is not None:
        if not isinstance(vault_raw, str):
            raise AppError(f"{label}.konflux.runner.vaultSecretKey must be a string in {path}", 2)
        vault_secret_key = vault_raw.strip()
    env_defaults: dict[str, str] | None = None
    env_raw = raw.get("envDefaults")
    if env_raw is not None:
        if not isinstance(env_raw, dict) or not env_raw:
            raise AppError(f"{label}.konflux.runner.envDefaults must be a non-empty mapping in {path}", 2)
        env_defaults = {}
        for env_key, env_val in env_raw.items():
            if not isinstance(env_key, str) or not env_key.strip():
                continue
            if not isinstance(env_val, str) or not env_val.strip():
                raise AppError(
                    f"{label}.konflux.runner.envDefaults.{env_key} must be a non-empty string in {path}",
                    2,
                )
            env_defaults[env_key.strip()] = env_val.strip()
    source_repo = ""
    source_repo_raw = raw.get("sourceRepo")
    if source_repo_raw is not None:
        if not isinstance(source_repo_raw, str) or not source_repo_raw.strip():
            raise AppError(f"{label}.konflux.runner.sourceRepo must be a non-empty string in {path}", 2)
        source_repo = source_repo_raw.strip()
    source_ref = "main"
    source_ref_raw = raw.get("sourceRef")
    if source_ref_raw is not None:
        if not isinstance(source_ref_raw, str) or not source_ref_raw.strip():
            raise AppError(f"{label}.konflux.runner.sourceRef must be a non-empty string in {path}", 2)
        source_ref = source_ref_raw.strip()
    cypress_config = _parse_cypress_runner_config(raw, label, path) if rtype == "cypress-dashboard" else None
    return ComponentRunner(
        type=rtype,
        image=image.strip(),
        working_dir=working_dir.strip(),
        results_dir=results_dir.strip(),
        phase_commands=phase_commands,
        vault_secret_key=vault_secret_key,
        env_defaults=env_defaults,
        source_repo=source_repo,
        source_ref=source_ref,
        cypress=cypress_config,
    )


def load_v2_component(item: dict[str, Any], path: Path, index: int) -> SmokeComponent:
    label = f"components[{index}]"
    component = item.get("component")
    if not isinstance(component, dict):
        raise AppError(f"{label} must contain a component mapping in {path}", 2)
    konflux = item.get("konflux")
    if not isinstance(konflux, dict):
        raise AppError(f"{label} must contain a konflux mapping in {path}", 2)

    merge = component.get("merge")
    if not isinstance(merge, dict):
        raise AppError(f"{label}.component.merge required in {path}", 2)
    metadata = merge.get("metadata")
    if not isinstance(metadata, dict):
        raise AppError(f"{label}.component.merge.metadata required in {path}", 2)
    component_name = metadata.get("name")
    if not isinstance(component_name, str) or not component_name.strip():
        raise AppError(f"{label}.component.merge.metadata.name required in {path}", 2)

    cid_raw = konflux.get("id", component_name.replace("-", "_"))
    if not isinstance(cid_raw, str) or not cid_raw.strip():
        raise AppError(f"{label}.konflux.id must be a non-empty string in {path}", 2)
    cid = cid_raw.strip()

    runner = parse_runner(konflux, path, label)

    image = merge.get("image")
    args: list[Any] = []
    if isinstance(image, dict):
        raw_args = image.get("args")
        if isinstance(raw_args, list):
            args = raw_args

    qg = merge.get("qualityGatesMap")
    if not isinstance(qg, dict):
        raise AppError(f"{label}.component.merge.qualityGatesMap required in {path}", 2)
    default_qg = qg.get("default")
    if not isinstance(default_qg, dict) or "smoke" not in default_qg:
        raise AppError(f"{label}.component.merge.qualityGatesMap.default.smoke required in {path}", 2)
    if "tier1" not in default_qg:
        raise AppError(f"{label}.component.merge.qualityGatesMap.default.tier1 required in {path}", 2)
    phase_markers: dict[str, str] = {}
    gate_cli_extras: list[str] = []
    for gate_key, gate_val in default_qg.items():
        if not isinstance(gate_key, str) or not str(gate_key).strip():
            continue
        if runner is not None:
            phase_markers[gate_key.strip()] = str(gate_val).strip().strip('"').strip("'")
        else:
            marker_part, cli_extra = split_quality_gate_pytest(str(gate_val))
            phase_markers[gate_key.strip()] = marker_part
            if cli_extra:
                gate_cli_extras.append(cli_extra)
    marker = phase_markers["smoke"]

    if runner is None:
        if not args:
            raise AppError(f"{label}.component.merge.image.args must be a non-empty list in {path}", 2)
        tests_subdir = tests_subdir_from_args(args)
        konflux_extra = konflux.get("pytestExtraArgs", "")
        if konflux_extra is not None and not isinstance(konflux_extra, str):
            raise AppError(f"{label}.konflux.pytestExtraArgs must be a string in {path}", 2)
        pytest_extra = pytest_extra_from_args(args, konflux_extra=str(konflux_extra or ""))
        if gate_cli_extras:
            joined = " ".join(gate_cli_extras)
            pytest_extra = f"{pytest_extra} {joined}".strip() if pytest_extra else joined
    else:
        tests_subdir = ""
        pytest_extra = ""

    artifact_raw = konflux.get("artifactPrefix")
    if artifact_raw is not None:
        if not isinstance(artifact_raw, str) or not artifact_raw.strip():
            raise AppError(f"{label}.konflux.artifactPrefix must be a non-empty string in {path}", 2)
        artifact_prefix = artifact_raw.strip()
    else:
        suite = junit_suite_from_args(args) or cid.replace("_", "-")
        artifact_prefix = f"{suite}-smoke"

    desc = konflux.get("description", "")
    if not isinstance(desc, str):
        desc = ""

    deps_args = konflux.get("setupDependenciesArgs", "")
    if deps_args is not None and not isinstance(deps_args, str):
        raise AppError(f"{label}.konflux.setupDependenciesArgs must be a string in {path}", 2)

    min_success = parse_min_pass_rate_for_success(konflux, path, label)

    non_blocking_raw = konflux.get("nonBlockingOnTimeout")
    non_blocking = non_blocking_raw is True

    enabled_raw = konflux.get("enabled", True)
    if enabled_raw is False:
        enabled = False
    elif enabled_raw is True or enabled_raw is None:
        enabled = True
    else:
        raise AppError(f"{label}.konflux.enabled must be boolean in {path}", 2)

    comp_timeout_raw = konflux.get("componentTestTimeout")
    comp_timeout: str | None = None
    if comp_timeout_raw is not None:
        if not isinstance(comp_timeout_raw, str) or not comp_timeout_raw.strip():
            raise AppError(f"{label}.konflux.componentTestTimeout must be a non-empty string in {path}", 2)
        comp_timeout = comp_timeout_raw.strip()

    comp_timeout_by_gate = parse_timeout_by_gate(
        konflux.get("componentTestTimeoutByGate"),
        path,
        f"{label}.konflux.componentTestTimeoutByGate",
    )

    run_order_raw = konflux.get("runOrder")
    run_order: str | None = None
    if run_order_raw is not None:
        if not isinstance(run_order_raw, str) or not run_order_raw.strip():
            raise AppError(f"{label}.konflux.runOrder must be a non-empty string in {path}", 2)
        run_order = run_order_raw.strip()

    comp_shift_left_raw = konflux.get("shiftLeftEnvSecret")
    shift_left_env_secret = ""
    if comp_shift_left_raw is not None:
        if not isinstance(comp_shift_left_raw, str) or not comp_shift_left_raw.strip():
            raise AppError(f"{label}.konflux.shiftLeftEnvSecret must be a non-empty string in {path}", 2)
        shift_left_env_secret = comp_shift_left_raw.strip()

    odt_image_raw = konflux.get("opendatahubTestsImage")
    odt_image: str | None = None
    if odt_image_raw is not None:
        if not isinstance(odt_image_raw, str) or not odt_image_raw.strip():
            raise AppError(f"{label}.konflux.opendatahubTestsImage must be a non-empty string in {path}", 2)
        odt_image = odt_image_raw.strip()

    min_rhoai: str | None = None
    max_rhoai: str | None = None
    enablement = component.get("enablement")
    if isinstance(enablement, dict):
        min_raw = enablement.get("minRhoai")
        max_raw = enablement.get("maxRhoai")
        if min_raw is not None:
            min_rhoai = str(min_raw).strip() or None
        if max_raw is not None:
            max_rhoai = str(max_raw).strip() or None

    return SmokeComponent(
        id=cid,
        description=desc.strip(),
        pytest_marker=marker,
        phase_markers=phase_markers,
        pytest_extra_args=pytest_extra,
        tests_subdir=tests_subdir,
        requires_minimal_deps=konflux.get("requiresMinimalDeps") is True,
        setup_dependencies_args=(deps_args or "").strip(),
        artifact_prefix=artifact_prefix,
        min_pass_rate_for_success=min_success,
        non_blocking_on_timeout=non_blocking,
        component_test_timeout=comp_timeout,
        component_test_timeout_by_gate=comp_timeout_by_gate,
        enabled=enabled,
        requires_shift_left_env=konflux.get("requiresShiftLeftEnv") is True,
        shift_left_env_secret=shift_left_env_secret,
        opendatahub_tests_image=odt_image,
        runner=runner,
        run_order=run_order,
        min_rhoai=min_rhoai,
        max_rhoai=max_rhoai,
    )
