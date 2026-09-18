"""Tests for ITS manifest registry (no cluster)."""

from __future__ import annotations

from pathlib import Path

import pytest

from suite.errors import AppError
from suite.its_registry import (
    integration_test_scenario_application,
    integration_test_scenario_default_konflux_app,
    list_integration_test_scenario_manifests,
    looks_like_its_manifest_path,
    resolve_integration_test_scenario_manifest,
    resolve_integration_test_scenario_manifest_path,
    resolve_integration_test_scenario_ref,
    resolve_integration_test_scenario_run_its_snapshot,
    validate_integration_test_scenario_name,
)

_ROOT = Path(__file__).resolve().parents[2]
_RH_NIGHTLY_REL = "integration-tests/rhoai-e2e/tekton/its/its-rhoai-e2e-rh-nightly-pm-ocp420.yaml"
_RH_NIGHTLY_SHORT = "tekton/its/its-rhoai-e2e-rh-nightly-pm-ocp420.yaml"


def test_validate_integration_test_scenario_name_ok() -> None:
    assert validate_integration_test_scenario_name("rhoai-e2e-ephc-ocp421") == (
        "rhoai-e2e-ephc-ocp421"
    )


def test_validate_integration_test_scenario_name_rejects_empty() -> None:
    with pytest.raises(AppError, match="non-empty"):
        validate_integration_test_scenario_name("  ")


def test_looks_like_its_manifest_path() -> None:
    assert looks_like_its_manifest_path(_RH_NIGHTLY_REL)
    assert looks_like_its_manifest_path(_RH_NIGHTLY_SHORT)
    assert not looks_like_its_manifest_path("rhoai-e2e-rh-nightly-pm-ocp420")


def test_resolve_ephc_manifest() -> None:
    path = resolve_integration_test_scenario_manifest(_ROOT, "rhoai-e2e-ephc-ocp421")
    assert path.name == "its-rhoai-e2e-ephc-ocp421.yaml"
    assert integration_test_scenario_application(path) == "rhoai-fbc-fragment-ocp-421"


def test_resolve_rh_nightly_manifest() -> None:
    path = resolve_integration_test_scenario_manifest(
        _ROOT,
        "rhoai-e2e-rh-nightly-pm-ocp420",
    )
    assert path.name == "its-rhoai-e2e-rh-nightly-pm-ocp420.yaml"
    assert integration_test_scenario_application(path) == "rhoai-fbc-fragment-ocp-420"


def test_resolve_manifest_path_repo_relative(monkeypatch: pytest.MonkeyPatch) -> None:
    repo_root = _ROOT.resolve().parent.parent
    monkeypatch.chdir(repo_root)
    path = resolve_integration_test_scenario_manifest_path(_ROOT, _RH_NIGHTLY_REL)
    assert path.name == "its-rhoai-e2e-rh-nightly-pm-ocp420.yaml"


def test_resolve_manifest_path_explicit_wrong_cwd_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.chdir(_ROOT)
    with pytest.raises(AppError, match="ITS manifest not found"):
        resolve_integration_test_scenario_manifest_path(
            _ROOT,
            "./integration-tests/rhoai-e2e/tekton/its/its-rhoai-e2e-rh-nightly-pm-ocp420.yaml",
        )


def test_resolve_manifest_path_rhoai_e2e_relative() -> None:
    path = resolve_integration_test_scenario_manifest_path(_ROOT, _RH_NIGHTLY_SHORT)
    assert path.name == "its-rhoai-e2e-rh-nightly-pm-ocp420.yaml"


def test_resolve_ref_from_path_returns_metadata_name() -> None:
    manifest, name = resolve_integration_test_scenario_ref(_ROOT, _RH_NIGHTLY_SHORT)
    assert manifest.name == "its-rhoai-e2e-rh-nightly-pm-ocp420.yaml"
    assert name == "rhoai-e2e-rh-nightly-pm-ocp420"


