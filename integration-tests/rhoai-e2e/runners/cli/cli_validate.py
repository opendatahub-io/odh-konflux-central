"""Cross-flag validation for ``rhoai_e2e.py`` CLI arguments."""

from __future__ import annotations

import argparse
import os
import re
from pathlib import Path

from .cli_parser import CliArgumentParser, _KA_HOST_FROM_ENV
from suite.component_catalog import default_components_smoke_config_path, load_components_smoke_catalog
from suite.component_plan import validate_and_normalize_components_csv
from suite.constants import default_tests_config_path
from suite.constants import is_test_only_product, product_installs_operator
from suite.errors import AppError
from suite.its_registry import (
    integration_test_scenario_application,
    integration_test_scenario_default_konflux_app,
    resolve_integration_test_scenario_ref,
    resolve_integration_test_scenario_run_its_snapshot,
)
from suite.tests_config import load_tests_catalog
from suite.tests_plan import (
    parse_tests_selection,
    validate_and_normalize_tests_csv_cli,
)
from suite.trigger_param_registry import apply_trigger_param_resolution

_DURATION_TOKEN_RE = re.compile(r"(?i)(\d+(?:\.\d+)?)([smhd])")


def _normalize_test_timeout(raw: str) -> str:
    """Normalize duration text (e.g. ``10m``, ``1h30m``); return empty for unset."""
    s = (raw or "").strip()
    if not s:
        return ""
    compact = s.replace(" ", "")
    if re.fullmatch(r"\d+(?:\.\d+)?", compact):
        return f"{compact}s"
    pos = 0
    out_parts: list[str] = []
    has_positive = False
    for m in _DURATION_TOKEN_RE.finditer(compact):
        if m.start() != pos:
            raise AppError(
                "--test-timeout must be a duration like 10m, 90s, 1h30m, 1.5h, or plain seconds.",
                2,
            )
        num = m.group(1)
        unit = m.group(2).lower()
        if float(num) > 0:
            has_positive = True
        out_parts.append(f"{num}{unit}")
        pos = m.end()
    if pos != len(compact) or not out_parts:
        raise AppError(
            "--test-timeout must be a duration like 10m, 90s, 1h30m, 1.5h, or plain seconds.",
            2,
        )
    if not has_positive:
        raise AppError("--test-timeout must be greater than zero.", 2)
    return "".join(out_parts)


def _apply_rhoai_version_product_default(
    args: argparse.Namespace, *, product_explicit: bool
) -> None:
    """``--rhoai-version`` implies ``--product rhoai`` unless another product was set explicitly."""
    if not (args.version or "").strip():
        return
    if args.product == "rhoai":
        return
    if product_explicit:
        raise AppError(
            "--rhoai-version is supported only with --product rhoai (or omit --product).",
            2,
        )
    args.product = "rhoai"


_ENABLE_ITS_ALLOWED_TRIGGER_FLAGS = frozenset({"--konflux-repo", "--konflux-branch"})


def _trigger_options_incompatible_with_query(
    args: argparse.Namespace, *, list_ocp_on: bool = False, list_components_on: bool = False
) -> list[str]:
    """Trigger/install flags that cannot be combined with query/maintenance modes."""
    checks: list[tuple[bool, str]] = [
        (bool(args.image), "--image"),
        (bool(args.version), "--rhoai-version"),
        (bool(args.channel), "--rhoai-channel"),
        (bool(args.konflux_repo), "--konflux-repo"),
        (bool(args.konflux_branch), "--konflux-branch"),
        (getattr(args, "quay_pull_secret_explicit", False), "--quay-pull-secret-name"),
        (bool(args.ocp_version) and not list_ocp_on, "--ocp-version"),
        (bool((getattr(args, "ocp_channel", "") or "").strip()), "--ocp-channel"),
        (getattr(args, "tests_explicit", False) and not list_components_on, "--tests"),
        (bool((args.tests_config or "").strip()), "--tests-config"),
        (getattr(args, "components_explicit", False) and not list_components_on, "--components"),
        (getattr(args, "test_timeout_explicit", False), "--test-timeout"),
        (bool(args.external_kubeconfig), "--external-kubeconfig"),
        (bool(args.external_kubeconfig_secret), "--external-kubeconfig-secret"),
        (getattr(args, "cleanup_opt_out", False), "--cleanup false"),
        (getattr(args, "install_dependencies", False), "--install-dependencies"),
        (bool((args.tests_rhoai_version or "").strip()), "--tests-rhoai-version"),
        (bool(args.slack_channel_id), "--slack-channel-id"),
        (getattr(args, "product_explicit", False), "--product"),
    ]
    return [flag for active, flag in checks if active]


