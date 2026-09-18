"""Unit tests for ITS apply overrides on --enable-its."""

from __future__ import annotations

import argparse

import pytest

from runners.cli.cli import make_parser, parse_cli_args
from runners.cli.runner_mixin_its import RunnerItsAdminMixin
from suite.errors import AppError

def _resolve_its_apply_application(
    manifest_app: str, *, app: str, konflux_app_explicit: bool
) -> tuple[str, str]:
    runner = RunnerItsAdminMixin()
    runner.args = argparse.Namespace(app=app, konflux_app_explicit=konflux_app_explicit)
    return runner._resolve_its_apply_application(manifest_app)


@pytest.mark.parametrize(
    ("manifest_app", "cli_app", "explicit", "expected_apply", "expected_patch"),
    [
        ("rhoai-fbc-fragment-ocp-420", "testops-playpen", False, "rhoai-fbc-fragment-ocp-420", ""),
        ("rhoai-fbc-fragment-ocp-420", "testops-playpen", True, "testops-playpen", "testops-playpen"),
        ("rhoai-fbc-fragment-ocp-420", "rhoai-fbc-fragment-ocp-420", True, "rhoai-fbc-fragment-ocp-420", ""),
        ("", "testops-playpen", False, "testops-playpen", ""),
    ],
)
def test_resolve_its_apply_application(
    manifest_app: str,
    cli_app: str,
    explicit: bool,
    expected_apply: str,
    expected_patch: str,
) -> None:
    apply_app, patch = _resolve_its_apply_application(
        manifest_app, app=cli_app, konflux_app_explicit=explicit
    )
    assert apply_app == expected_apply
    assert patch == expected_patch


def test_resolve_its_apply_application_missing_app() -> None:
    with pytest.raises(AppError, match="missing spec.application"):
        _resolve_its_apply_application("", app="", konflux_app_explicit=False)


def test_enable_its_rejects_external_kubeconfig() -> None:
    parser = make_parser()
    with pytest.raises(AppError, match="accepts only Konflux rollout flags"):
        parse_cli_args(
            parser,
            [
                "--enable-its",
                "rhoai-e2e-rh-nightly-pm-ocp420",
                "--external-kubeconfig",
                "/etc/hosts",
            ],
        )
