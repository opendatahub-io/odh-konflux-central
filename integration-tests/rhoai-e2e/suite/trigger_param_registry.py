"""Resolve rhoai-e2e trigger-layer pipeline params from CLI / --run-its / ITS.

Precedence: explicit CLI > staged ITS > infer (per-param rules below) > default.

Each entry in ``TRIGGER_PARAMS`` is the single place to edit a param.
"""

from __future__ import annotations

import argparse
import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field, replace
from functools import lru_cache
from pathlib import Path
from typing import Literal

from suite.constants import (
    DEFAULT_QUAY_PULL_SECRET_NAME,
    is_test_only_product,
    ITS_TEST_GATES_PARAM_DEFAULT,
)
from suite.its_registry import its_manifest_param
from suite.its_trigger_params import (
    ocp_install_prefix,
    resolve_cluster_source_for_trigger,
    resolve_version_display_params,
)

PatchPolicy = Literal["always", "when_non_empty", "override_only"]


def infer_cleanup_param(*, product: str, rhoai_version: str = "") -> str:
    """Default ``CLEANUP`` when CLI/ITS did not set it (see ``TRIGGER_PARAMS`` / pipeline ``when:``)."""
    if is_test_only_product(product):
        return "false"
    if (rhoai_version or "").strip():
        return "true"
    if (product or "").strip().lower() in ("rhoai", "odh"):
        return "true"
    return "false"

_LEGACY_ITS_PARAM_ALIASES = frozenset(
    {"FBCF_COMPONENT_NAME", "FBCF_IMAGE_DISPLAY", "UPDATE_CHANNEL_DISPLAY", "TESTS"}
)


@dataclass(frozen=True)
class TriggerContext:
    """CLI / runner inputs (not Tekton param names)."""

    product: str
    rhoai_version: str
    tests: str
    install_dependencies: bool
    external_kubeconfig: bool
    external_secret: str = ""
    resolved_app: str = ""
    resolved_rhoai_fbc_name: str = ""
    resolved_ocp_minor: str = ""
    image: str = ""
    update_channel_override: str = ""
    odh_overrides: bool = False
    ocp_version: str = ""
    ocp_channel: str = ""
    components_csv: str = ""
    test_timeout: str = ""
    test_tags: str = ""
    tests_rhoai_version: str = ""
    slack_channel_id: str = ""
    secret_source: str = "vault"
    quay_pull_secret_name: str = ""
    quay_pull_secret_explicit: bool = False
    tests_explicit: bool = False
    tests_catalog_default: str = ITS_TEST_GATES_PARAM_DEFAULT
    components_explicit: bool = False
    components_inferred: bool = False
    snapshot_yaml: str = ""
    committed_its_params: Mapping[str, str] = field(default_factory=dict)
    smoke_aws_secret: str = ""
    smoke_aws_override: bool = False
    konflux_repo: str = ""
    konflux_branch: str = ""


InferRule = tuple[str, Callable[[TriggerContext], bool], Callable[[TriggerContext], str | None]]
PatchWhen = Callable[[TriggerContext, str, Mapping[str, str | None]], bool]


@dataclass(frozen=True)
class TriggerParam:
    name: str
    default: str
    infer: tuple[InferRule, ...] = ()
    cli_when: Callable[[argparse.Namespace], bool] | None = None
    cli_value: Callable[[argparse.Namespace], str] | None = None
    patch: PatchPolicy = "always"
    patch_when: PatchWhen | None = None


# --- Shared infer helpers (multi-field / non-trivial logic only) -------------------


def _ocp_prefix(ctx: TriggerContext) -> str:
    return ocp_install_prefix((ctx.ocp_version or "").strip() or (ctx.resolved_ocp_minor or "").strip())


def _cluster_source(ctx: TriggerContext) -> str | None:
    resolved = resolve_cluster_source_for_trigger(
        product=ctx.product,
        external_secret=(ctx.external_secret or "").strip(),
    )
    return resolved or None


def _update_channel(ctx: TriggerContext) -> str:
    if (ctx.update_channel_override or "").strip():
        return (ctx.update_channel_override or "").strip()
    committed = (ctx.committed_its_params.get("UPDATE_CHANNEL") or "").strip()
    return committed or "stable"


