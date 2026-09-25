"""CLI argument parsing tests (no cluster)."""

from __future__ import annotations

import contextlib
import io
from pathlib import Path

import pytest

from runners.cli.cli import parse_cli_args
from suite.errors import AppError
from unit_tests.runners.rhoai_e2e_cli_fixtures import (
    ARGPARSE_FAIL_CASES,
    OkCase,
    ErrCase,
    apply_env,
    err_cases,
    ok_cases,
)

@pytest.mark.parametrize("case", ok_cases(), ids=lambda c: c.id)
def test_parse_cli_args_ok(case: OkCase, parser, monkeypatch: pytest.MonkeyPatch) -> None:
    apply_env(monkeypatch, case.env)
    args = parse_cli_args(parser, case.argv)
    if case.check:
        case.check(args)

@pytest.mark.parametrize("case", err_cases(), ids=lambda c: c.id)
def test_parse_cli_args_err(case: ErrCase, parser, monkeypatch: pytest.MonkeyPatch) -> None:
    apply_env(monkeypatch, case.env)
    with pytest.raises(AppError, match=case.substr):
        parse_cli_args(parser, case.argv)

@pytest.mark.parametrize("argv", ARGPARSE_FAIL_CASES)
def test_parse_cli_args_argparse_fail(argv: list[str], parser) -> None:
    buf = io.StringIO()
    with contextlib.redirect_stderr(buf):
        with pytest.raises(SystemExit) as exc_info:
            parse_cli_args(parser, argv)
    assert exc_info.value.code == 2

def test_external_kubeconfig_ok_and_err(tmp_path: Path, parser, monkeypatch: pytest.MonkeyPatch) -> None:
    kubeconfig = tmp_path / "kubeconfig"
    kubeconfig.write_text("apiVersion: v1\nkind: Config\n", encoding="utf-8")
    kc = str(kubeconfig)

    args = parse_cli_args(parser, ["--external-kubeconfig", kc, "--tests", "bvt"])
    assert args.external_kubeconfig_path is not None
    assert args.product == ""

    args = parse_cli_args(parser, ["--external-kubeconfig-secret", "my-kubeconfig-secret", "--tests", "smoke"])
    assert args.external_kubeconfig_secret == "my-kubeconfig-secret"

    with pytest.raises(AppError, match="mutually exclusive"):
        parse_cli_args(
            parser,
            ["--external-kubeconfig", kc, "--external-kubeconfig-secret", "x"],
        )

    with pytest.raises(AppError, match="test-only runs skip FBC"):
        parse_cli_args(parser, ["--external-kubeconfig", kc, "--ocp-version", "4.20"])

    args = parse_cli_args(
        parser,
        ["--external-kubeconfig", kc, "--ocp-version", "4.21", "--product", "rhoai", "--rhoai-version", "3.5-ea.2"],
    )
    assert args.ocp_version == "4.21"
    assert args.product == "rhoai"

    with pytest.raises(AppError, match="must be an existing file"):
        parse_cli_args(parser, ["--external-kubeconfig", "/no/such/kubeconfig"])

    with pytest.raises(AppError, match="Trigger/install options cannot be used"):
        parse_cli_args(parser, ["-w", "--external-kubeconfig", kc])

    with pytest.raises(AppError, match="Trigger/install options cannot be used"):
        parse_cli_args(parser, ["--external-kubeconfig", kc, "--cleanup", "--tests", "smoke"])

    with pytest.raises(AppError, match="Trigger/install options cannot be used"):
        parse_cli_args(parser, ["--external-kubeconfig", kc, "--cleanup"])

    args = parse_cli_args(
        parser,
        ["--external-kubeconfig", kc, "--product", "rhoai", "--tests", "smoke"],
    )
    assert args.cleanup is True
    assert args.cleanup_maintenance is False

    args = parse_cli_args(
        parser,
        ["--external-kubeconfig", kc, "--product", "rhoai", "--tests", "smoke", "--cleanup", "false"],
    )
    assert args.cleanup is False
    assert args.cleanup_opt_out is True

    with pytest.raises(AppError, match="Trigger/install options cannot be used"):
        parse_cli_args(
            parser,
            [
                "--external-kubeconfig",
                kc,
                "--install-dependencies",
                "--tests",
                "smoke",
                "--cleanup",
            ],
        )

