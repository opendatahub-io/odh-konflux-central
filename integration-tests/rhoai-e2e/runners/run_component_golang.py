#!/usr/bin/env python3
"""Orchestrate per-component golang/ginkgo tests (Tekton step 1 of task-component-golang).

Writes ``component-golang.env`` and optional ``component-golang.skip`` under ARTIFACTS_DIR.
Step 2 in the Task runs the component image and sources that env file.

Env (required):
    COMPONENT_TEST_COMPONENT_ID
    COMPONENT_TEST_PLAN_JSON
    ARTIFACTS_DIR
Env (optional):
    FAIL_FAST_DISABLED_COMPONENT, COMPONENT_TEST_TIMEOUT, TEST_GATES
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Any

from _bootstrap import ensure_rhoai_e2e_path

ensure_rhoai_e2e_path()

from helpers.log_redact import redact_command_for_log
from suite.component_dsc_gate import smoke_component_prereq_unavailable
from suite.cluster_api_health import (
    cluster_smoke_infra_blocked_reason,
    openshift_guest_rh_ai_route_tekton_unreachable_reason,
)
from suite.dsc_baseline import reconcile_baseline_dsc_before_component
from suite.component_version_gate import resolve_operator_version_for_gates
from suite.component_test_timeout import (
    apply_cluster_source_timeout_cap,
    parse_component_timeout_seconds,
    resolve_component_test_timeout_raw,
)
from suite.test_slice_filter import filter_cypress_config_by_slice_ids
from components.dashboard_cypress.config import (
    _parse_cypress_runner_config,
    apply_dashboard_cypress_runtime_env,
    bootstrap_dashboard_cypress_env,
    cypress_results_subdirs,
    resolve_cypress_run_command,
    sync_cypress_orchestrate_env,
)
from components.dashboard_cypress.source_ref import resolve_dashboard_git_source
from runners.orchestrator import stage_cypress_cli_tools
from runners.component_prereqs import prepare_component_for_smoke, _external_existing_cluster, run_pooled_external_smoke_prep
from runners.component_junit import prereq_junit_outcome, write_single_failure_junit
from runners.run_component_pytest import (
    _accumulate_exit_file,
    _artifacts_dir,
    _filter_component_id,
    _iter_components_from_plan,
    _plan_component_test_phases,
    _plan_gate_timeout_defaults,
    _truthy_env,
)
from steps.cluster_prep_state import cluster_prep_already_done
from steps.tekton_util import prepare_kubeconfig_auth_for_tests
from suite.component_runner_env import component_golang_env_path


def _command_for_phases(phase_commands: dict[str, str], phases: tuple[str, ...]) -> str:
    parts: list[str] = []
    for phase in phases:
        cmd = (phase_commands.get(phase) or "").strip()
        if cmd and cmd not in parts:
            parts.append(cmd)
    if parts:
        return " && ".join(parts)
    if "smoke" in phases:
        return (phase_commands.get("smoke") or "").strip()
    return ""


def _shell_single_quote(value: str) -> str:
    """Quote for bash ``source`` so ``$`` in RUN_COMMAND is not expanded at load time."""
    return "'" + value.replace("'", "'\"'\"'") + "'"



def _resolve_oc_server() -> str:
    from install.dsc_install import oc_run
    from steps.tekton_util import _kubeconfig_api_server

    server_r = oc_run(
        ["whoami", "--show-server"],
        check=False,
        capture_output=True,
        timeout=30,
    )
    if server_r.returncode == 0 and (server_r.stdout or "").strip():
        return (server_r.stdout or "").strip()
    kc = os.environ.get("KUBECONFIG", "").strip()
    if kc:
        return _kubeconfig_api_server(Path(kc))
    return ""


def _write_env_file(
    path: Path,
    *,
    skip: bool,
    working_dir: str,
    results_dir: str,
    artifact_prefix: str,
    run_command: str,
    env_defaults: dict[str, str] | None = None,
    test_timeout_sec: float | None = None,
    source_repo: str = "",
    source_ref: str = "main",
) -> None:
    lines = [
        f"SKIP={'true' if skip else 'false'}",
        f"WORKING_DIR={working_dir}",
        f"RESULTS_DIR={results_dir}",
        f"ARTIFACT_PREFIX={artifact_prefix}",
        f"RUN_COMMAND={_shell_single_quote(run_command)}",
    ]
    if source_repo:
        lines.append(f"SOURCE_REPO={source_repo}")
        lines.append(f"SOURCE_REF={source_ref}")
    if test_timeout_sec is not None:
        lines.append(f"TEST_TIMEOUT_SEC={test_timeout_sec:g}")
    for key, val in sorted((env_defaults or {}).items()):
        if not key.replace("_", "").isalnum() or key[0].isdigit():
            raise ValueError(f"invalid env var name: {key!r}")
        lines.append(f"export {key}={_shell_single_quote(str(val))}")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _write_skip_golang_env(
    *,
    comp: dict[str, Any],
    filter_id: str,
    skip_tag: str,
    testcase_name: str,
    message: str,
    artifacts_dir: Path,
    env_path: Path,
    skip_path: Path,
    working_dir: str,
    results_dir: str,
    run_command: str,
    env_defaults: dict[str, str] | None,
    test_timeout_sec: float,
    source_repo: str,
    source_ref: str,
    outcome: str = "skip",
) -> int:
    print(f"SKIP {filter_id}: {message}", flush=True)
    write_single_failure_junit(
        comp,
        artifacts_dir=artifacts_dir,
        testcase_name=testcase_name,
        message=message,
        outcome=outcome,
    )
    skip_path.write_text(f"{skip_tag}\n", encoding="ascii")
    _write_env_file(
        env_path,
        skip=True,
        working_dir=working_dir,
        results_dir=results_dir,
        artifact_prefix=comp["artifact_prefix"],
        run_command=run_command,
        env_defaults=env_defaults,
        test_timeout_sec=test_timeout_sec,
        source_repo=source_repo,
        source_ref=source_ref,
    )
    _accumulate_exit_file(1)
    return 1


def _resolve_runner_from_plan(plan_path: Path, filter_id: str) -> tuple[dict[str, Any], dict[str, Any]]:
    plan_raw: Any = json.loads(plan_path.read_text(encoding="utf-8"))
    runner_raw = next(
        (
            item.get("runner")
            for item in (plan_raw.get("components") or [])
            if isinstance(item, dict) and item.get("id") == filter_id
        ),
        None,
    )
    if not isinstance(runner_raw, dict):
        raise ValueError(f"component {filter_id!r} has no runner in plan")
    return plan_raw, runner_raw


def _resolve_dashboard_cypress_source(
    *,
    filter_id: str,
    plan_raw: dict[str, Any],
    source_repo: str,
    source_ref: str,
) -> tuple[str, str]:
    if filter_id != "dashboard_cypress" or not source_repo:
        return source_repo, source_ref
    plan_operator_version = str(plan_raw.get("operator_version") or "").strip()
    operator_version = (
        plan_operator_version
        if plan_operator_version and plan_operator_version not in {"(unknown)", "n/a"}
        else resolve_operator_version_for_gates()
    )
    resolved = resolve_dashboard_git_source(
        operator_version,
        catalog_repo=source_repo,
        catalog_ref=source_ref,
        product=os.environ.get("PRODUCT", "").strip().lower(),
    )
    if resolved.repo != source_repo or resolved.ref != source_ref:
        print(
            f"Resolved odh-dashboard source "
            f"{source_repo}@{source_ref!r} -> {resolved.repo}@{resolved.ref!r} "
            f"(operator {operator_version or '(unknown)'})",
            flush=True,
        )
    return resolved.repo, resolved.ref


def _build_golang_run_context(
    *,
    filter_id: str,
    runner_raw: dict[str, Any],
    component_phases: tuple[str, ...],
    plan_raw: dict[str, Any],
    artifacts_dir: Path,
) -> tuple[str, dict[str, str] | None, str, str]:
    phase_commands = {str(k): str(v) for k, v in runner_raw.get("phase_commands", {}).items()}
    env_defaults_raw = runner_raw.get("env_defaults")
    env_defaults: dict[str, str] | None = None
    if isinstance(env_defaults_raw, dict) and env_defaults_raw:
        env_defaults = {str(k): str(v) for k, v in env_defaults_raw.items()}
    source_repo = str(runner_raw.get("source_repo") or "").strip()
    source_ref = str(runner_raw.get("source_ref") or "main").strip() or "main"
    source_repo, source_ref = _resolve_dashboard_cypress_source(
        filter_id=filter_id,
        plan_raw=plan_raw,
        source_repo=source_repo,
        source_ref=source_ref,
    )
    run_command = _command_for_phases(phase_commands, component_phases)
    cypress_config = _parse_cypress_runner_config(runner_raw)
    if filter_id == "dashboard_cypress" and cypress_config is not None:
        slice_ids = os.environ.get("TEST_TAGS", "").strip()
        if slice_ids:
            cypress_config = filter_cypress_config_by_slice_ids(
                cypress_config,
                component_phases,
                slice_ids,
            )
        env_defaults = bootstrap_dashboard_cypress_env(env_defaults, artifacts_dir=artifacts_dir)
        apply_dashboard_cypress_runtime_env(env_defaults)
        sync_cypress_orchestrate_env(env_defaults)
        run_command = resolve_cypress_run_command(cypress_config, component_phases)
        subdirs = cypress_results_subdirs(cypress_config, component_phases)
        if subdirs:
            env_defaults = dict(env_defaults or {})
            env_defaults["CYPRESS_RESULTS_SUBDIRS"] = ",".join(subdirs)
    elif filter_id == "dashboard_cypress":
        env_defaults = bootstrap_dashboard_cypress_env(env_defaults, artifacts_dir=artifacts_dir)
    if _external_existing_cluster() and filter_id in {
        "model_registry",
        "mlflow",
        "ai_pipelines",
        "kuberay",
    }:
        try:
            run_pooled_external_smoke_prep(filter_id)
        except Exception as exc:
            print(
                f"WARN: pooled smoke prep for {filter_id} failed ({exc}); continuing",
                file=sys.stderr,
                flush=True,
            )
    if filter_id == "ai_pipelines" and _external_existing_cluster():
        env_defaults = dict(env_defaults or {})
        env_defaults.setdefault("DEPLOYMENT_TIMEOUT", "600")
        env_defaults.setdefault("DEPLOY_TIMEOUT", "600")
    return run_command, env_defaults, source_repo, source_ref


def main() -> int:
    filter_id = _filter_component_id()
    if not filter_id:
        print("ERROR: COMPONENT_TEST_COMPONENT_ID is required", file=sys.stderr)
        return 2

    plan_path_raw = os.environ.get("COMPONENT_TEST_PLAN_JSON", "").strip()
    if not plan_path_raw:
        print("ERROR: COMPONENT_TEST_PLAN_JSON is required", file=sys.stderr)
        return 2
    plan_path = Path(plan_path_raw)
    if not plan_path.is_file():
        print(f"ERROR: plan missing: {plan_path}", file=sys.stderr)
        return 2

    tekton_kubeconfig = os.environ.get("KUBECONFIG", "").strip()
    artifacts_dir = _artifacts_dir()
    os.environ.setdefault("ARTIFACTS_DIR", str(artifacts_dir))
    prepare_kubeconfig_auth_for_tests(tekton_kubeconfig_path=tekton_kubeconfig)
    from k8s.vault_runtime import ensure_runtime_vault_env

    ensure_runtime_vault_env()

    component_phases = _plan_component_test_phases(plan_path)
    if not component_phases:
        print("ERROR: no component test phases selected (smoke and/or tier1)", file=sys.stderr)
        return 1

    components = _iter_components_from_plan(plan_path)
    comp = next((c for c in components if c["id"] == filter_id), None)
    if comp is None:
        print(f"SKIP golang {filter_id}: not in component smoke plan", flush=True)
        return 0

    plan_raw, runner_raw = _resolve_runner_from_plan(plan_path, filter_id)

    working_dir = str(runner_raw.get("working_dir", "")).strip()
    results_dir = str(runner_raw.get("results_dir", "")).strip()
    phase_commands_raw = runner_raw.get("phase_commands")
    if not working_dir or not results_dir or not isinstance(phase_commands_raw, dict):
        msg = f"invalid runner block for {filter_id!r} in plan"
        print(f"ERROR: {msg}", file=sys.stderr)
        write_single_failure_junit(
            comp,
            artifacts_dir=artifacts_dir,
            testcase_name="invalid_plan",
            message=msg,
        )
        return 2

    try:
        run_command, env_defaults, source_repo, source_ref = _build_golang_run_context(
            filter_id=filter_id,
            runner_raw=runner_raw,
            component_phases=component_phases,
            plan_raw=plan_raw,
            artifacts_dir=artifacts_dir,
        )
    except ValueError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        write_single_failure_junit(
            comp,
            artifacts_dir=artifacts_dir,
            testcase_name="invalid_context",
            message=str(exc),
        )
        return 2
    if not run_command:
        msg = f"no run command for phases {component_phases!r}"
        print(f"ERROR: {msg}", file=sys.stderr)
        write_single_failure_junit(
            comp,
            artifacts_dir=artifacts_dir,
            testcase_name="no_command",
            message=msg,
        )
        return 2

    by_gate_raw = comp.get("component_test_timeout_by_gate")
    by_gate: dict[str, str] = by_gate_raw if isinstance(by_gate_raw, dict) else {}
    timeout_raw = resolve_component_test_timeout_raw(
        phases=component_phases,
        component_default=comp.get("component_test_timeout", ""),
        component_by_gate=by_gate,
        catalog_gate_defaults=_plan_gate_timeout_defaults(plan_path),
        cli_override=os.environ.get("COMPONENT_TEST_TIMEOUT", "").strip(),
    )
    timeout_raw = apply_cluster_source_timeout_cap(
        component_id=filter_id,
        timeout_raw=timeout_raw,
    )
    try:
        test_timeout_sec = parse_component_timeout_seconds(timeout_raw)
    except ValueError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        write_single_failure_junit(
            comp,
            artifacts_dir=artifacts_dir,
            testcase_name="invalid_timeout",
            message=str(exc),
        )
        return 2

    env_path = component_golang_env_path(artifacts_dir, filter_id)
    skip_path = artifacts_dir / f"component-golang-{filter_id}.skip"

    api_reason = cluster_smoke_infra_blocked_reason()
    if api_reason:
        print(f"FAIL golang {filter_id}: {api_reason}", flush=True)
        return _write_skip_golang_env(
            comp=comp,
            filter_id=filter_id,
            skip_tag="api_unreachable",
            testcase_name="cluster_smoke_infra_blocked",
            message=api_reason,
            artifacts_dir=artifacts_dir,
            env_path=env_path,
            skip_path=skip_path,
            working_dir=working_dir,
            results_dir=results_dir,
            run_command=run_command,
            env_defaults=env_defaults,
            test_timeout_sec=test_timeout_sec or 0.0,
            source_repo=source_repo,
            source_ref=source_ref,
            outcome="failure",
        )

    version_skip = str(comp.get("version_skip_reason") or "").strip()
    if version_skip:
        print(f"SKIP golang {filter_id}: version gate ({version_skip})", flush=True)
        return _write_skip_golang_env(
            comp=comp,
            filter_id=filter_id,
            skip_tag="version",
            testcase_name="version_not_supported",
            message=version_skip,
            artifacts_dir=artifacts_dir,
            env_path=env_path,
            skip_path=skip_path,
            working_dir=working_dir,
            results_dir=results_dir,
            run_command=run_command,
            env_defaults=env_defaults,
            test_timeout_sec=test_timeout_sec,
            source_repo=source_repo,
            source_ref=source_ref,
        )

    rh_ai_apps_reason = (
        openshift_guest_rh_ai_route_tekton_unreachable_reason()
        if filter_id in {"workbench_images", "mlflow"}
        else ""
    )
    if rh_ai_apps_reason:
        print(f"SKIP golang {filter_id}: {rh_ai_apps_reason}", flush=True)
        return _write_skip_golang_env(
            comp=comp,
            filter_id=filter_id,
            skip_tag="rh_ai_route_dns",
            testcase_name="openshift_guest_apps_route_unreachable",
            message=rh_ai_apps_reason,
            artifacts_dir=artifacts_dir,
            env_path=env_path,
            skip_path=skip_path,
            working_dir=working_dir,
            results_dir=results_dir,
            run_command=run_command,
            env_defaults=env_defaults,
            test_timeout_sec=test_timeout_sec,
            source_repo=source_repo,
            source_ref=source_ref,
        )

    if filter_id == "codeflare_sdk":
        from components.codeflare_sdk.kueue_prep import ensure_codeflare_kueue_ready

        try:
            ensure_codeflare_kueue_ready()
        except Exception as exc:
            print(f"SKIP golang {filter_id}: Kueue not ready ({exc})", flush=True)
            return _write_skip_golang_env(
                comp=comp,
                filter_id=filter_id,
                skip_tag="kueue",
                testcase_name="kueue_not_ready",
                message=f"Kueue prerequisites not ready for {filter_id}: {exc}",
                artifacts_dir=artifacts_dir,
                env_path=env_path,
                skip_path=skip_path,
                working_dir=working_dir,
                results_dir=results_dir,
                run_command=run_command,
                env_defaults=env_defaults,
                test_timeout_sec=test_timeout_sec,
                source_repo=source_repo,
                source_ref=source_ref,
            )

    if _truthy_env("FAIL_FAST_DISABLED_COMPONENT"):
        if filter_id and not cluster_prep_already_done(artifacts_dir):
            try:
                prepare_component_for_smoke(filter_id)
            except Exception as exc:
                print(
                    f"WARN: cluster prep for {filter_id} failed ({exc}); continuing prereq probe",
                    file=sys.stderr,
                    flush=True,
                )
        reconcile_baseline_dsc_before_component(filter_id, artifacts_dir)
        unavailable, reason = smoke_component_prereq_unavailable(filter_id)
        if unavailable:
            return _write_skip_golang_env(
                comp=comp,
                filter_id=filter_id,
                skip_tag="prereq",
                testcase_name="component_not_ready",
                message=f"Component not ready for {filter_id}: {reason}",
                artifacts_dir=artifacts_dir,
                env_path=env_path,
                skip_path=skip_path,
                working_dir=working_dir,
                results_dir=results_dir,
                run_command=run_command,
                env_defaults=env_defaults,
                test_timeout_sec=test_timeout_sec,
                source_repo=source_repo,
                source_ref=source_ref,
                outcome=prereq_junit_outcome(reason),
            )

    api_reason = cluster_smoke_infra_blocked_reason()
    if api_reason:
        print(f"FAIL golang {filter_id}: {api_reason}", flush=True)
        return _write_skip_golang_env(
            comp=comp,
            filter_id=filter_id,
            skip_tag="api_unreachable",
            testcase_name="cluster_smoke_infra_blocked",
            message=api_reason,
            artifacts_dir=artifacts_dir,
            env_path=env_path,
            skip_path=skip_path,
            working_dir=working_dir,
            results_dir=results_dir,
            run_command=run_command,
            env_defaults=env_defaults,
            test_timeout_sec=test_timeout_sec or 0.0,
            source_repo=source_repo,
            source_ref=source_ref,
            outcome="failure",
        )

    skip_path.unlink(missing_ok=True)
    if filter_id == "dashboard_cypress":
        stage_cypress_cli_tools()
    elif filter_id == "codeflare_sdk":
        from components.codeflare_sdk.auth import codeflare_env_overrides_from_vault
        from components.codeflare_sdk.dashboard_patch import prepend_codeflare_run_command_patches

        cf_overlay = codeflare_env_overrides_from_vault(artifacts_dir=artifacts_dir)
        dashboard_url = ""
        if cf_overlay:
            env_defaults = dict(env_defaults or {})
            env_defaults.update(cf_overlay)
            auth = cf_overlay.get("CLUSTER_AUTH", "htpasswd")
            print(
                f"codeflare_sdk: {auth} TEST_USER overlay -> "
                f"{cf_overlay.get('TEST_USER_USERNAME', '')}",
                flush=True,
            )
            dashboard_url = cf_overlay.get("ODH_DASHBOARD_URL", "").strip()
            if dashboard_url:
                print(f"codeflare_sdk: dashboard URL -> {dashboard_url}", flush=True)
            if auth == "openshift":
                oc_server = _resolve_oc_server()
                if oc_server:
                    env_defaults["OC_SERVER"] = oc_server
                    print(f"codeflare_sdk: resolved OC_SERVER -> {oc_server}", flush=True)
        run_command = prepend_codeflare_run_command_patches(
            run_command,
            dashboard_url=dashboard_url,
            artifacts_dir=artifacts_dir,
        )
    elif filter_id == "distributed_workloads":
        from components.distributed_workloads.kfto_smoke import prepend_kfto_smoke_patch

        run_command = prepend_kfto_smoke_patch(run_command)
    elif filter_id == "trainer":
        from components.trainer.smoke import prepend_trainer_smoke_patch_if_ephc

        run_command = prepend_trainer_smoke_patch_if_ephc(run_command)
    elif filter_id == "kuberay":
        from components.kuberay.rhoai_images import prepend_kuberay_smoke_patch

        run_command = prepend_kuberay_smoke_patch(run_command)
    elif filter_id == "mlflow":
        from components.mlflow.ephc_tracking import (
            prepend_mlflow_ephc_tracking,
            prepend_mlflow_run_wall_clock,
        )

        run_command = prepend_mlflow_ephc_tracking(run_command)
        if test_timeout_sec:
            run_command = prepend_mlflow_run_wall_clock(run_command, test_timeout_sec)
    elif filter_id == "platform":
        from components.platform.smoke import prepend_platform_smoke_command

        run_command = prepend_platform_smoke_command(run_command)
    _write_env_file(
        env_path,
        skip=False,
        working_dir=working_dir,
        results_dir=results_dir,
        artifact_prefix=comp["artifact_prefix"],
        run_command=run_command,
        env_defaults=env_defaults,
        test_timeout_sec=test_timeout_sec,
        source_repo=source_repo,
        source_ref=source_ref,
    )
    print(
        f"=== golang component {filter_id}: phases={','.join(component_phases)} "
        f"command={redact_command_for_log(run_command)!r} ===",
        flush=True,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