def _rhoai_fbc_name(ctx: TriggerContext) -> str:
    if ctx.odh_overrides:
        return "odh-operator-catalog"
    resolved = (ctx.resolved_rhoai_fbc_name or "").strip()
    if resolved:
        return resolved
    for key in ("RHOAI_FBC_NAME", "FBCF_COMPONENT_NAME"):
        candidate = (ctx.committed_its_params.get(key) or "").strip()
        if candidate:
            return candidate
    if ctx.snapshot_yaml:
        from runners.cli.runner_support import first_snapshot_component_name

        return first_snapshot_component_name(ctx.snapshot_yaml)
    return ""


def _fbc_image_for_display(ctx: TriggerContext) -> str:
    img = (ctx.image or "").strip()
    if img:
        return img
    if is_test_only_product(ctx.product):
        return ""
    snap_yaml = (ctx.snapshot_yaml or "").strip()
    if not snap_yaml:
        return ""
    comp = _rhoai_fbc_name(ctx)
    if comp:
        match = re.search(
            rf"(?ms)^\s+-\s+name:\s+{re.escape(comp)}\s*$\s+containerImage:\s+(\S+)",
            snap_yaml,
        )
        if match:
            return match.group(1).strip()
    match = re.search(r"(?m)^\s+containerImage:\s+(\S+)", snap_yaml)
    return match.group(1).strip() if match else ""


@lru_cache(maxsize=64)
def _version_display_cached(
    product: str,
    rhoai_version: str,
    resolved_app: str,
    update_channel: str,
    cluster_source: str,
    effective_ocp: str,
    rhoai_fbc_name: str,
    fbc_image: str,
    fbc_image_explicit: bool,
) -> tuple[str, str, str]:
    display = resolve_version_display_params(
        product=product,
        cli_version=rhoai_version,
        resolved_app=resolved_app,
        update_channel=update_channel,
        cluster_source=cluster_source,
        cli_ocp=effective_ocp,
        ocp_explicit=bool(effective_ocp),
        rhoai_fbc_name=rhoai_fbc_name,
        fbc_image=fbc_image,
        fbc_image_explicit=fbc_image_explicit,
    )
    return display["RHOAI_VERSION"], display["OCP_VERSION"], display["RHOAI_FBC_IMAGE"]


def _version_display(ctx: TriggerContext) -> tuple[str, str, str]:
    fbc_image = _fbc_image_for_display(ctx)
    return _version_display_cached(
        ctx.product,
        ctx.rhoai_version,
        ctx.resolved_app,
        _update_channel(ctx),
        _cluster_source(ctx) or "",
        _ocp_prefix(ctx),
        _rhoai_fbc_name(ctx),
        fbc_image,
        bool(fbc_image),
    )


def _quay_secret(ctx: TriggerContext) -> str:
    if ctx.quay_pull_secret_explicit:
        return (ctx.quay_pull_secret_name or "").strip()
    committed = (ctx.committed_its_params.get("QUAY_PULL_SECRET_NAME") or "").strip()
    return committed or DEFAULT_QUAY_PULL_SECRET_NAME


def _patch_tests(ctx: TriggerContext, value: str, explicit: Mapping[str, str | None]) -> bool:
    if "TEST_GATES" in explicit:
        return True
    if bool((ctx.committed_its_params.get("TEST_GATES") or "").strip()):
        return True
    return ctx.tests_explicit or value != (ctx.tests_catalog_default or ITS_TEST_GATES_PARAM_DEFAULT)


def _patch_components(ctx: TriggerContext, value: str, explicit: Mapping[str, str | None]) -> bool:
    if "COMPONENTS" in explicit:
        return True
    if bool((ctx.committed_its_params.get("COMPONENTS") or "").strip()):
        return True
    if (ctx.components_explicit or ctx.components_inferred) and bool((ctx.components_csv or "").strip()):
        return True
    return bool((value or "").strip())


def _arg_set(args: argparse.Namespace, attr: str) -> bool:
    return bool((getattr(args, attr, "") or "").strip())


def _arg_str(args: argparse.Namespace, attr: str) -> str:
    return (getattr(args, attr, "") or "").strip()


# --- One definition per pipeline param ---------------------------------------------


