"""Unit tests for cluster prep markers shared across Tekton tasks."""

from __future__ import annotations

from pathlib import Path

from steps.cluster_prep_state import (
    clear_cluster_api_unreachable_marker,
    cluster_api_unreachable_marker_reason,
    cluster_prep_already_done,
    dep_operators_already_done,
    maas_gateway_https_blocked_reason,
    maas_gateway_https_failed_reason,
    mark_cluster_api_unreachable,
    mark_cluster_prep_done,
    mark_dep_operators_done,
    mark_maas_gateway_https_failed,
)

def test_dep_operators_marker_skips_duplicate_rhcl(tmp_path, monkeypatch) -> None:
    payload = tmp_path / "tests-payload" / "results"
    monkeypatch.setenv("TESTS_SHARED", str(tmp_path))
    monkeypatch.setenv("PIPELINE_RUN_NAME", "pr-test-1")
    mark_dep_operators_done()
    assert dep_operators_already_done()
    assert not cluster_prep_already_done()

    mark_cluster_prep_done(payload)
    assert cluster_prep_already_done(payload)

def test_stale_marker_from_other_pipelinerun_ignored(tmp_path, monkeypatch) -> None:
    payload = tmp_path / "tests-payload" / "results"
    payload.mkdir(parents=True)
    monkeypatch.setenv("TESTS_SHARED", str(tmp_path))
    monkeypatch.setenv("PIPELINE_RUN_NAME", "pr-old")
    mark_dep_operators_done()
    monkeypatch.setenv("PIPELINE_RUN_NAME", "pr-new")
    assert not dep_operators_already_done()

def test_artifacts_dir_preferred_over_tests_shared(tmp_path, monkeypatch) -> None:
    artifacts = tmp_path / "artifacts"
    artifacts.mkdir()
    monkeypatch.setenv("ARTIFACTS_DIR", str(artifacts))
    monkeypatch.setenv("TESTS_SHARED", str(tmp_path / "other"))
    mark_dep_operators_done()
    assert (artifacts / ".dep-operators-done").is_file()


def test_maas_gateway_https_failed_marker_blocks_repeat_waits(tmp_path, monkeypatch) -> None:
    payload = tmp_path / "tests-payload" / "results"
    monkeypatch.setenv("TESTS_SHARED", str(tmp_path))
    monkeypatch.setenv("PIPELINE_RUN_NAME", "pr-maas-1")
    mark_maas_gateway_https_failed("MaaS gateway HTTPS service not ready after 480s")
    assert "480s" in maas_gateway_https_failed_reason()
    assert "480s" in maas_gateway_https_blocked_reason()


def test_maas_gateway_https_blocked_reason_prefixes_unmarked_prior_failure(tmp_path, monkeypatch) -> None:
    payload = tmp_path / "tests-payload" / "results"
    monkeypatch.setenv("TESTS_SHARED", str(tmp_path))
    monkeypatch.setenv("PIPELINE_RUN_NAME", "pr-maas-2")
    mark_maas_gateway_https_failed("gateway wait timed out after 480s")
    reason = maas_gateway_https_blocked_reason()
    assert reason.startswith("MaaS gateway HTTPS service not ready")
    assert "gateway wait timed out after 480s" in reason


def test_cluster_api_unreachable_marker_scoped_to_pipelinerun(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("TESTS_SHARED", str(tmp_path))
    monkeypatch.setenv("PIPELINE_RUN_NAME", "pr-api-1")
    mark_cluster_api_unreachable("cluster API unreachable: dial tcp: lookup elb.example")
    assert "elb.example" in cluster_api_unreachable_marker_reason()
    clear_cluster_api_unreachable_marker()
    assert cluster_api_unreachable_marker_reason() == ""
    monkeypatch.setenv("PIPELINE_RUN_NAME", "pr-api-2")
    assert cluster_api_unreachable_marker_reason() == ""


def test_maas_gateway_https_blocked_clears_when_live_stack_ready(tmp_path, monkeypatch) -> None:
    payload = tmp_path / "tests-payload" / "results"
    payload.mkdir(parents=True)
    monkeypatch.setenv("TESTS_SHARED", str(tmp_path))
    monkeypatch.setenv("PIPELINE_RUN_NAME", "pr-maas-live")
    incomplete = payload / ".gateway-auth-stack-incomplete"
    incomplete.write_text("rhcl post-install retry failed\n", encoding="utf-8")
    monkeypatch.setattr(
        "components.maas_billing.auth.maas_gateway_auth_stack_live_ready",
        lambda: True,
    )
    assert maas_gateway_https_blocked_reason() == ""
    assert not incomplete.is_file()