def test_install_dependencies_validation(tmp_path: Path, parser, monkeypatch: pytest.MonkeyPatch) -> None:
    kubeconfig = tmp_path / "kubeconfig"
    kubeconfig.write_text("apiVersion: v1\nkind: Config\n", encoding="utf-8")
    kc = str(kubeconfig)
    base = ["--external-kubeconfig", kc, "--tests", "smoke"]

    args = parse_cli_args(parser, [*base, "--install-dependencies"])
    assert args.install_dependencies is True

    with pytest.raises(AppError, match="requires test-only mode"):
        parse_cli_args(parser, ["--product", "rhoai", "--install-dependencies", "--tests", "smoke"])

    with pytest.raises(AppError, match="requires --tests smoke"):
        parse_cli_args(parser, ["--external-kubeconfig", kc, "--install-dependencies", "--tests", "bvt"])

    with pytest.raises(AppError, match="requires --external-kubeconfig"):
        parse_cli_args(parser, ["--install-dependencies", "--tests", "smoke"])


def test_quay_pull_secret_name_defaults_without_flag(parser) -> None:
    from suite.constants import DEFAULT_QUAY_PULL_SECRET_NAME

    args = parse_cli_args(parser, ["--product", "rhoai"])
    assert args.quay_pull_secret_name == DEFAULT_QUAY_PULL_SECRET_NAME
    assert args.quay_pull_secret_explicit is False


def test_quay_pull_secret_name_explicit_flag(parser) -> None:
    args = parse_cli_args(parser, ["--product", "rhoai", "--quay-pull-secret-name", "custom-quay"])
    assert args.quay_pull_secret_name == "custom-quay"
    assert args.quay_pull_secret_explicit is True

def test_format_test_output_for_ui() -> None:
    from runners.report.junit_suite_report import format_test_output_for_ui

    sample = (
        '{"result":"FAILURE","note":"Smoke: 19/20 passed (95% pass rate), 1 failed, 0 skipped",'
        '"suites":[{"id":"workbenches-smoke","name":"Workbenches","total":5,"passed":4,"failed":1,"skipped":0},'
        '{"id":"model-registry-smoke","name":"Model Registry","total":4,"passed":4,"failed":0,"skipped":0}]}'
    )
    formatted = format_test_output_for_ui(sample)
    assert formatted.splitlines()[0] == "smoke: 89% pass rate (8 passed, 1 failed, 0 skipped)"
    assert "workbenches: 4 passed, 1 failed, 0 skipped" not in formatted
    assert formatted.count("\n") == 0

def test_format_test_outputs_for_ui() -> None:
    from runners.report.junit_suite_report import format_test_outputs_for_ui

    merged = format_test_outputs_for_ui(
        [
            (
                "bvt",
                '{"note":"BVT:","suites":[{"id":"cluster-health","name":"Cluster Health","total":1,"passed":1,"failed":0,"skipped":0}]}',
            ),
            (
                "smoke",
                '{"note":"Smoke:","suites":[{"id":"workbenches-smoke","name":"Workbenches","total":5,"passed":4,"failed":1,"skipped":0}]}',
            ),
        ]
    )
    assert "bvt: 100% pass rate" in merged
    assert "smoke: 80% pass rate" in merged
    assert "workbenches: 4 passed, 1 failed, 0 skipped" not in merged
    assert merged.count("\n") == 1

def test_format_test_outputs_for_ui_dedupes_combined_finalize_note() -> None:
    from runners.report.junit_suite_report import format_test_outputs_for_ui

    combined = (
        '{"note":"bvt: 100% pass rate (5 passed, 0 failed, 0 skipped)\\n'
        'smoke: 100% pass rate (2 passed, 0 failed, 0 skipped)"}'
    )
    merged = format_test_outputs_for_ui(
        [
            ("bvt", '{"note":"bvt: 100% pass rate (5 passed, 0 failed, 0 skipped)"}'),
            ("smoke", combined),
        ]
    )
    lines = merged.splitlines()
    assert lines == [
        "bvt: 100% pass rate (5 passed, 0 failed, 0 skipped)",
        "smoke: 100% pass rate (2 passed, 0 failed, 0 skipped)",
    ]

def test_format_human_results_text_component_lines() -> None:
    from runners.report.junit_suite_report import format_human_results_text

    raw = (
        '{"note":"smoke: 80% pass rate (4 passed, 1 failed, 0 skipped)",'
        '"suites":[{"id":"workbenches-smoke","passed":4,"failed":1,"skipped":0,"total":5}]}'
    )
    text = format_human_results_text(raw)
    assert text.splitlines() == [
        "smoke: 80% pass rate (4 passed, 1 failed, 0 skipped)",
        "workbenches: 4 passed, 1 failed, 0 skipped",
    ]