TRIGGER_PARAMS: tuple[TriggerParam, ...] = (
    TriggerParam(
        "CLUSTER_SOURCE",
        "",
        infer=(
            (
                "external_secret or rhoai/odh",
                lambda c: _cluster_source(c) is not None,
                _cluster_source,
            ),
        ),
    ),
    TriggerParam(
        "CLEANUP",
        "false",
        infer=(
            (
                "infer_cleanup_param",
                lambda c: infer_cleanup_param(product=c.product, rhoai_version=c.rhoai_version) == "true",
                lambda c: infer_cleanup_param(product=c.product, rhoai_version=c.rhoai_version),
            ),
        ),
        cli_when=lambda a: getattr(a, "cleanup_opt_out", False),
        cli_value=lambda a: "false",
    ),
    TriggerParam(
        "PRODUCT",
        "rhoai",
        infer=(("CLI product set", lambda c: bool((c.product or "").strip()), lambda c: (c.product or "").strip()),),
        cli_when=lambda a: True,
        cli_value=lambda a: getattr(a, "product", "") or "",
    ),
    TriggerParam(
        "SECRET_SOURCE",
        "vault",
        infer=(
            (
                "CLI secret_source set",
                lambda c: bool((c.secret_source or "").strip()),
                lambda c: (c.secret_source or "").strip().lower() or "vault",
            ),
        ),
    ),
    TriggerParam(
        "RHOAI_VERSION",
        "",
        infer=(
            (
                "version display (UI labels)",
                lambda c: True,
                lambda c: _version_display(c)[0],
            ),
        ),
    ),
    TriggerParam(
        "OCP_VERSION",
        "",
        infer=(
            (
                "version display (UI labels)",
                lambda c: True,
                lambda c: _version_display(c)[1],
            ),
        ),
    ),
    TriggerParam(
        "RHOAI_FBC_IMAGE",
        "",
        infer=(
            (
                "version display (UI labels)",
                lambda c: True,
                lambda c: _version_display(c)[2],
            ),
        ),
    ),
    TriggerParam(
        "UPDATE_CHANNEL",
        "stable",
        infer=(("channel override / ITS / default", lambda c: True, lambda c: _update_channel(c)),),
        cli_when=lambda a: _arg_set(a, "channel"),
        cli_value=lambda a: _arg_str(a, "channel"),
    ),
    TriggerParam(
        "QUAY_PULL_SECRET_NAME",
        DEFAULT_QUAY_PULL_SECRET_NAME,
        infer=(("quay explicit / ITS / default", lambda c: True, _quay_secret),),
        cli_when=lambda a: getattr(a, "quay_pull_secret_explicit", False),
        cli_value=lambda a: _arg_str(a, "quay_pull_secret_name"),
    ),
    TriggerParam(
        "RHOAI_FBC_NAME",
        "odh-operator-catalog",
        infer=(
            ("snapshot / resolve / ODH", lambda c: bool(_rhoai_fbc_name(c)), lambda c: _rhoai_fbc_name(c)),
            ("odh_overrides", lambda c: c.odh_overrides, lambda c: "odh-operator-catalog"),
        ),
        patch="override_only",
        patch_when=lambda c, v, _e: c.odh_overrides or bool(v),
    ),
    TriggerParam(
        "OPERATOR_NAME",
        "rhods-operator",
        infer=(("odh_overrides", lambda c: c.odh_overrides, lambda c: "rhods-operator"),),
        patch="override_only",
        patch_when=lambda c, _v, _e: c.odh_overrides,
    ),
    TriggerParam(
        "OPERATOR_NAMESPACE",
        "redhat-ods-operator",
        infer=(("odh_overrides", lambda c: c.odh_overrides, lambda c: "redhat-ods-operator"),),
        patch="override_only",
        patch_when=lambda c, _v, _e: c.odh_overrides,
    ),
    TriggerParam(
        "OCP_VERSION_PREFIX",
        "",
        infer=(
            ("ocp_version or resolved minor", lambda c: bool(_ocp_prefix(c)), lambda c: _ocp_prefix(c)),
        ),
        patch="when_non_empty",
        cli_when=lambda a: _arg_set(a, "ocp_version"),
        cli_value=lambda a: ocp_install_prefix(_arg_str(a, "ocp_version")),
    ),
    TriggerParam(
        "OCP_RELEASE_CHANNEL",
        "stable",
        infer=(
            ("--ocp-channel set", lambda c: bool((c.ocp_channel or "").strip()), lambda c: (c.ocp_channel or "").strip()),
        ),
        patch="when_non_empty",
        cli_when=lambda a: _arg_set(a, "ocp_channel"),
        cli_value=lambda a: _arg_str(a, "ocp_channel"),
    ),
    TriggerParam(
        "INSTALL_DEPENDENCIES",
        "false",
        infer=(("--install-dependencies", lambda c: c.install_dependencies, lambda c: "true"),),
        patch="override_only",
        patch_when=lambda c, _v, e: c.install_dependencies or name_in_explicit("INSTALL_DEPENDENCIES", e),
        cli_when=lambda a: bool(getattr(a, "install_dependencies", False)),
        cli_value=lambda a: "true",
    ),
    TriggerParam(
        "TEST_GATES",
        ITS_TEST_GATES_PARAM_DEFAULT,
        infer=(
            ("CLI tests override", lambda c: bool((c.tests or "").strip()), lambda c: (c.tests or "").strip()),
        ),
        patch="override_only",
        patch_when=_patch_tests,
        cli_when=lambda a: getattr(a, "tests_explicit", False),
        cli_value=lambda a: _arg_str(a, "tests"),
    ),
    TriggerParam(
        "COMPONENTS",
        "",
        infer=(
            (
                "CLI components override",
                lambda c: (c.components_explicit or c.components_inferred) and bool((c.components_csv or "").strip()),
                lambda c: (c.components_csv or "").strip(),
            ),
            (
                "ITS COMPONENTS committed",
                lambda c: bool((c.committed_its_params.get("COMPONENTS") or "").strip()),
                lambda c: (c.committed_its_params.get("COMPONENTS") or "").strip(),
            ),
        ),
        patch="override_only",
        patch_when=_patch_components,
        cli_when=lambda a: getattr(a, "components_explicit", False),
        cli_value=lambda a: _arg_str(a, "components"),
    ),
    TriggerParam(
        "COMPONENT_TEST_TIMEOUT",
        "",
        infer=(
            ("--test-timeout set", lambda c: bool((c.test_timeout or "").strip()), lambda c: (c.test_timeout or "").strip()),
        ),
        patch="when_non_empty",
        cli_when=lambda a: getattr(a, "test_timeout_explicit", False),
        cli_value=lambda a: _arg_str(a, "test_timeout"),
    ),
    TriggerParam(
        "TEST_TAGS",
        "",
        infer=(("test tags set", lambda c: bool((c.test_tags or "").strip()), lambda c: (c.test_tags or "").strip()),),
        patch="when_non_empty",
        cli_when=lambda a: _arg_set(a, "test_tags"),
        cli_value=lambda a: _arg_str(a, "test_tags"),
    ),
    TriggerParam(
        "SLACK_CHANNEL_ID",
        "",
        infer=(
            (
                "--slack-channel-id set",
                lambda c: bool((c.slack_channel_id or "").strip()),
                lambda c: (c.slack_channel_id or "").strip(),
            ),
        ),
        patch="when_non_empty",
        cli_when=lambda a: _arg_set(a, "slack_channel_id"),
        cli_value=lambda a: _arg_str(a, "slack_channel_id"),
    ),
    TriggerParam(
        "OLMINSTALL_TESTS_VERSION_OVERRIDE",
        "",
        infer=(
            (
                "--tests-rhoai-version set",
                lambda c: bool((c.tests_rhoai_version or "").strip()),
                lambda c: (c.tests_rhoai_version or "").strip(),
            ),
        ),
        patch="when_non_empty",
        cli_when=lambda a: _arg_set(a, "tests_rhoai_version"),
        cli_value=lambda a: _arg_str(a, "tests_rhoai_version"),
    ),
    TriggerParam(
        "SMOKE_AWS_SECRET",
        "",
        infer=(
            (
                "shift-left smoke catalog",
                lambda c: c.smoke_aws_override and bool((c.smoke_aws_secret or "").strip()),
                lambda c: (c.smoke_aws_secret or "").strip(),
            ),
        ),
        patch="override_only",
        patch_when=lambda c, v, _e: c.smoke_aws_override and bool(v),
    ),
    TriggerParam(
        "SCRIPTS_REPO_URL",
        "",
        infer=(("--konflux-repo set", lambda c: bool((c.konflux_repo or "").strip()), lambda c: (c.konflux_repo or "").strip()),),
        patch="when_non_empty",
        cli_when=lambda a: _arg_set(a, "konflux_repo"),
        cli_value=lambda a: _arg_str(a, "konflux_repo"),
    ),
    TriggerParam(
        "SCRIPTS_REPO_REVISION",
        "",
        infer=(
            (
                "--konflux-branch set",
                lambda c: bool((c.konflux_branch or "").strip()),
                lambda c: (c.konflux_branch or "").strip(),
            ),
        ),
        patch="when_non_empty",
        cli_when=lambda a: _arg_set(a, "konflux_branch"),
        cli_value=lambda a: _arg_str(a, "konflux_branch"),
    ),
)