def test_resolve_manifest_path_rejects_missing_file() -> None:
    with pytest.raises(AppError, match="ITS manifest not found"):
        resolve_integration_test_scenario_manifest_path(
            _ROOT,
            "integration-tests/rhoai-e2e/tekton/its/does-not-exist.yaml",
        )


def test_rh_nightly_default_konflux_app() -> None:
    assert (
        integration_test_scenario_default_konflux_app("rhoai-e2e-rh-nightly-pm-ocp420")
        == "rhoai-fbc-fragment-ocp-420"
    )
    assert integration_test_scenario_default_konflux_app("rhoai-e2e-ephc-ocp421") == (
        "rhoai-fbc-fragment-ocp-421"
    )


def test_resolve_run_its_snapshot_rh_nightly() -> None:
    path = resolve_integration_test_scenario_run_its_snapshot(
        _ROOT,
        "rhoai-e2e-rh-nightly-pm-ocp420",
    )
    assert path is not None
    assert path.name == "test-snapshot-rh-nightly.yaml"


def test_resolve_run_its_snapshot_unsupported_returns_none() -> None:
    assert (
        resolve_integration_test_scenario_run_its_snapshot(_ROOT, "rhoai-e2e-ephc-ocp421")
        is None
    )


def test_its_manifest_param_reads_product() -> None:
    path = _ROOT / "tekton" / "its" / "its-rhoai-e2e-rh-nightly-pm-ocp420.yaml"
    from suite.its_registry import its_manifest_param

    assert its_manifest_param(path, "PRODUCT") == "rhoai"


def test_resolve_manifest_path_absolute_under_repo() -> None:
    abs_path = _ROOT / "tekton" / "its" / "its-rhoai-e2e-rh-nightly-pm-ocp420.yaml"
    path = resolve_integration_test_scenario_manifest_path(_ROOT, str(abs_path))
    assert path == abs_path.resolve()


