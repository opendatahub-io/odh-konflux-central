"""Fixtures and helpers for rhoai_e2e.py CLI argument tests."""

from __future__ import annotations

from suite.constants import DEFAULT_APP, DEFAULT_NAMESPACE
import sys
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

from runners.cli.cli import make_parser
from suite.component_catalog import default_components_smoke_config_path, load_components_smoke_catalog

_RHOAI_E2E_ROOT = Path(__file__).resolve().parents[2]

ALL_COMPONENTS_CSV = load_components_smoke_catalog(default_components_smoke_config_path()).enabled_components_csv


def apply_env(monkeypatch: pytest.MonkeyPatch, env: dict[str, str | None] | None) -> None:
    if not env:
        return
    for key, value in env.items():
        if value is None:
            monkeypatch.delenv(key, raising=False)
        else:
            monkeypatch.setenv(key, value)


@dataclass(frozen=True)
class OkCase:
    argv: list[str]
    env: dict[str, str | None] | None = None
    check: Callable[[Any], None] | None = None
    id: str = ""


@dataclass(frozen=True)
class ErrCase:
    argv: list[str]
    substr: str
    env: dict[str, str | None] | None = None
    id: str = ""


def _checks(*conditions: bool) -> None:
    if not all(conditions):
        raise AssertionError("condition false")