_PARAM_BY_NAME = {spec.name: spec for spec in TRIGGER_PARAMS}


# --- Resolution (CLI explicit, ITS, infer, patch) ----------------------------------


def name_in_explicit(name: str, explicit: Mapping[str, str | None]) -> bool:
    return name in explicit and explicit[name] is not None


def trigger_param_names() -> frozenset[str]:
    return frozenset(_PARAM_BY_NAME.keys())


def trigger_param_names_phase1() -> frozenset[str]:
    return trigger_param_names()


def trigger_params_to_clear_on_stage() -> frozenset[str]:
    return trigger_param_names() | _LEGACY_ITS_PARAM_ALIASES


def _infer(spec: TriggerParam, ctx: TriggerContext) -> str | None:
    for _label, when, resolve in spec.infer:
        if not when(ctx):
            continue
        value = resolve(ctx)
        if value is not None:
            return value
    return None


def _should_patch(spec: TriggerParam, ctx: TriggerContext, value: str, explicit: Mapping[str, str | None]) -> bool:
    if spec.patch_when is not None:
        return spec.patch_when(ctx, value, explicit)
    if spec.patch == "always":
        return True
    if spec.patch == "when_non_empty":
        return bool(value.strip())
    if name_in_explicit(spec.name, explicit):
        return True
    return False


