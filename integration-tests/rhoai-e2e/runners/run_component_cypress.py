#!/usr/bin/env python3
"""Tekton run step for dashboard Cypress (Jenkins dashboard-e2e-tests parity).

Reads ``component-golang.env`` from the orchestrate step, clones odh-dashboard,
applies cluster/runtime prep, runs parallel Cypress tag sets, and emits JUnit.

Env (required):
    ARTIFACTS_DIR
    KUBECONFIG
Env (optional):
    SCRIPTS_REPO_ROOT — rhoai-e2e scripts path on tests-shared workspace
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

from _bootstrap import ensure_rhoai_e2e_path

ensure_rhoai_e2e_path()

from runners.component_junit import write_single_failure_junit
from suite.component_runner_env import component_golang_env_path, load_component_runner_env
from components.dashboard_cypress.config import (
    inject_auth_into_cypress_run_command,
    inject_skip_tags_into_cypress_run_command,
    prepend_cypress_shell_env,
    resolve_oc_token_for_cypress,
)
from components.dashboard_cypress.runtime import (
    collect_cypress_junit,
    dashboard_url_is_local,
    ensure_cypress_cli_packages,
    ensure_google_chrome,
    gateway_cypress_uses_bearer_bypass,
    gateway_use_byoidc_auth,
    validate_gateway_cypress_auth,
    cypress_extra_skip_tags,
    inject_ci_auth_bypass,
    load_component_vault_env,
    patch_gateway_envoyfilter_if_needed,
    patch_runtime_cy_test_config,
    prepend_staged_python_deps,
    prepare_dashboard_worktree,
    run_cypress_shell_command,
    stage_writable_kubeconfig,
    sync_cypress_auth_env_from_config,
    unset_in_cluster_k8s_env,
    verify_dashboard_reachable,
    verify_dashboard_serves_html,
    verify_gateway_stack_healthy,
    gateway_auth_stack_ready,
)
from helpers.gateway_stack_marker import (
    clear_gateway_stack_incomplete_marker,
    gateway_stack_incomplete,
)
from install.kubeconfig_cluster_label import resolve_cypress_cluster_label
from runners.orchestrator import stage_cypress_cli_tools
from suite.component_test_timeout import parse_component_timeout_seconds
from suite.component_task_exit import component_from_plan, resolve_component_exit_codes
from steps.tests_payload import component_test_plan_path, resolve_tests_payload_root, tests_payload_tools_bin_dir


def _artifacts_dir() -> Path:
    raw = os.environ.get("ARTIFACTS_DIR", "/artifacts").strip() or "/artifacts"
    return Path(raw)


def _scripts_repo_root(artifacts_dir: Path) -> Path:
    explicit = os.environ.get("SCRIPTS_REPO_ROOT", "").strip()
    if explicit:
        return Path(explicit)
    return artifacts_dir.parent.parent / "scripts-repo" / "integration-tests" / "rhoai-e2e"


def _apply_runner_exports(runner_env: dict[str, str]) -> None:
    reserved = frozenset(
        {
            "SKIP",
            "WORKING_DIR",
            "RESULTS_DIR",
            "ARTIFACT_PREFIX",
            "RUN_COMMAND",
            "SOURCE_REPO",
            "SOURCE_REF",
            "TEST_TIMEOUT_SEC",
        }
    )
    for key, val in runner_env.items():
        if key in reserved:
            continue
        os.environ.setdefault(key, val)


def _prepend_tools_bin(artifacts_dir: Path) -> None:
    payload_root = resolve_tests_payload_root(artifacts_dir.parent)
    tools_bin = tests_payload_tools_bin_dir(payload_root)
    os.environ["PATH"] = f"{tools_bin}:{os.environ.get('PATH', '')}"


def _gateway_checks_fail_fast() -> bool:
    return os.environ.get("RHCL_GATEWAY_FAIL_FAST", "1").strip().lower() not in (
        "0",
        "false",
        "no",
    )


def _gateway_preflight_issues(
    *,
    auth_ready: bool,
    incomplete: bool,
    healthy: bool,
) -> list[str]:
    """Issues that block Cypress when RHCL_GATEWAY_FAIL_FAST is enabled."""
    issues: list[str] = []
    if not auth_ready:
        issues.append("gateway auth stack not ready (MaaS deps or Authorino TLS)")
    if not healthy:
        issues.append("gateway deployments not fully ready")
    if incomplete and not healthy:
        issues.append(
            "Kuadrant/Authorino stack incomplete (RHCL post-install retry failed)"
        )
    return issues


def _fail_or_warn_gateway(msg: str) -> int | None:
    if _gateway_checks_fail_fast():
        print(f"ERROR: {msg}", file=sys.stderr, flush=True)
        return 2
    print(f"WARN: {msg}", flush=True)
    return None


def _write_early_failure(
    msg: str, *, artifacts_dir: Path, testcase_name: str, artifact_prefix: str = ""
) -> None:
    payload_root = resolve_tests_payload_root(artifacts_dir.parent)
    plan_path = component_test_plan_path(payload_root)
    comp = component_from_plan(plan_path, "dashboard_cypress") if plan_path.is_file() else None
    prefix = (
        artifact_prefix
        or ((comp or {}).get("artifact_prefix") or "").strip()
        or "dashboard-cypress-smoke"
    )
    record = {**(comp or {"id": "dashboard_cypress"}), "artifact_prefix": prefix}
    write_single_failure_junit(
        record,
        artifacts_dir=artifacts_dir,
        testcase_name=testcase_name,
        message=msg,
    )


def main() -> int:
    artifacts_dir = _artifacts_dir()
    os.environ.setdefault("ARTIFACTS", str(artifacts_dir))
    envfile = component_golang_env_path(artifacts_dir, "dashboard_cypress")
    if not envfile.is_file():
        msg = f"missing {envfile} (orchestrate step did not run)"
        print(f"ERROR: {msg}", file=sys.stderr)
        _write_early_failure(msg, artifacts_dir=artifacts_dir, testcase_name="missing_envfile")
        return 2

    runner_env = load_component_runner_env(envfile)
    if runner_env.get("SKIP", "false").lower() == "true":
        print("SKIP cypress tests (orchestrate marked skip)", flush=True)
        return 0

    _prepend_tools_bin(artifacts_dir)
    prepend_staged_python_deps()
    stage_cypress_cli_tools()
    ensure_cypress_cli_packages()
    kubeconfig_src = os.environ.get("KUBECONFIG", "").strip()
    if not kubeconfig_src:
        msg = "KUBECONFIG is required"
        print(f"ERROR: {msg}", file=sys.stderr)
        _write_early_failure(
            msg,
            artifacts_dir=artifacts_dir,
            testcase_name="missing_kubeconfig",
            artifact_prefix=runner_env.get("ARTIFACT_PREFIX", ""),
        )
        return 2
    staged_kubeconfig = stage_writable_kubeconfig(artifacts_dir, kubeconfig_src)
    os.environ["KUBECONFIG"] = str(staged_kubeconfig)
    tools_bin = str(tests_payload_tools_bin_dir(resolve_tests_payload_root(artifacts_dir.parent)))
    _apply_runner_exports(runner_env)

    working_dir = Path(runner_env["WORKING_DIR"])
    results_dir = Path(runner_env["RESULTS_DIR"])
    source_repo = runner_env.get("SOURCE_REPO", "").strip()

    for key, val in load_component_vault_env().items():
        if key == "CY_TEST_CONFIG":
            os.environ[key] = val
        else:
            os.environ.setdefault(key, val)

    dashboard_url_for_label = (
        os.environ.get("ODH_DASHBOARD_URL", "").strip()
        or os.environ.get("BASE_URL", "").strip()
    )
    cluster_label = resolve_cypress_cluster_label(
        staged_kubeconfig,
        cluster_source=os.environ.get("CLUSTER_SOURCE", ""),
        dashboard_url=dashboard_url_for_label,
    )
    if cluster_label:
        print(f"✓ Resolved Cypress cluster label: {cluster_label}", flush=True)

    if source_repo:
        working_dir, results_dir = prepare_dashboard_worktree(
            artifacts_dir=artifacts_dir,
            source_repo=source_repo,
            source_ref=runner_env.get("SOURCE_REF", "main").strip() or "main",
            working_dir_rel=runner_env["WORKING_DIR"],
            results_dir_rel=runner_env["RESULTS_DIR"],
        )

    cy_test_config = os.environ.get("CY_TEST_CONFIG", "").strip()
    dashboard_url_for_patch = (
        os.environ.get("ODH_DASHBOARD_URL", "").strip()
        or os.environ.get("BASE_URL", "").strip()
    )
    if cy_test_config and dashboard_url_for_patch:
        os.environ["CY_TEST_CONFIG"] = patch_runtime_cy_test_config(
            artifacts_dir,
            cy_test_config=cy_test_config,
            odh_dashboard_url=dashboard_url_for_patch,
            cluster_label=cluster_label,
        )
        sync_cypress_auth_env_from_config(os.environ["CY_TEST_CONFIG"])

    unset_in_cluster_k8s_env()
    os.chdir(working_dir)

    odh_dashboard_url = (
        os.environ.get("ODH_DASHBOARD_URL", "").strip()
        or os.environ.get("BASE_URL", "").strip()
    )
    use_bearer_bypass = gateway_cypress_uses_bearer_bypass(
        odh_dashboard_url=odh_dashboard_url
    )
    oc_token = ""
    if use_bearer_bypass:
        if source_repo:
            patch_gateway_envoyfilter_if_needed()
        oc_token = resolve_oc_token_for_cypress()
        if oc_token:
            os.environ["OC_TOKEN"] = oc_token
            os.environ.setdefault("CYPRESS_OC_TOKEN", oc_token)
        if source_repo and oc_token:
            inject_ci_auth_bypass(working_dir)
    else:
        for key in ("OC_TOKEN", "CYPRESS_OC_TOKEN"):
            os.environ.pop(key, None)
        if source_repo and gateway_use_byoidc_auth(odh_dashboard_url=odh_dashboard_url):
            print(
                "Gateway run: skipping CI bearer auth bypass (BYOIDC Keycloak login flow)",
                flush=True,
            )
        elif source_repo:
            print(
                "Gateway run: skipping CI bearer auth bypass (vault/OAuth login)",
                flush=True,
            )

    cy_test_config = os.environ.get("CY_TEST_CONFIG", "").strip()

    def _apply_dashboard_url(url: str) -> None:
        nonlocal odh_dashboard_url, cy_test_config
        odh_dashboard_url = url
        os.environ["ODH_DASHBOARD_URL"] = url
        os.environ["BASE_URL"] = url
        if cy_test_config:
            cy_test_config = patch_runtime_cy_test_config(
                artifacts_dir,
                cy_test_config=cy_test_config,
                odh_dashboard_url=url,
                cluster_label=cluster_label,
            )
            os.environ["CY_TEST_CONFIG"] = cy_test_config
        sync_cypress_auth_env_from_config(cy_test_config)

    if odh_dashboard_url:
        _apply_dashboard_url(odh_dashboard_url)
        login_mode = (
            "bearer auth bypass"
            if use_bearer_bypass
            else "vault/OAuth login"
        )
        print(
            f"Using gateway URL {odh_dashboard_url} ({login_mode})",
            flush=True,
        )

    if not odh_dashboard_url:
        msg = "ODH_DASHBOARD_URL or BASE_URL is not set"
        print(f"ERROR: {msg}", file=sys.stderr)
        _write_early_failure(
            msg,
            artifacts_dir=artifacts_dir,
            testcase_name="missing_url",
            artifact_prefix=runner_env.get("ARTIFACT_PREFIX", ""),
        )
        return 2
    auth_err = validate_gateway_cypress_auth(odh_dashboard_url=odh_dashboard_url)
    if auth_err is not None:
        _write_early_failure(
            "gateway auth validation failed",
            artifacts_dir=artifacts_dir,
            testcase_name="auth_validation_failed",
            artifact_prefix=runner_env.get("ARTIFACT_PREFIX", ""),
        )
        return auth_err
    if not verify_dashboard_reachable(odh_dashboard_url):
        msg = f"dashboard not reachable at {odh_dashboard_url}"
        print(f"ERROR: {msg}", file=sys.stderr)
        _write_early_failure(
            msg,
            artifacts_dir=artifacts_dir,
            testcase_name="unreachable",
            artifact_prefix=runner_env.get("ARTIFACT_PREFIX", ""),
        )
        return 2
    if not verify_dashboard_serves_html(odh_dashboard_url):
        msg = (
            f"dashboard did not serve text/html at {odh_dashboard_url} "
            "(gateway text/plain breaks cy.visit; wait for Authorino/OAP recovery)"
        )
        print(f"ERROR: {msg}", file=sys.stderr)
        _write_early_failure(
            msg,
            artifacts_dir=artifacts_dir,
            testcase_name="not_html",
            artifact_prefix=runner_env.get("ARTIFACT_PREFIX", ""),
        )
        return 2

    gateway_healthy = verify_gateway_stack_healthy()
    # RHCL may leave an incomplete marker during install even when deployments recover.
    if gateway_stack_incomplete() and gateway_healthy:
        clear_gateway_stack_incomplete_marker()
        print(
            "WARN: cleared stale gateway incomplete marker (deployments ready, dashboard reachable)",
            flush=True,
        )

    gateway_issues = _gateway_preflight_issues(
        auth_ready=gateway_auth_stack_ready(),
        incomplete=gateway_stack_incomplete(),
        healthy=gateway_healthy,
    )

    if gateway_issues:
        ec = _fail_or_warn_gateway(
            "; ".join(gateway_issues)
            + " (set RHCL_GATEWAY_FAIL_FAST=0 for WARN-only and continue Cypress)"
        )
        if ec is not None:
            return ec

    try:
        ensure_google_chrome()
    except RuntimeError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        _write_early_failure(
            str(exc),
            artifacts_dir=artifacts_dir,
            testcase_name="missing_chrome",
            artifact_prefix=runner_env.get("ARTIFACT_PREFIX", ""),
        )
        return 2

    timeout_sec: float | None = None
    timeout_raw = runner_env.get("TEST_TIMEOUT_SEC", "").strip()
    if timeout_raw:
        try:
            timeout_sec = parse_component_timeout_seconds(timeout_raw)
        except ValueError as exc:
            print(f"ERROR: {exc}", file=sys.stderr)
            _write_early_failure(
                str(exc),
                artifacts_dir=artifacts_dir,
                testcase_name="invalid_timeout",
                artifact_prefix=runner_env.get("ARTIFACT_PREFIX", ""),
            )
            return 2

    extra_skip = cypress_extra_skip_tags(odh_dashboard_url=odh_dashboard_url)
    if extra_skip:
        print(f"✓ Expanding Cypress skipTags: {extra_skip}", flush=True)
    run_command = inject_skip_tags_into_cypress_run_command(
        runner_env["RUN_COMMAND"],
        extra_skip,
    )
    run_command = inject_auth_into_cypress_run_command(run_command)
    os.environ.setdefault("ARTIFACT_PREFIX", runner_env["ARTIFACT_PREFIX"])
    scripts_repo = os.environ.get("SCRIPTS_REPO_ROOT", "").strip()
    if scripts_repo:
        os.environ.setdefault("SCRIPTS_REPO_ROOT", scripts_repo)
    run_command = prepend_cypress_shell_env(
        run_command,
        tools_bin=tools_bin,
        kubeconfig=str(staged_kubeconfig),
    )
    try:
        exit_code = run_cypress_shell_command(
            run_command,
            test_timeout_sec=timeout_sec,
        )
    finally:
        collect_cypress_junit(
            artifacts_dir=artifacts_dir,
            artifact_prefix=runner_env["ARTIFACT_PREFIX"],
            results_dir=results_dir,
            results_subdirs=os.environ.get("CYPRESS_RESULTS_SUBDIRS", ""),
        )
    payload_root = resolve_tests_payload_root(artifacts_dir.parent)
    plan_path = component_test_plan_path(payload_root)
    exit_path = artifacts_dir / "component-test.exit"
    if plan_path.is_file():
        comp = component_from_plan(plan_path, "dashboard_cypress")
        if comp is not None:
            strict_ec, tekton_ec = resolve_component_exit_codes(
                comp,
                raw_ec=exit_code,
                artifacts_dir=artifacts_dir,
            )
            exit_path.write_text(str(strict_ec), encoding="ascii")
            return tekton_ec
    exit_path.write_text(str(exit_code), encoding="ascii")
    return exit_code


if __name__ == "__main__":
    repo = _scripts_repo_root(_artifacts_dir())
    if repo.is_dir():
        os.chdir(repo)
    raise SystemExit(main())