def _enable_its_forbidden_flags(args: argparse.Namespace) -> list[str]:
    """Cluster/test/install flags rejected with --enable-its (Konflux rollout only)."""
    return [
        flag
        for flag in _trigger_options_incompatible_with_query(args)
        if flag not in _ENABLE_ITS_ALLOWED_TRIGGER_FLAGS
    ]


def _filter_trigger_flags_for_its_admin(
    flags: list[str], *, enable_its: bool, run_its: bool
) -> list[str]:
    """Relax incompatible-flag checks for ITS admin modes."""
    if run_its:
        return []
    if enable_its:
        return [flag for flag in flags if flag not in _ENABLE_ITS_ALLOWED_TRIGGER_FLAGS]
    return flags


def parse_cli_args(parser: CliArgumentParser, argv: list[str]) -> argparse.Namespace:
    tests_explicit = any(x == "--tests" or x.startswith("--tests=") for x in argv)
    components_explicit = any(x == "--components" or x.startswith("--components=") for x in argv)
    test_timeout_explicit = any(x == "--test-timeout" or x.startswith("--test-timeout=") for x in argv)
    product_explicit = any(x == "--product" or x.startswith("--product=") for x in argv)
    quay_pull_secret_explicit = any(
        x == "--quay-pull-secret-name" or x.startswith("--quay-pull-secret-name=") for x in argv
    )
    konflux_app_explicit = any(
        x == "--konflux-app" or x.startswith("--konflux-app=") for x in argv
    )
    cleanup_argv = any(x == "--cleanup" or x.startswith("--cleanup=") for x in argv)
    args = parser.parse_args(argv)
    args.quay_pull_secret_explicit = quay_pull_secret_explicit
    if args.cleanup is None:
        args.cleanup = False
    args.cleanup_maintenance = cleanup_argv and bool(args.cleanup)
    args.cleanup_opt_out = cleanup_argv and not args.cleanup
    args.cleanup_explicit = args.cleanup_opt_out

    secret_source = (getattr(args, "secret_source", "") or "").strip().lower() or "vault"
    if secret_source not in ("vault", "tenant"):
        raise AppError(
            f"invalid --secret-source / OLMINSTALL_SECRET_SOURCE={secret_source!r}; "
            "must be vault or tenant",
            2,
        )
    args.secret_source = secret_source

    _apply_rhoai_version_product_default(args, product_explicit=product_explicit)
    if (args.version or "").strip() and not re.match(r"^\d+\.\d+", args.version.strip()):
        raise AppError(
            "--rhoai-version must start with MAJOR.MINOR (e.g. 3.5 or 3.5-ea.2), "
            f"not {args.version.strip()!r}.",
            2,
        )
    if getattr(args, "install_dependencies", False) and product_installs_operator(args.product):
        raise AppError("--install-dependencies requires test-only mode (omit --product)", 2)
    if args.ocp_version:
        if not re.fullmatch(r"\d+\.\d+", args.ocp_version.strip()):
            raise AppError("--ocp-version must be MAJOR.MINOR (e.g. 4.20)", 2)
        args.ocp_version = args.ocp_version.strip()
    if args.ka_host == _KA_HOST_FROM_ENV:
        args.ka_host = os.environ.get("KA_HOST", "")
        if not args.ka_host:
            raise AppError(
                "--ka-host with no URL requires KA_HOST in the environment, or pass "
                "--ka-host https://<kubearchive-host> (see README). "
                "Without KubeArchive, only PipelineRuns still on the apiserver are listed.",
                2,
            )
    if args.konflux_ui and not args.konflux_ui.startswith("https://"):
        raise AppError("--konflux-ui must use https://", 2)
    if args.ka_host and not args.ka_host.startswith("https://"):
        raise AppError("--ka-host must use https://", 2)
    if args.konflux_server and not args.konflux_server.startswith("https://"):
        raise AppError("--konflux-server must use https://", 2)

    cfg_arg = (args.tests_config or "").strip()
    cfg_path = Path(cfg_arg).expanduser().resolve() if cfg_arg else default_tests_config_path()
    catalog = load_tests_catalog(cfg_path)
    args.tests_catalog_default_csv = catalog.default_csv
    comp_cfg_path = default_components_smoke_config_path()
    comp_catalog = load_components_smoke_catalog(comp_cfg_path)
    args.components_catalog = comp_catalog
    args.components_catalog_default_csv = comp_catalog.enabled_components_csv

    if tests_explicit or (args.tests or "").strip():
        phases_csv, test_tags, scoped_components, auto_scope_tags = (
            validate_and_normalize_tests_csv_cli(
                args.tests,
                catalog,
                components_catalog=comp_catalog,
            )
        )
        args.tests = phases_csv
        args.test_tags = test_tags
        args.test_slice_scoped_components = scoped_components
        args.test_tags_inferred = auto_scope_tags and bool(test_tags)
    else:
        args.tests = catalog.default_csv
        args.test_tags = ""
        args.test_slice_scoped_components = ()
        args.test_tags_inferred = False
    args.tests_explicit = tests_explicit

    if args.test_tags_inferred and not components_explicit:
        args.components = ",".join(args.test_slice_scoped_components)
        args.components_inferred = True
    else:
        args.components_inferred = False

    selected_tests = parse_tests_selection(args.tests, catalog)
    args.components = validate_and_normalize_components_csv(
        args.components,
        tests_csv=args.tests,
        components_catalog=comp_catalog,
    )
    args.components_explicit = components_explicit
    if args.test_tags:
        selected = {c.strip() for c in args.components.split(",") if c.strip()}
        missing = set(args.test_slice_scoped_components) - selected
        if missing:
            need = ", ".join(sorted(missing))
            raise AppError(
                f"--tests test-slice tokens require component(s): {need} "
                "(pass --components or rely on auto-scope).",
                2,
            )
    if components_explicit and "smoke" not in selected_tests:
        raise AppError("--components is only valid when --tests includes smoke.", 2)
    if getattr(args, "install_dependencies", False) and not (selected_tests & {"smoke", "tier1"}):
        raise AppError("--install-dependencies requires --tests smoke and/or tier1.", 2)
    if getattr(args, "install_dependencies", False):
        if not args.external_kubeconfig and not args.external_kubeconfig_secret:
            raise AppError(
                "--install-dependencies requires --external-kubeconfig or --external-kubeconfig-secret.",
                2,
            )

    if args.enable_its and (forbidden := _enable_its_forbidden_flags(args)):
        joined = ", ".join(forbidden)
        raise AppError(
            f"--enable-its accepts only Konflux rollout flags (--konflux-repo, "
            f"--konflux-branch, --konflux-app); not allowed: {joined}. "
            "Use --run-its for one-shot debug runs with cluster/test overrides.",
            2,
        )

    args.external_kubeconfig = (args.external_kubeconfig or "").strip()
    args.external_kubeconfig_context = (getattr(args, "external_kubeconfig_context", "") or "").strip()
    args.external_kubeconfig_secret = (args.external_kubeconfig_secret or "").strip()
    if args.external_kubeconfig and args.external_kubeconfig_secret:
        raise AppError(
            "--external-kubeconfig and --external-kubeconfig-secret are mutually exclusive.",
            2,
        )
    if args.external_kubeconfig_context and not args.external_kubeconfig:
        raise AppError("--external-kubeconfig-context requires --external-kubeconfig.", 2)
    if args.external_kubeconfig:
        from k8s.external_kubeconfig import validate_kubeconfig_path

        args.external_kubeconfig_path = validate_kubeconfig_path(args.external_kubeconfig)
    else:
        args.external_kubeconfig_path = None
    if args.external_kubeconfig_secret and not re.fullmatch(
        r"[a-z0-9]([-a-z0-9]*[a-z0-9])?", args.external_kubeconfig_secret
    ):
        raise AppError(
            "--external-kubeconfig-secret must be a valid Kubernetes resource name.",
            2,
        )
    args.test_timeout = _normalize_test_timeout(args.test_timeout)
    args.test_timeout_explicit = test_timeout_explicit
    if args.test_timeout and "smoke" not in selected_tests:
        raise AppError("--test-timeout is only valid when --tests includes smoke.", 2)

    if args.list_pipelines is not None:
        try:
            lp = int(args.list_pipelines)
            if lp <= 0:
                raise ValueError
            args.list_pipelines = lp
        except ValueError as exc:
            raise AppError(f"-l expects a positive integer (got: {args.list_pipelines})", 2) from exc
    else:
        args.list_pipelines = 0

    args.enable_its = (getattr(args, "enable_its", "") or "").strip()
    args.disable_its = (getattr(args, "disable_its", "") or "").strip()
    args.run_its = (getattr(args, "run_its", "") or "").strip()
    args.konflux_app_explicit = konflux_app_explicit
    args.product_explicit = product_explicit
    if args.enable_its and args.disable_its:
        raise AppError("--enable-its and --disable-its are mutually exclusive.", 2)
    if args.enable_its and args.run_its:
        raise AppError("--enable-its and --run-its are mutually exclusive.", 2)
    if args.run_its and args.disable_its:
        raise AppError("--run-its cannot be used with --disable-its.", 2)
    if args.enable_its and getattr(args, "force_cluster_run", False):
        raise AppError(
            "--force-cluster-run cannot be used with --enable-its; use --run-its for debug runs.",
            2,
        )
    its_admin_on = bool(args.enable_its or args.disable_its or args.run_its)
    if its_admin_on:
        its_ref = args.enable_its or args.disable_its or args.run_its
        rhoai_e2e_root = Path(__file__).resolve().parent.parent.parent
        manifest_path, scenario_name = resolve_integration_test_scenario_ref(rhoai_e2e_root, its_ref)
        args.its_manifest_path = manifest_path
        args.its_scenario_name = scenario_name
        if args.run_its:
            snap_path = resolve_integration_test_scenario_run_its_snapshot(
                rhoai_e2e_root, scenario_name
            )
            if snap_path is not None:
                args.run_its_snapshot_path = snap_path
        if not konflux_app_explicit:
            mapped_app = integration_test_scenario_default_konflux_app(scenario_name)
            if mapped_app:
                args.app = mapped_app
            else:
                manifest_app = integration_test_scenario_application(manifest_path)
                if manifest_app:
                    args.app = manifest_app

    list_pipelines_on = bool(args.list_pipelines)
    list_ocp_on = bool(args.list_supported_ocp)
    list_components_on = bool(getattr(args, "list_components", False))
    watch_on = args.watch is not None
    delete_pipelines_on = bool(args.delete_pending_pipelines)
    cleanup_maintenance_on = bool(getattr(args, "cleanup_maintenance", False))
    query_modes = sum(
        [
            list_pipelines_on,
            list_ocp_on,
            list_components_on,
            watch_on,
            delete_pipelines_on,
            its_admin_on,
            cleanup_maintenance_on,
        ]
    )
    if query_modes > 1:
        raise AppError(
            "-l, -w, --delete-pending-pipelines, --cleanup, --list-supported-ocp, "
            "--list-components, --enable-its, --disable-its, and --run-its are mutually exclusive "
            "(pick one query/maintenance mode).",
            2,
        )

    if (args.external_kubeconfig or args.external_kubeconfig_secret) and args.ocp_version and not list_ocp_on:
        if is_test_only_product(args.product):
            raise AppError(
                "--ocp-version with --external-kubeconfig is only for --product rhoai install; "
                "test-only runs skip FBC catalog resolution.",
                2,
            )

    if query_modes and (
        bad := _filter_trigger_flags_for_its_admin(
            _trigger_options_incompatible_with_query(
                args, list_ocp_on=list_ocp_on, list_components_on=list_components_on
            ),
            enable_its=bool(args.enable_its),
            run_its=bool(args.run_its),
        )
    ):
        joined = ", ".join(bad)
        raise AppError(
            f"Trigger/install options cannot be used with -l, --list-supported-ocp, "
            f"-w, --delete-pending-pipelines, --cleanup, --enable-its, --disable-its, --list-components, or "
        f"--run-its: {joined}. "
        "Use only Konflux context flags (e.g. --konflux-namespace, --konflux-app, --ka-host, "
        "--konflux-ui; with --enable-its you may add "
        "--konflux-repo / --konflux-branch / --konflux-app) for list/watch/delete/ITS admin/catalog query; "
        "use --run-its for one-shot debug runs with cluster/test overrides; "
        "with --list-supported-ocp you may add --ocp-version to verify it appears in the "
        "supported list.",
            2,
        )

    if getattr(args, "stop_owned_running", False) and not delete_pipelines_on:
        raise AppError("--stop-owned-running requires --delete-pending-pipelines.", 2)
    if getattr(args, "include_unowned_stuck", False) and not delete_pipelines_on:
        raise AppError("--include-unowned-stuck requires --delete-pending-pipelines.", 2)
    if getattr(args, "dry_run", False) and not delete_pipelines_on:
        raise AppError("--dry-run requires --delete-pending-pipelines.", 2)

    if cleanup_maintenance_on:
        if not args.external_kubeconfig and not args.external_kubeconfig_secret:
            raise AppError(
                "--cleanup requires --external-kubeconfig or --external-kubeconfig-secret.",
                2,
            )

    if not query_modes:
        its_path = getattr(args, "its_manifest_path", None)
        apply_trigger_param_resolution(args, its_manifest_path=its_path)
        if getattr(args, "cleanup_opt_out", False) and not args.external_kubeconfig_path and not args.external_kubeconfig_secret:
            raise AppError(
                "--cleanup false requires --external-kubeconfig or --external-kubeconfig-secret.",
                2,
            )

    args.watch_mode = watch_on
    return args
