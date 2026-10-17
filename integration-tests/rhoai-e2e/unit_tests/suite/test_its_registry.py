"""Tests for ITS manifest registry (no cluster)."""

from __future__ import annotations

from suite.constants import DEFAULT_APP, DEFAULT_NAMESPACE
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
_KONFLUX_RHOAI1_REL = (
    "integration-tests/rhoai-e2e/tekton/its/its-e2e-konflux-rhoai1-ocp422.yaml"
)
_KONFLUX_RHOAI1_SHORT = "tekton/its/its-e2e-konflux-rhoai1-ocp422.yaml"
_KONFLUX_RHOAI1_NAME = "e2e-konflux-rhoai1-ocp422"


def test_validate_integration_test_scenario_name_ok() -> None:
    assert validate_integration_test_scenario_name("e2e-ephc-ocp420-wb-mr-msrv-mrt-maas") == (
        "e2e-ephc-ocp420-wb-mr-msrv-mrt-maas"
    )


def test_validate_integration_test_scenario_name_rejects_empty() -> None:
    with pytest.raises(AppError, match="non-empty"):
        validate_integration_test_scenario_name("  ")


def test_looks_like_its_manifest_path() -> None:
    assert looks_like_its_manifest_path(_KONFLUX_RHOAI1_REL)
    assert looks_like_its_manifest_path(_KONFLUX_RHOAI1_SHORT)
    assert not looks_like_its_manifest_path(_KONFLUX_RHOAI1_NAME)


def test_resolve_ephc_manifest_slice_b() -> None:
    path = resolve_integration_test_scenario_manifest(_ROOT, "e2e-ephc-ocp421-dcy-plat")
    assert path.name == "its-e2e-ephc-ocp421-dcy-plat.yaml"
    assert integration_test_scenario_application(path) == "rhoai-fbc-fragment-ocp-421"


def test_resolve_konflux_rhoai1_manifest() -> None:
    path = resolve_integration_test_scenario_manifest(_ROOT, _KONFLUX_RHOAI1_NAME)
    assert path.name == "its-e2e-konflux-rhoai1-ocp422.yaml"
    assert integration_test_scenario_application(path) == "rhoai-fbc-fragment-ocp-422"
    from suite.its_registry import its_manifest_param

    cluster = its_manifest_param(path, "CLUSTER_SOURCE")
    assert cluster == "rhoai-e2e-s3-konflux-rhoai1"
    comps = its_manifest_param(path, "COMPONENTS").split(",")
    assert "dashboard_cypress" in comps
    assert "platform" in comps


def test_resolve_manifest_path_repo_relative(monkeypatch: pytest.MonkeyPatch) -> None:
    repo_root = _ROOT.resolve().parent.parent
    monkeypatch.chdir(repo_root)
    path = resolve_integration_test_scenario_manifest_path(_ROOT, _KONFLUX_RHOAI1_REL)
    assert path.name == "its-e2e-konflux-rhoai1-ocp422.yaml"


def test_resolve_manifest_path_explicit_wrong_cwd_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.chdir(_ROOT)
    with pytest.raises(AppError, match="ITS manifest not found"):
        resolve_integration_test_scenario_manifest_path(
            _ROOT,
            "./integration-tests/rhoai-e2e/tekton/its/its-e2e-konflux-rhoai1-ocp422.yaml",
        )


def test_resolve_manifest_path_rhoai_e2e_relative() -> None:
    path = resolve_integration_test_scenario_manifest_path(_ROOT, _KONFLUX_RHOAI1_SHORT)
    assert path.name == "its-e2e-konflux-rhoai1-ocp422.yaml"


def test_resolve_ref_from_path_returns_metadata_name() -> None:
    manifest, name = resolve_integration_test_scenario_ref(_ROOT, _KONFLUX_RHOAI1_SHORT)
    assert manifest.name == "its-e2e-konflux-rhoai1-ocp422.yaml"
    assert name == _KONFLUX_RHOAI1_NAME


def test_resolve_manifest_path_rejects_missing_file() -> None:
    with pytest.raises(AppError, match="ITS manifest not found"):
        resolve_integration_test_scenario_manifest_path(
            _ROOT,
            "integration-tests/rhoai-e2e/tekton/its/does-not-exist.yaml",
        )


def test_konflux_rhoai1_default_konflux_app() -> None:
    assert (
        integration_test_scenario_default_konflux_app(_KONFLUX_RHOAI1_NAME)
        == "rhoai-fbc-fragment-ocp-422"
    )
    assert integration_test_scenario_default_konflux_app("e2e-ephc-ocp420-wb-mr-msrv-mrt-maas") == (
        "rhoai-fbc-fragment-ocp-420"
    )


def test_resolve_run_its_snapshot_konflux_rhoai1() -> None:
    path = resolve_integration_test_scenario_run_its_snapshot(_ROOT, _KONFLUX_RHOAI1_NAME)
    assert path is not None
    assert path.name == "test-snapshot-rh-nightly.yaml"


def test_resolve_run_its_snapshot_unsupported_returns_none() -> None:
    assert (
        resolve_integration_test_scenario_run_its_snapshot(_ROOT, "e2e-ephc-ocp420-wb-mr-msrv-mrt-maas")
        is None
    )


def test_its_manifest_param_reads_product() -> None:
    path = _ROOT / "tekton" / "its" / "its-e2e-konflux-rhoai1-ocp422.yaml"
    from suite.its_registry import its_manifest_param

    assert its_manifest_param(path, "PRODUCT") == "rhoai"