def build_trigger_explicit_from_args(args: argparse.Namespace) -> dict[str, str | None]:
    explicit: dict[str, str | None] = {}
    for spec in TRIGGER_PARAMS:
        if spec.cli_when is None or spec.cli_value is None:
            continue
        if not spec.cli_when(args):
            continue
        explicit[spec.name] = spec.cli_value(args)
    return explicit


def resolve_trigger_params(
    ctx: TriggerContext,
    *,
    its_params: Mapping[str, str],
    explicit: Mapping[str, str | None],
) -> dict[str, str]:
    result: dict[str, str] = {}
    for spec in TRIGGER_PARAMS:
        if name_in_explicit(spec.name, explicit):
            result[spec.name] = str(explicit[spec.name])
            continue
        its_val = (its_params.get(spec.name) or "").strip()
        if its_val:
            result[spec.name] = its_val
            continue
        inferred = _infer(spec, ctx)
        if inferred is not None:
            result[spec.name] = inferred
            continue
        result[spec.name] = spec.default
    return result


def resolve_trigger_patch_plan(
    ctx: TriggerContext,
    *,
    its_params: Mapping[str, str],
    explicit: Mapping[str, str | None],
) -> tuple[dict[str, str], dict[str, bool]]:
    values = resolve_trigger_params(ctx, its_params=its_params, explicit=explicit)
    patch = {
        spec.name: _should_patch(spec, ctx, values.get(spec.name, spec.default), explicit)
        for spec in TRIGGER_PARAMS
    }
    return values, patch


def format_trigger_dependency_map() -> str:
    lines = ["Trigger param rules (WHEN : PARAM=value):", ""]
    for spec in TRIGGER_PARAMS:
        if spec.infer:
            for label, _when, resolve in spec.infer:
                sample = resolve(TriggerContext("rhoai", "", "", False, False)) or spec.default
                lines.append(f"WHEN {label:<34} : {spec.name}={sample}")
        else:
            lines.append(f"  {spec.name} default={spec.default!r}")
    lines.extend(["", "Pipeline defaults (no infer rule matched):"])
    for spec in TRIGGER_PARAMS:
        cli = "  CLI via build_trigger_explicit_from_args" if spec.cli_when else ""
        lines.append(f"  {spec.name} = {spec.default!r}{cli}")
    return "\n".join(lines)


