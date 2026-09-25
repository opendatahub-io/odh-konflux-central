#!/usr/bin/env python3
"""
Tekton step: read TESTS / COMPONENTS params + YAML catalogs, write RUN_* result files.

Invoked from parse-pipeline-tests after SCRIPTS_REPO is cloned to REPO_ROOT (e.g. /tmp/repo).
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

from _bootstrap import ensure_rhoai_e2e_path

ensure_rhoai_e2e_path()

from suite.component_catalog import (
    load_components_smoke_catalog,
    merged_setup_dependencies_args,
    resolve_shift_left_env_secret,
)
from suite.component_plan import parse_components_selection, resolve_components_csv
from suite.component_smoke_flag_refresh import parse_pipeline_run_smoke_result_ids
from suite.component_smoke_results import component_smoke_result_name
from suite.constants import DEFAULT_SETUP_DEPENDENCIES_ARGS, is_test_only_product, product_installs_operator
from install.dsc_install import components_need_models_as_service
from suite.errors import AppError
from suite.tests_config import compute_pipeline_result_flags, load_tests_catalog
from suite.tests_plan import parse_tests_selection, validate_and_normalize_tests_csv

_DISTRIBUTED_WORKLOADS_COMPONENTS = frozenset({"trainer", "distributed_workloads"})

try:
    import yaml
except ImportError:
    yaml = None  # type: ignore[assignment]

# Extra Tekton results (not driven by rhoai-e2e-tests-config.yaml phase entries).
EXTRA_RESULT_KEYS = (
    "RUN_OPENDATAHUB_TESTS",
    "RUN_MINIMAL_DEPS",
    "RUN_INSTALL_DEP_OPERATORS",
    "RUN_COMPONENT_TESTS",
    "RUN_COMPONENT_CLUSTER_PREP_IN_DEP_OPERATORS",
    "RUN_BVT_PLACEHOLDER_ONLY",
    "RUN_DISTRIBUTED_WORKLOADS_TESTS",
)


def _snapshot_only_no_cluster(*, product: str, cluster_source: str) -> bool:
    """True when test-only PRODUCT with no external kubeconfig (placeholder BVT / no smoke)."""
    prod = (product or "").strip().lower()
    source = (cluster_source or "").strip()
    if product_installs_operator(prod):
        return False
    return not source

DEFAULT_SMOKE_AWS_SECRET = "unused-smoke-aws-secret"


def _write_bool_tekton_result(
    *,
    path_var: str,
    value: bool,
    results_base: Path,
    result_label: str = "",
    required: bool = True,
) -> int:
    p = os.environ.get(path_var, "").strip()
    if not p:
        if not required:
            return 0
        suffix = f" for result {result_label}" if result_label else ""
        print(f"Missing env {path_var}{suffix}", file=sys.stderr)
        return 1
    result_path = Path(p).resolve()
    if not result_path.is_relative_to(results_base):
        print(
            f"ERROR: {path_var}={p!r} resolves outside allowed results directory {results_base}",
            file=sys.stderr,
        )
        return 1
    try:
        result_path.parent.mkdir(parents=True, exist_ok=True)
        result_path.write_text("true" if value else "false", encoding="utf-8")
    except (FileNotFoundError, PermissionError, OSError) as exc:
        print(f"ERROR: could not write result file {path_var}={p!r}: {exc}", file=sys.stderr)
        return 1
    return 0


def _write_workspace_text(env_name: str, content: str, *, required: bool = True) -> int:
    workspace = os.environ.get(env_name, "").strip()
    if not workspace:
        if not required:
            return 0
        print(f"Missing env {env_name} for parse run-config", file=sys.stderr)
        return 1
    path = Path(workspace)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    return 0


def _log_workspace_only_tekton_result(result_path_env: str, label: str) -> None:
    if os.environ.get(result_path_env, "").strip():
        print(
            f"INFO: {label} written to workspace only; emit-parse-artifacts publishes Tekton result",
            flush=True,
        )


def _write_component_smoke_results(
    *,
    catalog_component_ids: tuple[str, ...],
    selected_ids: frozenset[str],
    run_component_tests: bool,
    results_base: Path,
) -> int:
    """Write RUN_SMOKE_<id> true/false for each catalog component."""
    for cid in catalog_component_ids:
        key = component_smoke_result_name(cid)
        selected = run_component_tests and cid in selected_ids
        if _write_bool_tekton_result(
            path_var=f"{key}_PATH",
            value=selected,
            results_base=results_base,
            required=False,
        ):
            return 1
    return 0


def main() -> int:
    tests_raw = os.environ.get("TEST_GATES", os.environ.get("TESTS", "")).strip()
    components_raw = os.environ.get("COMPONENTS", "").strip()
    repo_root = os.environ.get("REPO_ROOT", "").strip()
    if not repo_root:
        print("REPO_ROOT is required (clone destination of SCRIPTS_REPO).", file=sys.stderr)
        return 1
    root = Path(repo_root)
    cfg = root / "integration-tests" / "rhoai-e2e" / "config" / "rhoai-e2e-tests-config.yaml"
    comp_cfg = root / "integration-tests" / "rhoai-e2e" / "config" / "rhoai-e2e-components-smoke.yaml"
    smoke_aws_secret = DEFAULT_SMOKE_AWS_SECRET

    try:
        catalog = load_tests_catalog(cfg)
        csv = validate_and_normalize_tests_csv(tests_raw or None, catalog)
        selected = parse_tests_selection(csv, catalog)
        flags = compute_pipeline_result_flags(selected, catalog)

        components_csv = ""
        setup_deps_args = ""
        needs_smoke_maas_deps = False
        product = os.environ.get("PRODUCT", "").strip().lower()
        cluster_source = os.environ.get("CLUSTER_SOURCE", "").strip()
        snapshot_only = _snapshot_only_no_cluster(product=product, cluster_source=cluster_source)
        installs_product = product_installs_operator(product)
        install_dependencies = os.environ.get("INSTALL_DEPENDENCIES", "").strip().lower() in (
            "1",
            "true",
            "yes",
        )
        comp_catalog = load_components_smoke_catalog(comp_cfg)
        selected_component_ids: frozenset[str] = frozenset()

        if selected & {"smoke", "tier1"}:
            components_csv = resolve_components_csv(
                components_raw or None,
                tests_catalog=catalog,
                tests_selected=selected,
                components_catalog=comp_catalog,
            )
            selected_component_ids = parse_components_selection(components_csv, comp_catalog)
            needs_smoke_maas_deps = components_need_models_as_service(selected_component_ids)
            merged = merged_setup_dependencies_args(selected_component_ids, comp_catalog)
            if installs_product:
                setup_deps_args = merged or DEFAULT_SETUP_DEPENDENCIES_ARGS
            elif install_dependencies:
                setup_deps_args = merged or (
                    DEFAULT_SETUP_DEPENDENCIES_ARGS if needs_smoke_maas_deps else ""
                )
            elif needs_smoke_maas_deps:
                setup_deps_args = merged or DEFAULT_SETUP_DEPENDENCIES_ARGS
        elif installs_product:
            setup_deps_args = DEFAULT_SETUP_DEPENDENCIES_ARGS

        run_component_tests = flags.get("RUN_SMOKE", False) or flags.get("RUN_TIER1", False)
        if snapshot_only:
            if run_component_tests:
                print(
                    "INFO snapshot-only (test-only PRODUCT, no CLUSTER_SOURCE): "
                    "smoke/tier1 disabled — pass --external-kubeconfig for component tests",
                    flush=True,
                )
            run_component_tests = False
            flags["RUN_SMOKE"] = False
            flags["RUN_TIER1"] = False
            setup_deps_args = ""
            components_csv = ""
            selected_component_ids = frozenset()
            needs_smoke_maas_deps = False

        run_dep_operators = installs_product or (
            run_component_tests and (install_dependencies or needs_smoke_maas_deps)
        )
        flags["RUN_MINIMAL_DEPS"] = run_dep_operators
        flags["RUN_INSTALL_DEP_OPERATORS"] = run_dep_operators

        flags["RUN_COMPONENT_CLUSTER_PREP_IN_DEP_OPERATORS"] = (
            run_component_tests
            and (install_dependencies or needs_smoke_maas_deps)
            and not installs_product
        )
        flags["RUN_COMPONENT_TESTS"] = run_component_tests
        flags["RUN_OPENDATAHUB_TESTS"] = flags.get("RUN_BVT", False) or run_component_tests
        flags["RUN_BVT_PLACEHOLDER_ONLY"] = snapshot_only and bool(flags.get("RUN_BVT", False))
        flags["RUN_DISTRIBUTED_WORKLOADS_TESTS"] = run_component_tests and bool(
            selected_component_ids & _DISTRIBUTED_WORKLOADS_COMPONENTS
        )

        smoke_aws_secret = resolve_shift_left_env_secret(
            comp_catalog,
            selected_ids=selected_component_ids,
            explicit="",
        ) or DEFAULT_SMOKE_AWS_SECRET

    except AppError as exc:
        print(
            f"ERROR: tests config or selection failed (fix YAML/CSV or paths): {exc}",
            file=sys.stderr,
        )
        return 1
    except FileNotFoundError as exc:
        print(
            f"ERROR: file not found — verify REPO_ROOT={repo_root!r} and that the repo contains "
            f"integration-tests/rhoai-e2e/config/*.yaml: {exc}",
            file=sys.stderr,
        )
        return 1
    except PermissionError as exc:
        print(
            f"ERROR: permission denied reading config under REPO_ROOT={repo_root!r}: {exc}",
            file=sys.stderr,
        )
        return 1
    except Exception as exc:
        if yaml is not None and isinstance(exc, yaml.YAMLError):
            print(
                f"ERROR: invalid YAML in tests config ({cfg}): {exc}. Fix indentation/quoting in the file.",
                file=sys.stderr,
            )
            return 1
        raise

    print(
        f"TEST_GATES={csv!r} COMPONENTS={components_csv!r} selection={sorted(selected)} -> {flags}",
        flush=True,
    )
    results_base = Path(os.environ.get("RESULTS_DIR", "/tekton/results")).resolve()
    all_keys = set(flags) | set(EXTRA_RESULT_KEYS)
    for key in sorted(all_keys):
        if _write_bool_tekton_result(
            path_var=f"{key}_PATH",
            value=bool(flags.get(key, False)),
            results_base=results_base,
            result_label=key,
        ):
            return 1

    smoke_result_ids = parse_pipeline_run_smoke_result_ids(comp_catalog.component_ids)
    if _write_component_smoke_results(
        catalog_component_ids=smoke_result_ids,
        selected_ids=selected_component_ids,
        run_component_tests=bool(flags.get("RUN_COMPONENT_TESTS")),
        results_base=results_base,
    ):
        return 1

    if _write_workspace_text("COMPONENTS_CSV_WORKSPACE", components_csv):
        return 1
    _log_workspace_only_tekton_result("COMPONENTS_CSV_PATH", "COMPONENTS_CSV")

    if _write_workspace_text("SETUP_DEPENDENCIES_ARGS_WORKSPACE", setup_deps_args):
        return 1
    _log_workspace_only_tekton_result("SETUP_DEPENDENCIES_ARGS_PATH", "SETUP_DEPENDENCIES_ARGS")

    run_component_tests = bool(flags.get("RUN_COMPONENT_TESTS"))
    if _write_workspace_text(
        "SMOKE_AWS_SECRET_WORKSPACE",
        smoke_aws_secret,
        required=run_component_tests,
    ):
        return 1
    _log_workspace_only_tekton_result("SMOKE_AWS_SECRET_PATH", "SMOKE_AWS_SECRET")

    secret_source = (os.environ.get("SECRET_SOURCE") or "vault").strip().lower()
    if secret_source not in ("vault", "tenant"):
        secret_source = "vault"
    if _write_workspace_text("SECRET_SOURCE_WORKSPACE", secret_source, required=False):
        return 1

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