def ok_cases() -> list[OkCase]:
    return [
        OkCase(
            [],
            check=lambda a: _checks(not a.product, a.list_pipelines == 0, not a.watch_mode),
            id="defaults",
        ),
        OkCase(
            ["--product", "rhoai"],
            check=lambda a: _checks(a.product == "rhoai", a.list_pipelines == 0, not a.watch_mode),
            id="product-rhoai",
        ),
        OkCase(
            ["--tests", "bvt"],
            check=lambda a: _checks(not a.product, a.tests == "bvt", not a.watch_mode),
            id="tests-bvt",
        ),
        OkCase(
            ["--tests", "smoke"],
            check=lambda a: _checks(a.tests == "smoke", a.components == ALL_COMPONENTS_CSV),
            id="tests-smoke-all-components",
        ),
        OkCase(
            ["--tests", "smoke", "--components", "all"],
            check=lambda a: _checks(
                a.tests == "smoke",
                a.components == ALL_COMPONENTS_CSV,
                a.components_explicit,
            ),
            id="tests-smoke-components-all-alias",
        ),
        OkCase(
            ["--tests", "smoke", "--components", "workbenches,model_registry"],
            check=lambda a: _checks(a.tests == "smoke", a.components == "workbenches,model_registry"),
            id="tests-smoke-subset",
        ),
        OkCase(
            ["--tests", "SmokeSet5"],
            check=lambda a: _checks(a.tests == "smoke", a.test_tags == "SmokeSet5", a.components == "dashboard_cypress"),
            id="tests-cypress-smokeset5",
        ),
        OkCase(["--tests", "smoke", "--secret-source", "tenant"], check=lambda a: _checks(a.secret_source == "tenant"), id="secret-source-tenant"),
        OkCase(["--tests", "smoke", "--test-timeout", "600"], check=lambda a: _checks(a.test_timeout == "600s"), id="test-timeout-600"),
        OkCase(
            ["--product", "rhoai", "--rhoai-version", "3.5"],
            check=lambda a: _checks(a.version == "3.5", a.product == "rhoai"),
            id="rhoai-version",
        ),
        OkCase(
            ["--rhoai-version", "3.5-ea.2"],
            check=lambda a: _checks(a.version == "3.5-ea.2", a.product == "rhoai"),
            id="rhoai-version-implies-product",
        ),
        OkCase(["--watch"], check=lambda a: _checks(a.watch_mode, a.watch == ""), id="watch-long"),
        OkCase(["-w"], check=lambda a: _checks(a.watch_mode, a.watch == ""), id="watch-short"),
        OkCase(["--watch-pipelines"], check=lambda a: _checks(a.watch_mode, a.watch == ""), id="watch-pipelines"),
        OkCase(["--list-pipelines"], check=lambda a: _checks(a.list_pipelines == 10), id="list-pipelines"),
        OkCase(["-l"], check=lambda a: _checks(a.list_pipelines == 10), id="list-short"),
        OkCase(["--list", "3"], check=lambda a: _checks(a.list_pipelines == 3), id="list-long"),
        OkCase(["-w", "pr-xyz"], check=lambda a: _checks(a.watch_mode, a.watch == "pr-xyz"), id="watch-named"),
        OkCase(
            ["--delete-pending-pipelines"],
            check=lambda a: _checks(a.delete_pending_pipelines),
            id="delete-pending-pipelines",
        ),
        OkCase(
            ["--delete-pending-pipelines", "--stop-owned-running"],
            check=lambda a: _checks(a.delete_pending_pipelines, a.stop_owned_running),
            id="delete-stop-owned-running",
        ),
        OkCase(
            ["--delete-pending-pipelines", "--include-unowned-stuck"],
            check=lambda a: _checks(a.delete_pending_pipelines, a.include_unowned_stuck),
            id="delete-include-unowned-stuck",
        ),
        OkCase(
            ["--delete-pending-pipelines", "--dry-run"],
            check=lambda a: _checks(a.delete_pending_pipelines, a.dry_run),
            id="delete-dry-run",
        ),
        OkCase(
            ["--enable-its", "rhoai-e2e-rh-nightly-pm-ocp420"],
            check=lambda a: _checks(
                a.enable_its == "rhoai-e2e-rh-nightly-pm-ocp420",
                not a.konflux_app_explicit,
                a.app == "rhoai-fbc-fragment-ocp-420",
                not a.disable_its,
            ),
            id="enable-its-rh-nightly",
        ),
        OkCase(
            [
                "--run-its",
                "tekton/its/its-rhoai-e2e-rh-nightly-pm-ocp420.yaml",
            ],
            check=lambda a: _checks(
                a.run_its.endswith("its-rhoai-e2e-rh-nightly-pm-ocp420.yaml"),
                a.its_scenario_name == "rhoai-e2e-rh-nightly-pm-ocp420",
                a.app == "rhoai-fbc-fragment-ocp-420",
            ),
            id="run-its-rh-nightly-by-path",
        ),
        OkCase(
            [
                "--enable-its",
                "tekton/its/its-rhoai-e2e-rh-nightly-pm-ocp420.yaml",
            ],
            check=lambda a: _checks(
                a.enable_its == "tekton/its/its-rhoai-e2e-rh-nightly-pm-ocp420.yaml",
                a.its_scenario_name == "rhoai-e2e-rh-nightly-pm-ocp420",
            ),
            id="enable-its-by-rhoai-e2e-relative-path",
        ),
        OkCase(
            [
                "--run-its",
                "rhoai-e2e-ephc-playpen-a",
                "--tests",
                "smoke",
                "--components",
                "all",
            ],
            check=lambda a: _checks(
                a.run_its == "rhoai-e2e-ephc-playpen-a",
                a.tests == "smoke",
                a.components == ALL_COMPONENTS_CSV,
                a.components_explicit,
            ),
            id="run-its-components-all",
        ),
        OkCase(
            [
                "--run-its",
                "rhoai-e2e-rh-nightly-pm-ocp420",
                "--tests",
                "smoke",
                "--components",
                "dashboard_cypress",
            ],
            check=lambda a: _checks(
                a.run_its == "rhoai-e2e-rh-nightly-pm-ocp420",
                a.tests == "smoke",
                a.components == "dashboard_cypress",
            ),
            id="run-its-scoped-smoke",
        ),
        OkCase(
            ["--enable-its", "rhoai-e2e-rh-nightly-pm-ocp420", "--konflux-app", DEFAULT_APP],
            check=lambda a: _checks(
                a.enable_its == "rhoai-e2e-rh-nightly-pm-ocp420",
                a.konflux_app_explicit,
                a.app == DEFAULT_APP,
            ),
            id="enable-its-rh-nightly-playpen-debug",
        ),
        OkCase(
            ["--disable-its", "rhoai-e2e-rh-nightly-pm-ocp420"],
            check=lambda a: _checks(a.disable_its == "rhoai-e2e-rh-nightly-pm-ocp420", not a.enable_its),
            id="disable-its-rh-nightly",
        ),
        OkCase(
            ["--list-supported-ocp"],
            check=lambda a: _checks(a.list_supported_ocp, a.list_pipelines == 0, not a.watch_mode),
            id="list-supported-ocp",
        ),
        OkCase(
            ["--list-supported-ocp", "--ocp-version", "4.19"],
            check=lambda a: _checks(a.list_supported_ocp, a.ocp_version == "4.19", a.list_pipelines == 0),
            id="list-supported-ocp-version",
        ),
        OkCase(
            ["--konflux-namespace", "ns1", "--konflux-app", "app1", "--rhoai-channel", "ch1"],
            check=lambda a: _checks(a.namespace == "ns1", a.app == "app1", a.channel == "ch1"),
            id="namespace-app-channel",
        ),
        OkCase(
            ["--konflux-ui", "https://konflux-ui.example.com"],
            check=lambda a: _checks(a.konflux_ui == "https://konflux-ui.example.com"),
            id="konflux-ui",
        ),
        OkCase(
            ["--ka-host", "https://kubearchive.example.com"],
            check=lambda a: _checks(a.ka_host == "https://kubearchive.example.com"),
            id="ka-host-explicit",
        ),
        OkCase(
            ["--konflux-server", "https://api.stone.example.com:6443"],
            check=lambda a: _checks(a.konflux_server == "https://api.stone.example.com:6443"),
            id="konflux-server",
        ),
        OkCase(
            ["--image", "quay.io/rhoai/x@sha256:deadbeef"],
            check=lambda a: _checks(a.image == "quay.io/rhoai/x@sha256:deadbeef"),
            id="image",
        ),
        OkCase(
            ["--konflux-repo", "https://github.com/o/r.git", "--konflux-branch", "your-branch"],
            check=lambda a: _checks(a.konflux_repo.endswith(".git"), a.konflux_branch == "your-branch"),
            id="konflux-repo-branch",
        ),
        OkCase(["--ocp-version", "4.20"], check=lambda a: _checks(a.ocp_version == "4.20"), id="ocp-version-420"),
        OkCase(["--ocp-version", " 4.19 "], check=lambda a: _checks(a.ocp_version == "4.19"), id="ocp-version-trimmed"),
        OkCase(
            ["--tests", "bvt", "--ocp-version", "4.19"],
            check=lambda a: _checks(a.tests == "bvt", a.ocp_version == "4.19"),
            id="bvt-ocp-version",
        ),
        OkCase(
            ["--tests", "bvt", "--image", "quay.io/rhoai/x@sha256:deadbeef"],
            check=lambda a: _checks(a.tests == "bvt"),
            id="bvt-image",
        ),
        OkCase(
            ["--tests", "bvt", "--rhoai-channel", "stable"],
            check=lambda a: _checks(a.tests == "bvt"),
            id="bvt-channel",
        ),
        OkCase(
            ["--ocp-version", "5.0", "--ocp-channel", "candidate"],
            check=lambda a: _checks(a.ocp_version == "5.0", a.ocp_channel == "candidate"),
            id="ocp-channel-candidate",
        ),
        OkCase(["--tests", "tier1,bvt"], check=lambda a: _checks(a.tests == "bvt,tier1"), id="tests-normalized"),
        OkCase(
            ["--ka-host"],
            env={"KA_HOST": "https://kubearchive.apps.cluster.openshiftapps.com"},
            check=lambda a: _checks(a.ka_host.startswith("https://kubearchive")),
            id="ka-host-from-env",
        ),
        OkCase(
            [
                "--product",
                "rhoai",
                "--rhoai-version",
                "3.4",
                "--rhoai-channel",
                "stable-3.x",
                "--konflux-namespace",
                DEFAULT_NAMESPACE,
                "--konflux-app",
                DEFAULT_APP,
                "--image",
                "quay.io/rhoai/f@sha256:abc",
                "--konflux-repo",
                "https://github.com/you/fork.git",
                "--konflux-branch",
                "branch",
                "--konflux-ui",
                "https://ui.example.com",
                "--ka-host",
                "https://ka.example.com",
                "--konflux-server",
                "https://api.stone-prod-p02.example.com:6443",
                "--ocp-version",
                "4.19",
            ],
            check=lambda a: _checks(a.product == "rhoai", a.version == "3.4", a.channel == "stable-3.x", a.namespace == DEFAULT_NAMESPACE, a.app == DEFAULT_APP, a.ocp_version == "4.19", not a.watch_mode, a.konflux_repo.endswith(".git"), a.konflux_branch == "branch"),
            id="full-trigger",
        ),
        OkCase(
            [
                "-w",
                "--konflux-namespace",
                DEFAULT_NAMESPACE,
                "--konflux-app",
                DEFAULT_APP,
                "--ka-host",
                "https://ka.example.com",
            ],
            check=lambda a: _checks(a.watch_mode, a.namespace == DEFAULT_NAMESPACE, a.app == DEFAULT_APP),
            id="watch-with-namespace",
        ),
    ]


