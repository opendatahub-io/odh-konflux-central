"""Argument parser definitions for ``rhoai_e2e.py``."""

from __future__ import annotations

import argparse
import os
import sys
import textwrap
from pathlib import Path
from typing import NoReturn

from suite.constants import (
    DEFAULT_APP,
    DEFAULT_KA_HOST,
    DEFAULT_KONFLUX_SERVER,
    DEFAULT_KONFLUX_UI,
    DEFAULT_LIST_COUNT,
    DEFAULT_NAMESPACE,
    DEFAULT_PRODUCT,
    DEFAULT_QUAY_PULL_SECRET_NAME,
    DEFAULT_TESTS_CONFIG_RELATIVE,
    LIST_SUPPORTED_OCP_MAX_PRS,
    PRODUCT_INSTALL_CHOICES,
)

# When user passes ``--ka-host`` with no URL, read KA_HOST from the environment.
_ITS_REF_HELP = (
    "metadata.name, cwd-relative or absolute path to a manifest file "
    "(no repo fallback for ./…, ../…, or integration-tests/… paths), "
    "or rhoai-e2e-relative path (e.g. tekton/its/…)"
)
_KA_HOST_FROM_ENV = "__KA_HOST_FROM_ENV__"


class CliHelpFormatter(argparse.ArgumentDefaultsHelpFormatter, argparse.RawDescriptionHelpFormatter):
    """Keep epilog layout; append option defaults when helpful (similar intent to Click ``show_default``)."""

    def _get_help_string(self, action: argparse.Action) -> str:
        help_txt = action.help
        if help_txt is None:
            help_txt = ""
        if "%(default)" in help_txt:
            return super()._get_help_string(action)
        # PY-CLI-4: omit noisy "(default: False)" on store_true / store_false flags.
        if isinstance(action, argparse._StoreTrueAction) or isinstance(action, argparse._StoreFalseAction):
            return help_txt
        optional_value = action.nargs in (None, argparse.OPTIONAL, argparse.ZERO_OR_MORE)
        if (
            action.option_strings
            and optional_value
            and not action.required
            and action.default is not argparse.SUPPRESS
        ):
            if action.default == "":
                return help_txt
            if action.default is None:
                return help_txt
        return super()._get_help_string(action)


def emit_click_style_error(parser: argparse.ArgumentParser | None, message: str, *, usage: bool) -> None:
    if usage and parser is not None:
        parser.print_usage(sys.stderr)
        print(file=sys.stderr)
        print(f"Try '{parser.prog} --help' for help.\n", file=sys.stderr)
    print(f"Error: {message}", file=sys.stderr)


class CliArgumentParser(argparse.ArgumentParser):
    """Emit usage + ``Try '… --help'`` + ``Error:`` on parser failures (Click-style)."""

    def error(self, message: str) -> NoReturn:
        emit_click_style_error(self, message, usage=True)
        self.exit(2)


def _add_product_group(parser: CliArgumentParser) -> None:
    product = parser.add_argument_group(
        "product & catalog",
        "RHOAI deploy: product, FBC image, channel, EPHC OCP version, and supported-OCP query (trigger or --list-supported-ocp).",
    )
    product.add_argument(
        "--image",
        default="",
        metavar="REF",
        help="FBC/catalog image. Empty = resolve from Konflux for --product rhoai; omitted for test-only.",
    )
    product.add_argument(
        "--product",
        default=DEFAULT_PRODUCT,
        metavar="NAME",
        choices=sorted(PRODUCT_INSTALL_CHOICES),
        help=(
            "Omit for test-only on an external cluster (skip EPHC/install and FBC snapshot extract). "
            "rhoai: catalog wiring and auto image resolution for full installs."
        ),
    )
    product.add_argument(
        "--install-dependencies",
        action="store_true",
        help=(
            "Test-only (omit --product): run install-dep-operators (setup-dependencies.sh, RHCL, "
            "cluster prep) before component tests instead of prepare-components-prerequisites."
        ),
    )
    product.add_argument(
        "--rhoai-version",
        dest="version",
        metavar="VER",
        default="",
        help=(
            "RHOAI catalog stream (e.g. 3.5-ea.2): resolve FBC from rhoai-v* apps. "
            "Implies --product rhoai when --product is omitted."
        ),
    )
    product.add_argument(
        "--rhoai-channel",
        dest="channel",
        metavar="NAME",
        default="",
        help="OLM UPDATE_CHANNEL passed to the ITS (e.g. stable-3.x, odh-stable, beta).",
    )
    product.add_argument(
        "--ocp-version",
        metavar="X.Y",
        default="",
        help=(
            "OCP cluster minor (e.g. 4.21 or 5.0): ephemeral provision uses that version. "
            "With --product rhoai, OCP 4.x also selects rhoai-fbc-fragment-ocp-4XX; OCP 5.x "
            "keeps the ITS FBC (no ocp-5XX fragment). External kubeconfig: optional override "
            "(auto-detected when omitted). With --list-supported-ocp, assert minor is listed."
        ),
    )
    product.add_argument(
        "--ocp-channel",
        dest="ocp_channel",
        metavar="KIND",
        default="",
        choices=("stable", "candidate", "nightly"),
        help=(
            "OpenShift CI payload stream for provision-ephemeral-cluster: stable (GA, default), "
            "candidate (EC), or nightly. Independent of --rhoai-channel (OLM)."
        ),
    )
    product.add_argument(
        "--list-supported-ocp",
        action="store_true",
        help=f"Print supported OCP minors from archived logs (≤{LIST_SUPPORTED_OCP_MAX_PRS} runs). Use with --ocp-version to verify.",
    )