def test_resolve_manifest_path_absolute_under_repo() -> None:
    abs_path = _ROOT / "tekton" / "its" / "its-e2e-konflux-rhoai1-ocp422.yaml"
    path = resolve_integration_test_scenario_manifest_path(_ROOT, str(abs_path))
    assert path == abs_path.resolve()


def test_resolve_manifest_path_cwd_relative(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    source = _ROOT / "tekton" / "its" / "its-e2e-konflux-rhoai1-ocp422.yaml"
    copy = tmp_path / "my-its.yaml"
    copy.write_text(source.read_text(encoding="utf-8"), encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    path = resolve_integration_test_scenario_manifest_path(_ROOT, "my-its.yaml")
    assert path == copy.resolve()


def test_resolve_manifest_path_absolute_outside_repo(tmp_path: Path) -> None:
    source = _ROOT / "tekton" / "its" / "its-e2e-konflux-rhoai1-ocp422.yaml"
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


def test_list_manifests_includes_ephc_slice_its() -> None:
    names = list_integration_test_scenario_manifests(_ROOT)
    assert "e2e-ephc-ocp420-wb-mr-msrv-mrt-maas" in names
    assert (
        "e2e-ephc-ocp420-aip-ray-mlf-ogx-ais-tr-sprk-cf"
        in names
    )
    assert "e2e-ephc-ocp420-llam-dsw-wbi" in names
    assert "e2e-ephc-ocp421-dcy-plat" in names
    assert _KONFLUX_RHOAI1_NAME in names
    assert "rhoai-e2e-ephc-ocp420" not in names
    assert "rhoai-e2e-ephc-ocp422-a" not in names
    assert "rhoai-e2e-ephc-ocp422-b" not in names


def test_resolve_ephc_fbc_slice_a_on_420_b_on_421() -> None:
    from suite.its_registry import its_manifest_param

    path_core = resolve_integration_test_scenario_manifest(_ROOT, "e2e-ephc-ocp420-wb-mr-msrv-mrt-maas")
    path_mlop = resolve_integration_test_scenario_manifest(
        _ROOT,
        "e2e-ephc-ocp420-aip-ray-mlf-ogx-ais-tr-sprk-cf",
    )
    path_tail = resolve_integration_test_scenario_manifest(_ROOT, "e2e-ephc-ocp420-llam-dsw-wbi")
    path_b = resolve_integration_test_scenario_manifest(_ROOT, "e2e-ephc-ocp421-dcy-plat")
    assert integration_test_scenario_application(path_core) == "rhoai-fbc-fragment-ocp-420"
    assert integration_test_scenario_application(path_b) == "rhoai-fbc-fragment-ocp-421"
    core_comps = set(its_manifest_param(path_core, "COMPONENTS").split(","))
    mlops_comps = set(its_manifest_param(path_mlop, "COMPONENTS").split(","))
    tail_comps = set(its_manifest_param(path_tail, "COMPONENTS").split(","))
    assert core_comps == {
        "workbenches",
        "model_registry",
        "model_server",
        "model_runtime",
        "maas_billing",
    }
    assert mlops_comps == {
        "ai_pipelines",
        "kuberay",
        "mlflow",
        "ogx",
        "ai_safety",
        "ai_safety_evalhub",
        "ai_safety_guardrails",
        "ai_safety_lmeval",
        "ai_safety_trustyai_operator",
        "ai_safety_trustyai_service",
        "trainer",
        "spark_operator",
        "codeflare_sdk",
    }
    assert tail_comps == {"llama_stack", "distributed_workloads", "workbench_images"}
    assert "dashboard_cypress" not in core_comps | mlops_comps | tail_comps
    assert its_manifest_param(path_b, "COMPONENTS") == "dashboard_cypress,platform"
    assert integration_test_scenario_default_konflux_app("e2e-ephc-ocp420-wb-mr-msrv-mrt-maas") == (
        "rhoai-fbc-fragment-ocp-420"
    )
    assert integration_test_scenario_default_konflux_app("e2e-ephc-ocp421-dcy-plat") == (
        "rhoai-fbc-fragment-ocp-421"
    )


def test_external_fbc_pipelinerun_wrapper_prefix() -> None:
    path = _ROOT / "tekton" / "pipelines" / "rhoai-e2e-pipelinerun-rh-nightly.yaml"
    text = path.read_text(encoding="utf-8")
    assert "generateName: e2e-its-konflux-rhoai1-smoke-" in text


def test_ephc_pipelinerun_wrapper_prefix() -> None:
    path = _ROOT / "tekton" / "pipelines" / "rhoai-e2e-pipelinerun-ephc.yaml"
    text = path.read_text(encoding="utf-8")
    assert "generateName: e2e-its-ephc-smoke-" in text


def test_should_use_upstream_main_its_for_run_its() -> None:
    from suite.its_registry import should_use_upstream_main_its_for_run_its

    assert should_use_upstream_main_its_for_run_its(
        local_its=False,
        konflux_repo="",
        konflux_branch="",
    )
    assert not should_use_upstream_main_its_for_run_its(
        local_its=False,
        konflux_repo="https://github.com/x/y.git",
        konflux_branch="",
    )
    assert not should_use_upstream_main_its_for_run_its(
        local_its=False,
        konflux_repo="",
        konflux_branch="fix/foo",
    )
    assert not should_use_upstream_main_its_for_run_its(
        local_its=True,
        konflux_repo="",
        konflux_branch="",
    )