def err_cases() -> list[ErrCase]:
    return [
        ErrCase(["--tests", "bvt", "--components", "workbenches"], "only valid when --tests includes smoke", id="bvt-components"),
        ErrCase(
            ["--tests", "smoke", "--components", "all,workbenches"],
            "cannot be mixed",
            id="components-all-mixed",
        ),
        ErrCase(["--tests", "bvt", "--test-timeout", "10m"], "only valid when --tests includes smoke", id="bvt-timeout"),
        ErrCase(["--tests", "smoke", "--test-timeout", "0m"], "greater than zero", id="timeout-zero"),
        ErrCase(["--tests", "smoke", "--test-timeout", "junk"], "must be a duration", id="timeout-junk"),
        ErrCase(["--rhoai-version", "3-5"], "MAJOR.MINOR", id="rhoai-version-hyphen"),
        ErrCase(["--ka-host"], "KA_HOST", env={"KA_HOST": ""}, id="ka-host-empty-env"),
        ErrCase(["--ka-host"], "KA_HOST", env={"KA_HOST": None}, id="ka-host-missing-env"),
        ErrCase(["--konflux-ui", "http://insecure.local"], "https://", id="konflux-ui-http"),
        ErrCase(["--ka-host", "http://insecure.local"], "https://", id="ka-host-http"),
        ErrCase(["--konflux-server", "http://api:6443"], "https://", id="konflux-server-http"),
        ErrCase(["-l", "0"], "positive integer", id="list-zero"),
        ErrCase(["-l", "-3"], "positive integer", id="list-negative"),
        ErrCase(["-l", "nope"], "positive integer", id="list-invalid"),
        ErrCase(["--ocp-version", "4"], "MAJOR.MINOR", id="ocp-version-major-only"),
        ErrCase(["--ocp-version", "foo"], "MAJOR.MINOR", id="ocp-version-foo"),
        ErrCase(["--ocp-version", "4.20.21"], "MAJOR.MINOR", id="ocp-version-patch"),
        ErrCase(["--list-supported-ocp", "-l"], "mutually exclusive", id="list-supported-ocp-list"),
        ErrCase(["--list-supported-ocp", "-w"], "mutually exclusive", id="list-supported-ocp-watch"),
        ErrCase(["-l", "-w"], "mutually exclusive", id="list-watch"),
        ErrCase(["--list", "--watch"], "mutually exclusive", id="list-watch-long"),
        ErrCase(["--delete-pending-pipelines", "-l"], "mutually exclusive", id="delete-list"),
        ErrCase(["--delete-pending-pipelines", "-w"], "mutually exclusive", id="delete-watch"),
        ErrCase(["--stop-owned-running"], "--delete-pending-pipelines", id="stop-owned-without-delete"),
        ErrCase(["--include-unowned-stuck"], "--delete-pending-pipelines", id="include-unowned-without-delete"),
        ErrCase(["--dry-run"], "--delete-pending-pipelines", id="dry-run-without-delete"),
        ErrCase(
            ["--enable-its", "rhoai-e2e-rh-nightly-pm-ocp420", "--disable-its", "x"],
            "mutually exclusive",
            id="enable-disable-its",
        ),
        ErrCase(
            ["--enable-its", "rhoai-e2e-rh-nightly-pm-ocp420", "--run-its", "rhoai-e2e-rh-nightly-pm-ocp420"],
            "mutually exclusive",
            id="enable-run-its",
        ),
        ErrCase(
            ["--disable-its", "rhoai-e2e-rh-nightly-pm-ocp420", "--run-its", "rhoai-e2e-rh-nightly-pm-ocp420"],
            "cannot be used with --disable-its",
            id="run-its-with-disable",
        ),
        ErrCase(
            [
                "--enable-its",
                "rhoai-e2e-rh-nightly-pm-ocp420",
                "--external-kubeconfig",
                "/etc/hosts",
            ],
            "accepts only Konflux rollout flags",
            id="enable-its-external-kubeconfig",
        ),
        ErrCase(
            [
                "--enable-its",
                "rhoai-e2e-rh-nightly-pm-ocp420",
                "--components",
                "dashboard_cypress",
            ],
            "accepts only Konflux rollout flags",
            id="enable-its-components",
        ),
        ErrCase(["-l", "--enable-its", "rhoai-e2e-ephc-ocp421"], "mutually exclusive", id="list-enable-its"),
        ErrCase(
            ["--enable-its", "not a valid name!"],
            "Invalid IntegrationTestScenario name",
            id="enable-its-bad-name",
        ),
        ErrCase(
            ["--enable-its", "does-not-exist"],
            "No in-tree ITS manifest",
            id="enable-its-unknown",
        ),
        ErrCase(
            ["--delete-pending-pipelines", "--tests", "smoke"],
            "Trigger/install options cannot be used",
            id="delete-tests",
        ),
        ErrCase(["-l", "--rhoai-channel", "x"], "Trigger/install options cannot be used", id="list-channel"),
        ErrCase(
            ["-l", "--ocp-channel", "nightly"],
            "Trigger/install options cannot be used",
            id="list-ocp-channel",
        ),
        ErrCase(
            ["-l", "--image", "quay.io/x@sha256:a"],
            "Trigger/install options cannot be used",
            id="list-image",
        ),
        ErrCase(["-w", "--ocp-version", "4.19"], "Trigger/install options cannot be used", id="watch-ocp-version"),
        ErrCase(
            ["-w", "--rhoai-version", "3.5", "--product", "rhoai"],
            "Trigger/install options cannot be used",
            id="watch-version",
        ),
        ErrCase(
            ["--list-supported-ocp", "--konflux-repo", "https://g/r.git"],
            "Trigger/install options cannot be used",
            id="list-supported-ocp-konflux-repo",
        ),
        ErrCase(["-l", "--tests", "bvt"], "Trigger/install options cannot be used", id="list-tests"),
        ErrCase(["-w", "--test-timeout", "10m"], "Trigger/install options cannot be used", id="watch-test-timeout"),
        ErrCase(["--cleanup"], "requires --external-kubeconfig", id="cleanup-without-kubeconfig"),
        ErrCase(["-l", "--cleanup"], "mutually exclusive", id="list-cleanup"),
        ErrCase(["--cleanup", "--delete-pending-pipelines"], "mutually exclusive", id="cleanup-delete"),
    ]


ARGPARSE_FAIL_CASES = [
    pytest.param(["--product", "invalid"], id="invalid-product"),
    pytest.param(["--product", "odh"], id="product-odh-removed"),
    pytest.param(["--bvt-env-only"], id="bvt-env-only"),
    pytest.param(["--channel", "beta"], id="removed-channel-flag"),
    pytest.param(["--ocp-channel", "fast"], id="invalid-ocp-channel"),
    pytest.param(["--cleanup", "maybe"], id="cleanup-invalid-value"),
]


@pytest.fixture(scope="module")
def parser():
    """Parser with prog matching rhoai_e2e.py entrypoint."""
    argv0, sys.argv[0] = sys.argv[0], str(_RHOAI_E2E_ROOT / "rhoai_e2e.py")
    try:
        yield make_parser()
    finally:
        sys.argv[0] = argv0