def test_list_pipeline_test_outputs_for_ui_prefers_finalize() -> None:
    import json

    from runners.report.pipelinerun_summary import list_pipeline_test_outputs

    combined = json.dumps(
        {
            "note": "bvt: 100% pass rate (5 passed, 0 failed, 0 skipped)\n"
            "smoke: 100% pass rate (2 passed, 0 failed, 0 skipped)",
        }
    )
    bvt_only = json.dumps({"note": "bvt: 100% pass rate (5 passed, 0 failed, 0 skipped)"})
    taskruns = [
        {
            "metadata": {"labels": {"tekton.dev/pipelineTask": "bvt-health-checks"}},
            "status": {"results": [{"name": "TEST_OUTPUT", "value": bvt_only}]},
        },
        {
            "metadata": {"labels": {"tekton.dev/pipelineTask": "test-finalize"}},
            "status": {"results": [{"name": "TEST_OUTPUT", "value": combined}]},
        },
    ]
    assert list_pipeline_test_outputs(taskruns, for_ui=True) == [("combined", combined)]
    assert len(list_pipeline_test_outputs(taskruns)) == 2

def test_suites_by_component_id_and_format_suite_stats_compact() -> None:
    from runners.report.junit_suite_report import format_suite_stats_compact, suites_by_component_id

    sample = '{"suites":[{"id":"workbenches-smoke","passed":4,"failed":1,"skipped":0}]}'
    wb = suites_by_component_id(sample).get("workbenches")
    assert wb is not None
    assert format_suite_stats_compact(wb) == "4 passed, 1 failed, 0 skipped"

def test_smoke_component_result_label() -> None:
    from runners.report.junit_suite_report import (
        DISABLED_LABEL,
        NOT_RUN_LABEL,
        NO_RESULTS_LABEL,
        smoke_component_result_label,
    )

    suite = {"passed": 4, "failed": 1, "skipped": 0, "total": 5}
    assert (
        smoke_component_result_label(
            "workbenches",
            smoke_in_gates=True,
            selected_ids=frozenset({"workbenches"}),
            disabled_ids=frozenset(),
            suite=suite,
        )
        == "4 passed, 1 failed, 0 skipped"
    )
    assert (
        smoke_component_result_label(
            "ogx",
            smoke_in_gates=True,
            selected_ids=frozenset({"model_server"}),
            disabled_ids=frozenset(),
            suite=None,
        )
        == NOT_RUN_LABEL
    )
    assert (
        smoke_component_result_label(
            "ogx",
            smoke_in_gates=True,
            selected_ids=frozenset({"ogx", "model_server"}),
            disabled_ids=frozenset(),
            suite=None,
        )
        == NO_RESULTS_LABEL
    )
    assert (
        smoke_component_result_label(
            "ogx",
            smoke_in_gates=False,
            selected_ids=frozenset({"ogx"}),
            disabled_ids=frozenset(),
            suite=None,
        )
        == NOT_RUN_LABEL
    )
    assert (
        smoke_component_result_label(
            "ogx",
            smoke_in_gates=True,
            selected_ids=frozenset({"ogx"}),
            disabled_ids=frozenset({"ogx"}),
            suite=None,
        )
        == DISABLED_LABEL
    )

def test_smoke_component_result_lines_and_run_summary_block() -> None:
    from runners.report.junit_suite_report import format_run_summary_block, smoke_component_result_lines

    comp_lines = smoke_component_result_lines(
        '{"suites":[{"id":"workbenches-smoke","passed":5,"failed":0,"skipped":0,"total":5}]}',
        smoke_in_gates=True,
        selected_ids=frozenset({"workbenches", "ogx"}),
        disabled_ids=frozenset(),
    )
    assert comp_lines[0] == "workbenches: 5 passed, 0 failed, 0 skipped"
    ogx_line = next((ln for ln in comp_lines if ln.startswith("ogx: ")), "")
    assert ogx_line == "ogx: N/A (no results)"
    block = format_run_summary_block(
        pipeline_run_name="pr-1",
        cluster="ods-qe-psi-23",
        test_status="Failed",
        test_output="bvt: 100% pass rate (5 passed, 0 failed, 0 skipped)\nsmoke: 98% pass rate (43 passed, 1 failed, 0 skipped)",
        smoke_component_lines=comp_lines,
    )
    assert "PIPELINE_RUN_NAME: pr-1" in block
    assert "TEST_OUTPUT:" in block
    assert "bvt: 100% pass rate" in block
    assert "workbenches: 5 passed" in block

def test_collect_diagnostics_helpers(monkeypatch: pytest.MonkeyPatch) -> None:
    from steps.collect_diagnostics import _install_ran, _install_task_failed, _truthy

    monkeypatch.setenv("PRODUCT", "rhoai")
    assert _install_ran()
    monkeypatch.setenv("INSTALL_OPERATOR_STATUS", "Failed")
    assert _install_task_failed()
    assert _truthy("true")
    assert not _truthy("false", default=True)
