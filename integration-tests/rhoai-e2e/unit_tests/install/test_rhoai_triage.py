"""Unit tests for RHOAI triage diagnostics."""

from __future__ import annotations

import pytest

from steps.rhoai_triage import (
    ISSUE_GREP_EXCLUDE_RE,
    ISSUE_GREP_RE,
    _latest_pod_per_prefix,
    build_issues_summary,
    needs_dependency_install_diagnostics,
    resolve_logs_since_time,
    ODH_RHOAI_WORKLOAD_NS_RE,
)

def test_resolve_logs_since_time_accepts_rfc3339() -> None:
    assert resolve_logs_since_time("2026-06-22T10:15:30Z") == "2026-06-22T10:15:30Z"
    assert resolve_logs_since_time("2026-06-22T10:15:30.123Z") == "2026-06-22T10:15:30.123Z"

def test_resolve_logs_since_time_requires_value(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("PIPELINE_RUN_START_TIME", raising=False)
    monkeypatch.setattr(
        "steps.tekton_incluster.pipeline_run_creation_timestamp",
        lambda *a, **k: "",
    )
    with pytest.raises(ValueError, match="could not be loaded"):
        resolve_logs_since_time(None)

def test_resolve_logs_since_time_fetches_when_tekton_var_unsubstituted(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(
        "PIPELINE_RUN_START_TIME", "$(context.pipelineRun.creationTimestamp)"
    )
    monkeypatch.setattr(
        "steps.tekton_incluster.pipeline_run_creation_timestamp",
        lambda *a, **k: "2026-06-22T10:00:00Z",
    )
    assert resolve_logs_since_time() == "2026-06-22T10:00:00Z"

def test_resolve_logs_since_time_falls_back_when_unexpanded(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "steps.tekton_incluster.pipeline_run_creation_timestamp",
        lambda *a, **k: "",
    )
    result = resolve_logs_since_time("$(tasks.init.results.startTime)")
    assert result.endswith("Z")

def test_resolve_logs_since_time_raises_when_invalid(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "steps.tekton_incluster.pipeline_run_creation_timestamp",
        lambda *a, **k: "",
    )
    with pytest.raises(ValueError, match="invalid PIPELINE_RUN_START_TIME"):
        resolve_logs_since_time("not-a-date")

def test_needs_dependency_install_diagnostics(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("INSTALL_DEP_OPERATORS_STATUS", raising=False)
    monkeypatch.setattr(
        "steps.rhoai_triage.odh_rhoai_namespaces",
        lambda: ["redhat-ods-operator"],
    )
    monkeypatch.setattr(
        "steps.rhoai_triage._oc_json",
        lambda args: {"items": [{"metadata": {"name": "rhoai"}}]},
    )
    assert not needs_dependency_install_diagnostics("redhat-ods-operator")

    monkeypatch.setattr("steps.rhoai_triage._oc_json", lambda args: {"items": []})
    assert needs_dependency_install_diagnostics("redhat-ods-operator")

    monkeypatch.setattr("steps.rhoai_triage.odh_rhoai_namespaces", lambda: [])
    assert needs_dependency_install_diagnostics("redhat-ods-operator")

    monkeypatch.setenv("INSTALL_DEP_OPERATORS_STATUS", "Failed")
    assert needs_dependency_install_diagnostics("redhat-ods-operator")

def test_print_triage_to_step_log(tmp_path, capsys: pytest.CaptureFixture[str]) -> None:
    from steps.collect_diagnostics import _print_triage_to_step_log

    diag = tmp_path / "diag"
    (diag / "triage").mkdir(parents=True)
    (diag / "triage" / "issues-summary.txt").write_text("ProvisioningFailed\n", encoding="utf-8")
    (diag / "triage" / "status-report.txt").write_text("STATUS LINE\n", encoding="utf-8")

    _print_triage_to_step_log(diag, artifact_name="rhoai-diagnostic-2026.log")
    out = capsys.readouterr().out
    assert "COLLECT-DIAGNOSTICS REPORT" in out
    assert "ISSUES SUMMARY" in out
    assert "ProvisioningFailed" in out
    assert "STATUS LINE" in out

def test_latest_pod_per_prefix_picks_newest() -> None:
    pods_json = {
        "items": [
            {
                "metadata": {
                    "namespace": "redhat-ods-applications",
                    "name": "dashboard-abc123",
                    "creationTimestamp": "2026-01-01T00:00:00Z",
                }
            },
            {
                "metadata": {
                    "namespace": "redhat-ods-applications",
                    "name": "dashboard-def456",
                    "creationTimestamp": "2026-01-02T00:00:00Z",
                }
            },
            {
                "metadata": {
                    "namespace": "redhat-ods-operator",
                    "name": "rhods-operator-xyz",
                    "creationTimestamp": "2026-01-01T00:00:00Z",
                }
            },
            {
                "metadata": {
                    "namespace": "redhat-ods-applications",
                    "name": "singlepod",
                    "creationTimestamp": "2026-01-03T00:00:00Z",
                }
            },
        ]
    }
    selected = _latest_pod_per_prefix(pods_json, ODH_RHOAI_WORKLOAD_NS_RE)
    assert selected == [("redhat-ods-applications", "dashboard-def456")]

def test_build_issues_summary_filters_noise(tmp_path) -> None:
    root = tmp_path / "triage"
    root.mkdir()
    status = root / "status.txt"
    status.write_text(
        "DSC Ready summary:\n"
        "Ready: status=False reason=ProvisioningFailed message=component failed\n"
        "Registering webhook handler\n",
        encoding="utf-8",
    )
    events = root / "events.txt"
    events.write_text(
        "LAST SEEN   TYPE      REASON\n"
        "1m          Warning   FailedMount\n",
        encoding="utf-8",
    )
    highlights = root / "highlights.txt"
    highlights.write_text(
        "Reconciler error: could not sync deployment\n",
        encoding="utf-8",
    )
    out = root / "issues.txt"
    text = build_issues_summary(
        out,
        status_report=status,
        events=events,
        operator_highlights=highlights,
        max_lines=10,
    )
    assert "ProvisioningFailed" in text
    assert "FailedMount" in text
    assert "Reconciler error" in text
    assert "Registering webhook" not in text
    assert ISSUE_GREP_RE.search("phase=Failed")
    assert ISSUE_GREP_EXCLUDE_RE.search("Registering webhook")

def test_collect_diagnostics_pipeline_failed(monkeypatch: pytest.MonkeyPatch) -> None:
    from steps.collect_diagnostics import (
        _install_task_failed,
        _pipeline_failed,
        _resolve_kubeconfig,
        _should_collect_adm_inspect,
        _stage_triage_for_artifacts,
    )

    monkeypatch.delenv("PIPELINE_RUN_STATUS", raising=False)
    monkeypatch.delenv("INSTALL_OPERATOR_RHOAI_STATUS", raising=False)
    assert not _pipeline_failed()
    assert not _should_collect_adm_inspect()

    monkeypatch.setenv("PIPELINE_RUN_STATUS", "Failed")
    assert _pipeline_failed()
    assert _should_collect_adm_inspect()

    monkeypatch.setenv("PIPELINE_RUN_STATUS", "Succeeded")
    monkeypatch.setenv("INSTALL_OPERATOR_RHOAI_STATUS", "Failed")
    assert _install_task_failed()
    assert _should_collect_adm_inspect()

def test_resolve_kubeconfig_prefers_existing_file(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    from steps.collect_diagnostics import _resolve_kubeconfig

    creds = tmp_path / "credentials"
    creds.mkdir()
    staged = creds / "kubeconfig"
    staged.write_text("kubeconfig", encoding="utf-8")

    monkeypatch.setenv("KUBECONFIG", str(tmp_path / "missing"))
    monkeypatch.setenv("TESTS_SHARED_DIR", str(tmp_path))
    monkeypatch.setattr(
        "steps.collect_diagnostics._DEFAULT_KUBECONFIG",
        tmp_path / "also-missing",
    )
    assert _resolve_kubeconfig() == str(staged)

def test_diagnostic_artifact_log_name(monkeypatch: pytest.MonkeyPatch) -> None:
    from steps.collect_diagnostics import _diagnostic_artifact_log_name

    monkeypatch.setattr(
        "steps.collect_diagnostics.cluster_label_from_kubeconfig",
        lambda _k: "ods-qe-psi-07",
    )
    assert (
        _diagnostic_artifact_log_name(
            since_time="2026-06-24T11:25:10Z",
            pipeline_product="",
            operator_name="rhods-operator",
            operator_version="2.4.1",
            kubeconfig="/tmp/kubeconfig",
        )
        == "rhoai-2.4.1-ods-qe-psi-07-diagnostic-2026-06-24T112510Z.log"
    )

def test_stage_triage_for_artifacts_writes_single_log(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    from steps.collect_diagnostics import _stage_triage_for_artifacts

    shared = tmp_path / "tests-shared"
    diag = tmp_path / "diag"
    (diag / "triage" / "operator-logs").mkdir(parents=True)
    (diag / "triage" / "workload-logs" / "redhat-ods-applications").mkdir(parents=True)
    (diag / "triage" / "issues-summary.txt").write_text("ProvisioningFailed\n", encoding="utf-8")
    (diag / "triage" / "status-report.txt").write_text("DSC Ready summary\n", encoding="utf-8")
    (diag / "triage" / "operator-logs" / "rhods-operator-abc.log").write_text(
        "operator error line\n", encoding="utf-8"
    )
    (diag / "triage" / "workload-logs" / "redhat-ods-applications" / "dash.log").write_text(
        "workload log line\n", encoding="utf-8"
    )

    monkeypatch.setenv("TESTS_SHARED_DIR", str(shared))
    monkeypatch.setattr(
        "steps.collect_diagnostics.cluster_label_from_kubeconfig",
        lambda _k: "",
    )
    staged = _stage_triage_for_artifacts(
        diag,
        since_time="2026-06-22T10:00:00Z",
        pipeline_product="",
        operator_name="rhods-operator",
        operator_version="2.4.1",
        kubeconfig=str(tmp_path / "kubeconfig"),
    )
    assert staged is not None
    assert staged.name == "rhoai-2.4.1-diagnostic-2026-06-22T100000Z.log"
    body = staged.read_text(encoding="utf-8")
    assert "ISSUES SUMMARY" in body
    assert "OPERATOR POD LOG" in body
    assert "WORKLOAD POD LOG" in body
    assert "operator error line" in body
    assert "workload log line" in body

def test_collect_diagnostics_main_fails_without_kubeconfig(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from steps import collect_diagnostics as mod

    tekton_results = tmp_path / "tekton-results"
    tekton_results.mkdir()
    result_file = tekton_results / "manifest.txt"
    monkeypatch.setenv("TEKTON_RESULTS_DIR", str(tekton_results))
    monkeypatch.setenv("OPERATOR_NAMESPACE", "redhat-ods-operator")
    monkeypatch.setenv("DIAG_MANIFEST_RESULT", str(result_file))
    monkeypatch.setenv("DIAG_DIR", str(tmp_path / "diag"))
    monkeypatch.setenv("PIPELINE_RUN_START_TIME", "2026-06-22T10:00:00Z")
    monkeypatch.setenv("TESTS_SHARED_DIR", str(tmp_path / "shared"))
    monkeypatch.setattr(mod, "_DEFAULT_KUBECONFIG", tmp_path / "missing-kubeconfig")

    assert mod.main() == 1
    assert "error:" in result_file.read_text(encoding="utf-8")
    from steps.tests_payload import COLLECT_DIAGNOSTICS_DONE_MARKER

    marker = tmp_path / "shared" / "tests-payload" / COLLECT_DIAGNOSTICS_DONE_MARKER
    assert marker.is_file()
    assert "failed" in marker.read_text(encoding="utf-8")
