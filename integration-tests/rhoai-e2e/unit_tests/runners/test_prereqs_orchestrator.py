"""Orchestration tests for component cluster prerequisites (no cluster)."""

from __future__ import annotations

import os
import tempfile
from pathlib import Path
from unittest.mock import patch

from runners.component_prereqs import (
    cluster_prep_already_done,
    mark_cluster_prep_done,
    prepare_component_for_smoke,
)
from runners.orchestrator import (
    _remove_staged_pyyaml_binaries,
    prepare_cluster_for_components,
    stage_git_for_prereqs,
    stage_which_shim,
)

def test_stage_which_shim_writes_executable(tmp_path, monkeypatch) -> None:
    payload = tmp_path / "tests-payload" / "results"
    payload.mkdir(parents=True)
    monkeypatch.setenv("ARTIFACTS_DIR", str(payload))
    stage_which_shim()
    which = tmp_path / "tests-payload" / ".tools" / "bin" / "which"
    assert which.is_file()
    assert which.stat().st_mode & 0o111
    assert which.read_text(encoding="utf-8").startswith("#!/bin/sh")

def test_stage_git_for_prereqs_stages_https_helper(tmp_path, monkeypatch) -> None:
    payload = tmp_path / "tests-payload" / "results"
    payload.mkdir(parents=True)
    monkeypatch.setenv("ARTIFACTS_DIR", str(payload))
    stage_git_for_prereqs()
    bindir = tmp_path / "tests-payload" / ".tools" / "bin"
    assert (bindir / "git").is_file()
    assert (bindir / "git-core" / "git-remote-https").is_file()
    assert os.environ.get("GIT_EXEC_PATH") == str(bindir / "git-core")

def test_prepare_skips_when_collect_only() -> None:
    with patch("runners.orchestrator.selected_component_ids") as sel:
        prepare_cluster_for_components(collect_only=True)
        sel.assert_not_called()

def test_prepare_skips_when_no_components_selected() -> None:
    with patch("runners.orchestrator.selected_component_ids", return_value=set()):
        with patch("runners.orchestrator.wait_gateway_config_ready", return_value=True):
            with patch("runners.orchestrator.prepare_components_for_smoke") as prep:
                with (
                    patch("runners.orchestrator.stage_git_for_prereqs"),
                    patch("runners.orchestrator.stage_oc_for_pytest"),
                    patch("runners.orchestrator.mark_cluster_prep_done") as mark,
                ):
                    prepare_cluster_for_components(collect_only=False)
                    prep.assert_not_called()
                    mark.assert_not_called()

def test_prepare_marks_cluster_prep_done(tmp_path, monkeypatch) -> None:
    payload = tmp_path / "tests-payload" / "results"
    payload.mkdir(parents=True)
    monkeypatch.setenv("ARTIFACTS_DIR", str(payload))
    with patch("runners.orchestrator.selected_component_ids", return_value={"dashboard_cypress"}):
        with (
            patch("runners.orchestrator.wait_gateway_config_ready", return_value=True),
            patch("runners.orchestrator.prepare_components_for_smoke") as prep,
            patch("runners.orchestrator.load_shift_left_env_from_mount"),
            patch("runners.orchestrator.stage_git_for_prereqs"),
            patch("runners.orchestrator.stage_oc_for_pytest"),
        ):
            prepare_cluster_for_components(collect_only=False)
            prep.assert_called_once()
    assert cluster_prep_already_done(payload)

def test_prepare_component_skips_after_marker(tmp_path, monkeypatch) -> None:
    payload = tmp_path / "results"
    payload.mkdir(parents=True)
    monkeypatch.setenv("ARTIFACTS_DIR", str(payload))
    mark_cluster_prep_done(payload)
    with patch("runners.component_prereqs.ensure_dsc_component_managed") as dsc:
        prepare_component_for_smoke("dashboard_cypress")
        dsc.assert_not_called()

def test_prepare_component_runs_pooled_cleanup_when_marker_set(tmp_path, monkeypatch) -> None:
    payload = tmp_path / "results"
    payload.mkdir(parents=True)
    monkeypatch.setenv("ARTIFACTS_DIR", str(payload))
    monkeypatch.setenv("CLUSTER_SOURCE", "rhoai-e2e-kubeconfig-psi-23")
    monkeypatch.setenv("PRODUCT", "")
    mark_cluster_prep_done(payload)
    with (
        patch("runners.component_prereqs.run_pooled_external_smoke_prep") as pooled,
        patch("runners.component_prereqs.ensure_dsc_component_managed") as dsc,
    ):
        prepare_component_for_smoke("ai_pipelines")
        pooled.assert_called_once_with("ai_pipelines")
        dsc.assert_not_called()

def test_remove_staged_pyyaml_binaries_drops_directory(tmp_path) -> None:
    target = tmp_path / ".tools" / "python"
    target.mkdir(parents=True)
    (target / "yaml" / "__init__.py").parent.mkdir(parents=True)
    (target / "yaml" / "__init__.py").write_text("# pkg\n", encoding="utf-8")
    stale_dir = target / "_yaml"
    stale_dir.mkdir()
    stale_file = target / "_yaml.cpython-39-x86_64-linux-gnu.so"
    stale_file.write_bytes(b"so")
    _remove_staged_pyyaml_binaries(target)
    assert not stale_dir.exists()
    assert not stale_file.exists()
    assert (target / "yaml" / "__init__.py").is_file()


def test_prepare_oc_binary_path_for_pytest_sets_oc_binary_path(tmp_path, monkeypatch) -> None:
    artifacts = tmp_path / "artifacts"
    tools_bin = artifacts / "tests-payload" / ".tools" / "bin"
    tools_bin.mkdir(parents=True)
    staged = tools_bin / "oc"
    staged.write_bytes(b"")
    staged.chmod(0o755)
    monkeypatch.setenv("ARTIFACTS_DIR", str(artifacts))
    monkeypatch.delenv("OC_BINARY_PATH", raising=False)
    prev_path = os.environ.get("PATH", "")
    from runners.orchestrator import prepare_oc_binary_path_for_pytest

    try:
        prepare_oc_binary_path_for_pytest()
        assert os.environ.get("OC_BINARY_PATH") == str(staged)
        assert str(tools_bin) in os.environ.get("PATH", "")
    finally:
        os.environ["PATH"] = prev_path
        os.environ.pop("OC_BINARY_PATH", None)