def _add_tests_group(parser: CliArgumentParser) -> None:
    tests = parser.add_argument_group(
        "tests & components",
        "Test gates, phase config, smoke components, and timeouts (trigger only).",
    )
    tests.add_argument(
        "--tests",
        metavar="LIST",
        default=None,
        help=(
            "Comma-separated TEST_GATES for the ITS (e.g. bvt,smoke). "
            "Unknown tokens that match catalog test slices (e.g. SmokeSet5, @SanitySet1) "
            "filter sub-selections and infer the matching phase (smoke/tier1). "
            "Include every phase marked requiredInSelection in --tests-config. "
            "Omit to use defaults from that file."
        ),
    )
    tests.add_argument(
        "--tests-config",
        metavar="PATH",
        default="",
        help=(
            "Path to rhoai-e2e-tests-config.yaml (phase list + defaults). "
            f"Default: {DEFAULT_TESTS_CONFIG_RELATIVE}"
        ),
    )
    tests.add_argument(
        "--tests-rhoai-version",
        dest="tests_rhoai_version",
        metavar="VER",
        default="",
        help=(
            "Override installed CSV for opendatahub-tests image tag and component version gates. "
            "Use on external test-only runs (omit --product) (optional)."
        ),
    )
    tests.add_argument(
        "--components",
        metavar="LIST",
        default=None,
        help=(
            "Comma-separated smoke component ids (rhoai-e2e-components-smoke.yaml). "
            "Only when --tests includes smoke. Omit or ``all`` = every enabled catalog id. "
            "Use --list-components to see available IDs."
        ),
    )
    tests.add_argument(
        "--list-components",
        action="store_true",
        help="Print the table of available smoke components and descriptions.",
    )
    tests.add_argument(
        "--secret-source",
        choices=("vault", "tenant"),
        default=os.environ.get("OLMINSTALL_SECRET_SOURCE", "vault").strip() or "vault",
        help=(
            "Where component tests load Jenkins envFile* credentials: "
            "vault (AppRole + apps/rhods-ci/shift-left at runtime, default) or "
            "tenant (cloned Konflux Secrets). Env: OLMINSTALL_SECRET_SOURCE."
        ),
    )
    tests.add_argument(
        "--test-timeout",
        metavar="DURATION",
        default=os.environ.get("OLMINSTALL_TEST_TIMEOUT", ""),
        help=(
            "Per-component smoke pytest timeout (e.g. 10m, 90s). "
            "Failed components do not stop the pipeline. Env: OLMINSTALL_TEST_TIMEOUT."
        ),
    )


def _parse_cleanup_cli_value(raw: str) -> bool:
    text = (raw or "").strip().lower()
    if text == "true":
        return True
    if text == "false":
        return False
    raise argparse.ArgumentTypeError("--cleanup must be true or false (or omit the value for true)")