def build_trigger_context_from_args(
    args: argparse.Namespace,
    *,
    external_secret: str = "",
    committed_its_params: Mapping[str, str] = {},
) -> TriggerContext:
    secret = (external_secret or getattr(args, "external_kubeconfig_secret", "") or "").strip()
    has_external = bool(
        getattr(args, "external_kubeconfig_path", None)
        or secret
        or getattr(args, "external_kubeconfig", "")
    )
    return TriggerContext(
        product=getattr(args, "product", "") or "",
        rhoai_version=(getattr(args, "version", "") or "").strip(),
        tests=(getattr(args, "tests", "") or "").strip(),
        install_dependencies=bool(getattr(args, "install_dependencies", False)),
        external_kubeconfig=has_external,
        external_secret=secret,
        update_channel_override=(getattr(args, "channel", "") or "").strip(),
        ocp_version=(getattr(args, "ocp_version", "") or "").strip(),
        ocp_channel=(getattr(args, "ocp_channel", "") or "").strip(),
        components_csv=(getattr(args, "components", "") or "").strip(),
        test_timeout=(getattr(args, "test_timeout", "") or "").strip(),
        test_tags=(getattr(args, "test_tags", "") or "").strip(),
        tests_rhoai_version=(getattr(args, "tests_rhoai_version", "") or "").strip(),
        slack_channel_id=(getattr(args, "slack_channel_id", "") or "").strip(),
        secret_source=(getattr(args, "secret_source", "vault") or "vault").strip().lower(),
        quay_pull_secret_name=(getattr(args, "quay_pull_secret_name", "") or "").strip(),
        quay_pull_secret_explicit=bool(getattr(args, "quay_pull_secret_explicit", False)),
        tests_explicit=bool(getattr(args, "tests_explicit", False)),
        tests_catalog_default=getattr(args, "tests_catalog_default_csv", ITS_TEST_GATES_PARAM_DEFAULT),
        components_explicit=bool(getattr(args, "components_explicit", False)),
        components_inferred=bool(getattr(args, "components_inferred", False)),
        committed_its_params=dict(committed_its_params),
        konflux_repo=(getattr(args, "konflux_repo", "") or "").strip(),
        konflux_branch=(getattr(args, "konflux_branch", "") or "").strip(),
    )


def build_trigger_context_from_runner(
    runner: object,
    *,
    external_secret: str,
    odh_overrides: bool,
    committed_its_params: Mapping[str, str],
) -> TriggerContext:
    snapshot_yaml = ""
    snapshot_file = getattr(runner, "snapshot_file", None)
    if snapshot_file is not None:
        try:
            snapshot_yaml = snapshot_file.read_text(encoding="utf-8")
        except OSError:
            snapshot_yaml = ""
    smoke_override = bool(runner._smoke_aws_its_override())
    smoke_secret = runner._resolve_smoke_aws_secret() if smoke_override else ""
    base = build_trigger_context_from_args(
        runner.args,
        external_secret=external_secret,
        committed_its_params=committed_its_params,
    )
    return replace(
        base,
        external_secret=external_secret,
        resolved_app=(getattr(runner, "resolved_app", "") or "").strip(),
        resolved_rhoai_fbc_name=(getattr(runner, "resolved_rhoai_fbc_name", "") or "").strip(),
        resolved_ocp_minor=(getattr(runner, "resolved_ocp_minor", "") or "").strip(),
        image=(getattr(runner, "image", "") or "").strip(),
        update_channel_override=(getattr(runner, "update_channel_override", "") or "").strip(),
        odh_overrides=odh_overrides,
        snapshot_yaml=snapshot_yaml,
        smoke_aws_secret=smoke_secret,
        smoke_aws_override=smoke_override,
    )


def read_trigger_its_params(manifest_path: Path | None) -> dict[str, str]:
    if manifest_path is None:
        return {}
    return {
        name: value
        for name in trigger_param_names()
        for value in [its_manifest_param(manifest_path, name)]
        if value
    }


def read_committed_its_params(its_file: Path) -> dict[str, str]:
    out: dict[str, str] = {}
    for name in trigger_param_names() | _LEGACY_ITS_PARAM_ALIASES:
        value = its_manifest_param(its_file, name)
        if value:
            out[name] = value
    return out


def apply_trigger_param_resolution(
    args: argparse.Namespace,
    *,
    its_manifest_path: Path | None = None,
) -> None:
    its_params = read_trigger_its_params(its_manifest_path)
    explicit = build_trigger_explicit_from_args(args)
    ctx = build_trigger_context_from_args(args, committed_its_params=its_params)
    resolved = resolve_trigger_params(ctx, its_params=its_params, explicit=explicit)
    args.trigger_explicit = explicit
    args.cleanup = resolved.get("CLEANUP", "false") == "true"