def test_resolve_manifest_path_cwd_relative(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    source = _ROOT / "tekton" / "its" / "its-rhoai-e2e-rh-nightly-pm-ocp420.yaml"
    copy = tmp_path / "my-its.yaml"
    copy.write_text(source.read_text(encoding="utf-8"), encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    path = resolve_integration_test_scenario_manifest_path(_ROOT, "my-its.yaml")
    assert path == copy.resolve()


def test_resolve_manifest_path_absolute_outside_repo(tmp_path: Path) -> None:
    source = _ROOT / "tekton" / "its" / "its-rhoai-e2e-rh-nightly-pm-ocp420.yaml"
    copy = tmp_path / "its-copy.yaml"
    copy.write_text(source.read_text(encoding="utf-8"), encoding="utf-8")
    path = resolve_integration_test_scenario_manifest_path(_ROOT, str(copy))
    assert path == copy.resolve()


def test_resolve_manifest_path_absolute_missing_fails_fast() -> None:
    with pytest.raises(AppError, match="ITS manifest not found"):
        resolve_integration_test_scenario_manifest_path(_ROOT, "/no/such/its-manifest.yaml")


def test_resolve_manifest_path_rejects_escape() -> None:
    with pytest.raises(AppError, match="stay under repository root"):
        resolve_integration_test_scenario_manifest_path(
            _ROOT,
            "tekton/its/../../../../../etc/passwd",
        )


def test_resolve_unknown_manifest() -> None:
    with pytest.raises(AppError, match="No in-tree ITS manifest"):
        resolve_integration_test_scenario_manifest(_ROOT, "does-not-exist")


def test_list_manifests_includes_playpen_its() -> None:
    names = list_integration_test_scenario_manifests(_ROOT)
    assert "rhoai-e2e-ephc-ocp421" in names
    assert "rhoai-e2e-ephc-ocp422" in names
    assert "rhoai-e2e-rh-nightly-pm-ocp420" in names
    assert "rhoai-e2e-ephc-playpen-a" in names
    assert "rhoai-e2e-ephc-playpen-b" in names
    assert "rhoai-e2e-ephc-ocp420-a" not in names
    assert "rhoai-e2e-ephc-ocp420-b" not in names
    assert "rhoai-e2e-ephc-ocp422-a" not in names
    assert "rhoai-e2e-ephc-ocp422-b" not in names


def test_resolve_ephc_playpen_slice_manifests() -> None:
    path_a = resolve_integration_test_scenario_manifest(_ROOT, "rhoai-e2e-ephc-playpen-a")
    path_b = resolve_integration_test_scenario_manifest(_ROOT, "rhoai-e2e-ephc-playpen-b")
    assert path_a.name == "its-rhoai-e2e-ephc-playpen-a.yaml"
    assert path_b.name == "its-rhoai-e2e-ephc-playpen-b.yaml"
    assert integration_test_scenario_application(path_a) == "testops-playpen"
    assert integration_test_scenario_application(path_b) == "testops-playpen"
    from suite.its_registry import its_manifest_param

    comps_a = its_manifest_param(path_a, "COMPONENTS")
    comps_b = its_manifest_param(path_b, "COMPONENTS")
    assert comps_b.split(",") == ["dashboard_cypress", "platform"]
    assert "ogx" in comps_a.split(",")
    assert "codeflare_sdk" in comps_a.split(",")
    assert "ai_safety" in comps_a.split(",")
    assert "platform" not in comps_a.split(",")
    assert "dashboard_cypress" not in comps_a.split(",")
    assert integration_test_scenario_default_konflux_app("rhoai-e2e-ephc-playpen-a") == ""


def test_resolve_ephc_fbc_slice_a_on_421_b_on_422() -> None:
    from suite.its_registry import its_manifest_param

    path_a = resolve_integration_test_scenario_manifest(_ROOT, "rhoai-e2e-ephc-ocp421")
    path_b = resolve_integration_test_scenario_manifest(_ROOT, "rhoai-e2e-ephc-ocp422")
    assert integration_test_scenario_application(path_a) == "rhoai-fbc-fragment-ocp-421"
    assert integration_test_scenario_application(path_b) == "rhoai-fbc-fragment-ocp-422"
    assert its_manifest_param(path_a, "RHOAI_FBC_NAME") == "rhoai-fbc-fragment-ocp-421"
    assert its_manifest_param(path_b, "RHOAI_FBC_NAME") == "rhoai-fbc-fragment-ocp-422"
    playpen_a = resolve_integration_test_scenario_manifest(_ROOT, "rhoai-e2e-ephc-playpen-a")
    playpen_b = resolve_integration_test_scenario_manifest(_ROOT, "rhoai-e2e-ephc-playpen-b")
    assert its_manifest_param(path_a, "COMPONENTS") == its_manifest_param(playpen_a, "COMPONENTS")
    assert its_manifest_param(path_b, "COMPONENTS") == its_manifest_param(playpen_b, "COMPONENTS")
    assert its_manifest_param(path_b, "COMPONENTS") == "dashboard_cypress,platform"
    assert "ogx" in its_manifest_param(path_a, "COMPONENTS").split(",")
    assert "dashboard_cypress" in its_manifest_param(path_b, "COMPONENTS").split(",")
    assert "dashboard_cypress" not in its_manifest_param(path_a, "COMPONENTS").split(",")
    assert "platform" not in its_manifest_param(path_a, "COMPONENTS").split(",")
    assert integration_test_scenario_default_konflux_app("rhoai-e2e-ephc-ocp421") == (
        "rhoai-fbc-fragment-ocp-421"
    )
    assert integration_test_scenario_default_konflux_app("rhoai-e2e-ephc-ocp422") == (
        "rhoai-fbc-fragment-ocp-422"
    )


def test_ephc_pipelinerun_wrapper_prefix() -> None:
    path = _ROOT / "tekton" / "pipelines" / "rhoai-e2e-pipelinerun-ephc.yaml"
    text = path.read_text(encoding="utf-8")
    assert "generateName: e2e-its-ephc-smoke-" in text