def _add_external_group(parser: CliArgumentParser) -> None:
    external = parser.add_argument_group(
        "external cluster",
        "Skip EPHC; run install/BVT/smoke on a pre-existing cluster (trigger only).",
    )
    external.add_argument(
        "--external-kubeconfig",
        metavar="PATH",
        default=os.environ.get("OLMINSTALL_EXTERNAL_KUBECONFIG", ""),
        help="Upload local kubeconfig as a tenant Secret (key kubeconfig). Env: OLMINSTALL_EXTERNAL_KUBECONFIG.",
    )
    external.add_argument(
        "--external-kubeconfig-context",
        metavar="NAME",
        default=os.environ.get("OLMINSTALL_EXTERNAL_KUBECONFIG_CONTEXT", ""),
        help=(
            "Kubeconfig context to upload for --external-kubeconfig (default: auto-select cluster-admin "
            "when current context is limited). Env: OLMINSTALL_EXTERNAL_KUBECONFIG_CONTEXT."
        ),
    )
    external.add_argument(
        "--external-kubeconfig-secret",
        metavar="NAME",
        default=os.environ.get("OLMINSTALL_EXTERNAL_KUBECONFIG_SECRET", ""),
        help="Use existing tenant Secret (key kubeconfig). Env: OLMINSTALL_EXTERNAL_KUBECONFIG_SECRET.",
    )
    external.add_argument(
        "--cleanup",
        nargs="?",
        const="true",
        type=_parse_cleanup_cli_value,
        metavar="true|false",
        default=None,
        help=(
            "Maintenance: --cleanup or --cleanup true runs rhoai-e2e cleanup.sh -t operator locally "
            "(requires --external-kubeconfig or --external-kubeconfig-secret; does not trigger a "
            "PipelineRun). On a trigger run, only --cleanup false opts out of inferred pipeline "
            "CLEANUP (rhoai/odh). Destructive."
        ),
    )
    external.add_argument(
        "--force-cluster-run",
        action="store_true",
        help=(
            "Skip external-cluster single-flight wait/check in external-cluster-ready and allow "
            "parallel rhoai-e2e runs on the same physical cluster (EPHC unchanged)."
        ),
    )


def _add_konflux_group(parser: CliArgumentParser) -> None:
    konflux = parser.add_argument_group(
        "konflux",
        "PipelineRun control, tenant, Application, ITS enable/disable, pipeline git source, "
        "UI/API, and KubeArchive. Default (no run flag): trigger a new E2E PipelineRun (e2e-cli-*).",
    )
    konflux.add_argument(
        "--watch-pipelines",
        "-w",
        "--watch",
        nargs="?",
        const="",
        default=None,
        dest="watch",
        metavar="PIPELINERUN",
        help=(
            "Watch an existing E2E run (e2e-cli-* / e2e-its-*): newest match for --konflux-app "
            "(same order as --list-pipelines), else match by owner/Snapshot, or name PIPELINERUN."
        ),
    )
    konflux.add_argument(
        "--list-pipelines",
        "-l",
        "--list",
        nargs="?",
        const=str(DEFAULT_LIST_COUNT),
        default=None,
        dest="list_pipelines",
        metavar="N",
        help=f"List last N PipelineRuns for --konflux-app (default N={DEFAULT_LIST_COUNT}).",
    )
    konflux.add_argument(
        "--delete-pending-pipelines",
        action="store_true",
        help=(
            "Stop incomplete E2E PipelineRuns (e2e-* and legacy olminstall-*) for --konflux-app: "
            "Kueue/resolver pending and your owned incomplete runs (PR or Snapshot rhoai-e2e.run-owner). "
            "Live runs with tasks are cancelled via tkn (Konflux Stop/Cancel) before oc delete when selected. "
            "Use --include-unowned-stuck for shared-tenant runs stuck with no TaskRuns. Does not remove "
            "archived Konflux UI ghosts (see README)."
        ),
    )
    konflux.add_argument(
        "--dry-run",
        action="store_true",
        dest="dry_run",
        help="With --delete-pending-pipelines: list targets only; no cancel or delete.",
    )
    konflux.add_argument(
        "--stop-owned-running",
        action="store_true",
        help=(
            "With --delete-pending-pipelines: also cancel+delete your owned PipelineRuns that are actively "
            "Running with TaskRuns (default skips them). Requires tkn in PATH for graceful cancel."
        ),
    )
    konflux.add_argument(
        "--include-unowned-stuck",
        action="store_true",
        help=(
            "With --delete-pending-pipelines: also stop rhoai-e2e runs stuck with no TaskRuns that lack "
            "your rhoai-e2e.run-owner marker (shared tenant only; default skips unowned runs)."
        ),
    )
    konflux.add_argument(
        "--konflux-namespace",
        dest="namespace",
        default=DEFAULT_NAMESPACE,
        metavar="NAMESPACE",
        help="Konflux tenant namespace.",
    )
    konflux.add_argument(
        "--konflux-app",
        dest="app",
        default=DEFAULT_APP,
        metavar="APP",
        help="Konflux Application name.",
    )
    konflux.add_argument(
        "--enable-its",
        metavar="NAME_OR_PATH",
        default="",
        help=(
            f"Apply an in-tree IntegrationTestScenario manifest by {_ITS_REF_HELP}. "
            "Uses --konflux-namespace; spec.application comes from the manifest unless "
            "--konflux-app is passed. Only Konflux rollout flags are allowed "
            "(--konflux-repo, --konflux-branch, --konflux-app). Cluster and test scope "
            "flags are rejected; use --run-its for debug."
        ),
    )
    konflux.add_argument(
        "--disable-its",
        metavar="NAME_OR_PATH",
        default="",
        help=(
            f"Delete IntegrationTestScenario from --konflux-namespace by {_ITS_REF_HELP} "
            "(stops auto/integration triggers for that scenario)."
        ),
    )
    konflux.add_argument(
        "--run-its",
        metavar="NAME_OR_PATH",
        default="",
        help=(
            f"One-shot debug run: create a direct PipelineRun from the ITS manifest ({_ITS_REF_HELP}; "
            "generateName e2e-cli-{user}-…; does not apply the ITS to the cluster). Accepts cluster and "
            "test overrides (--components, --tests, --external-kubeconfig, etc.) in addition to "
            "Konflux flags."
        ),
    )
    konflux.add_argument(
        "--konflux-ui",
        metavar="URL",
        default=os.environ.get("KONFLUX_UI", DEFAULT_KONFLUX_UI),
        help="Konflux UI base URL (env KONFLUX_UI; inferred on hosted clusters).",
    )
    konflux.add_argument(
        "--ka-host",
        nargs="?",
        metavar="URL",
        const=_KA_HOST_FROM_ENV,
        default=os.environ.get("KA_HOST", DEFAULT_KA_HOST),
        help="KubeArchive API base URL (archive UI). Bare --ka-host reads env KA_HOST.",
    )
    konflux.add_argument(
        "--konflux-server",
        metavar="URL",
        default=os.environ.get("KONFLUX_SERVER", DEFAULT_KONFLUX_SERVER),
        help="Konflux API URL for oc login fallback (env KONFLUX_SERVER).",
    )
    konflux.add_argument(
        "--konflux-repo",
        metavar="URL",
        default="",
        help=(
            "Git URL with integration-tests/rhoai-e2e/ (patches ITS resolver; needs yq). "
            "Omit = ITS default opendatahub-io/odh-konflux-central @ main. "
            "Or set OLMINSTALL_PIPELINE_REPO / OLMINSTALL_PIPELINE_REVISION."
        ),
    )
    konflux.add_argument(
        "--konflux-branch",
        metavar="REF",
        default="",
        help="Git revision for --konflux-repo (branch, tag, or SHA). Omit = main from ITS YAML.",
    )
    konflux.add_argument(
        "--quay-pull-secret-name",
        dest="quay_pull_secret_name",
        metavar="NAME",
        default=DEFAULT_QUAY_PULL_SECRET_NAME,
        help=(
            "QUAY_PULL_SECRET_NAME for rhoai/odh image pulls (Catalog/Operator/sidecars). "
            "Omit = pipeline default; ITS param wins unless this flag is passed."
        ),
    )


def _add_reporting_group(parser: CliArgumentParser) -> None:
    reporting = parser.add_argument_group(
        "reporting",
        "Run notifications and external reporting (trigger only).",
    )
    reporting.add_argument(
        "--slack-channel-id",
        metavar="CHANNEL",
        default="",
        help=(
            "Slack channel ID for run notification (requires slack-webhook secret in the namespace). "
            "Omit to suppress Slack."
        ),
    )


def make_parser(description: str = "", epilog: str | None = None) -> CliArgumentParser:
    desc = textwrap.dedent(description or "").strip() or "Konflux OLM pipeline CLI."
    epi = None if epilog is None else textwrap.dedent(epilog).strip()
    prog = Path(sys.argv[0]).name if sys.argv else "rhoai_e2e.py"
    parser = CliArgumentParser(
        prog=prog,
        formatter_class=CliHelpFormatter,
        description=desc,
        epilog=epi,
    )
    _add_product_group(parser)
    _add_tests_group(parser)
    _add_external_group(parser)
    _add_konflux_group(parser)
    _add_reporting_group(parser)
    return parser
