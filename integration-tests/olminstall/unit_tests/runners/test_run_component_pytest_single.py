"""Tests for per-component Tekton step mode in run_component_pytest."""

from __future__ import annotations

import json
import os
import shlex
from pathlib import Path
from unittest import mock

import pytest

from runners import run_component_pytest

def test_ephc_pytest_extra_args_skip_image_validation() -> None:
    with mock.patch.dict(os.environ, {"CLUSTER_SOURCE": "EPHC"}, clear=False):
        assert (
            run_component_pytest._apply_cluster_source_pytest_extra_args(
                "--tc use_unprivileged_client:False"
            )
            == "--tc use_unprivileged_client:False -k 'not image_validation and not verify_images'"
        )
        workbenches_extra = (
            "--tc use_unprivileged_client:False "
            "-k 'not trainer_imagestreams and not older_tags_health'"
        )
        merged = run_component_pytest._apply_cluster_source_pytest_extra_args(workbenches_extra)
        import shlex
        from runners.run_bvt_pytest import _build_pytest_args

        args = _build_pytest_args("smoke", merged, "tests/workbenches/", "/tmp/junit.xml")
        assert "trainer_imagestreams" not in args
        assert "-k" in args
        k_expr = args[args.index("-k") + 1]
        assert "image_validation" in k_expr
        assert "imagestream_health" not in k_expr
    with mock.patch.dict(os.environ, {"CLUSTER_SOURCE": "my-secret"}, clear=False):
        merged = run_component_pytest._apply_cluster_source_pytest_extra_args("-svv")
        assert "image_validation" in merged
    with mock.patch.dict(os.environ, {"CLUSTER_SOURCE": ""}, clear=False):
        assert run_component_pytest._apply_cluster_source_pytest_extra_args("-svv") == "-svv"

def test_external_rhoai_pytest_extra_args_skip_image_validation() -> None:
    with mock.patch.dict(
        os.environ,
        {
            "CLUSTER_SOURCE": "olminstall-kubeconfig-nmanos-konflux1-nmanos",
            "PRODUCT": "rhoai",
        },
        clear=False,
    ):
        merged = run_component_pytest._apply_cluster_source_pytest_extra_args(
            "--tc use_unprivileged_client:False"
        )
        assert "image_validation" in merged
        assert "not imagestream_health" not in merged
        assert "--cluster-sanity-skip-rhoai-check" not in merged

def test_external_existing_pytest_extra_args_skip_image_validation() -> None:
    with mock.patch.dict(
        os.environ,
        {
            "CLUSTER_SOURCE": "olminstall-kubeconfig-nmanos-konflux1-nmanos",
            "PRODUCT": "",
        },
        clear=False,
    ):
        merged = run_component_pytest._apply_cluster_source_pytest_extra_args(
            "--tc use_unprivileged_client:False"
        )
        assert "image_validation" in merged
        assert "not imagestream_health" not in merged
        assert "--cluster-sanity-skip-rhoai-check" in merged

def test_external_existing_pytest_extra_args_skip_rhoai_cluster_sanity() -> None:
    with mock.patch.dict(
        os.environ,
        {
            "CLUSTER_SOURCE": "olminstall-kubeconfig-ods-qe-psi-23",
            "PRODUCT": "",
            "RUN_COMPONENT_CLUSTER_PREP_IN_DEP_OPERATORS": "true",
        },
        clear=False,
    ):
        assert (
            run_component_pytest._apply_cluster_source_pytest_extra_args(
                "--tc use_unprivileged_client:False"
            )
            == "--tc use_unprivileged_client:False -k 'not image_validation and not verify_images' --cluster-sanity-skip-rhoai-check"
        )


def test_needs_schedulable_nodes_wait_ephc_only() -> None:
    with mock.patch.dict(os.environ, {"CLUSTER_SOURCE": "EPHC"}, clear=False):
        assert run_component_pytest._needs_schedulable_nodes_wait() is True
    with mock.patch.dict(os.environ, {"CLUSTER_SOURCE": "my-secret"}, clear=False):
        assert run_component_pytest._needs_schedulable_nodes_wait() is False


def test_wait_for_schedulable_nodes_before_component_pytest_skips_non_ephc() -> None:
    with mock.patch.dict(os.environ, {"CLUSTER_SOURCE": "my-secret"}, clear=False):
        assert run_component_pytest._wait_for_schedulable_nodes_before_component_pytest() == ""


def test_wait_for_schedulable_nodes_before_component_pytest_returns_error() -> None:
    with mock.patch.dict(os.environ, {"CLUSTER_SOURCE": "EPHC"}, clear=False):
        with mock.patch(
            "steps.prepare_bvt_cluster_nodes.wait_for_schedulable_nodes_for_bvt",
            side_effect=RuntimeError("cluster_health precheck timed out waiting for schedulable nodes (120s): node-a"),
        ):
            msg = run_component_pytest._wait_for_schedulable_nodes_before_component_pytest()
    assert "node-a" in msg


def test_needs_full_dsc_ready_before_pytest() -> None:
    assert run_component_pytest._needs_full_dsc_ready_before_pytest("ogx") is True
    assert run_component_pytest._needs_full_dsc_ready_before_pytest("ai_safety_evalhub") is True
    assert run_component_pytest._needs_full_dsc_ready_before_pytest("ai_safety_trustyai_service") is True
    assert run_component_pytest._needs_full_dsc_ready_before_pytest("workbenches") is False
    assert run_component_pytest._needs_full_dsc_ready_before_pytest("maas_billing") is True

def test_merge_pytest_k_skip_dedupes_fragment() -> None:
    merged = run_component_pytest._merge_pytest_k_skip(
        "--cluster-sanity-skip-rhoai-check",
        "-k 'not TestAPIKeyCRUD'",
    )
    assert "TestAPIKeyCRUD" in merged
    assert "-k" in merged

def test_collect_only_mode_product_existing_no_external() -> None:
    with mock.patch.dict(
        os.environ,
        {"PRODUCT": "", "CLUSTER_SOURCE": "", "KUBECONFIG": ""},
        clear=False,
    ):
        assert run_component_pytest._collect_only_mode() is True

def test_collect_only_mode_external_kubeconfig() -> None:
    with mock.patch.dict(
        os.environ,
        {"PRODUCT": "", "CLUSTER_SOURCE": "my-secret"},
        clear=False,
    ):
        assert run_component_pytest._collect_only_mode() is False

def test_collect_only_mode_staged_kubeconfig(tmp_path: Path) -> None:
    kubeconfig = tmp_path / "kubeconfig"
    kubeconfig.write_text("apiVersion: v1\nkind: Config\n", encoding="utf-8")
    with mock.patch.dict(
        os.environ,
        {"PRODUCT": "", "CLUSTER_SOURCE": "", "KUBECONFIG": str(kubeconfig)},
        clear=False,
    ):
        assert run_component_pytest._collect_only_mode() is False

def test_accumulate_exit_file_keeps_worst(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TEST_ARTIFACTS_DIR", str(tmp_path))
    artifacts = tmp_path / "artifacts"
    artifacts.mkdir()
    monkeypatch.setenv("ARTIFACTS_DIR", str(artifacts))
    run_component_pytest._accumulate_exit_file(0)
    assert not (artifacts / "component-test.exit").exists()
    run_component_pytest._accumulate_exit_file(1)
    assert (artifacts / "component-test.exit").read_text() == "1"
    run_component_pytest._accumulate_exit_file(0)
    assert (artifacts / "component-test.exit").read_text() == "1"
    run_component_pytest._accumulate_exit_file(2)
    assert (artifacts / "component-test.exit").read_text() == "2"

def test_main_skips_component_not_in_plan(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TEST_ARTIFACTS_DIR", str(tmp_path))
    artifacts = tmp_path / "artifacts"
    artifacts.mkdir()
    plan = {
        "component_test_phases": ["smoke"],
        "components": [
            {
                "id": "workbenches",
                "pytest_marker": "smoke",
                "pytest_extra_args": "",
                "tests_subdir": "tests/workbenches/",
                "artifact_prefix": "workbenches-smoke",
            }
        ],
    }
    plan_path = artifacts / "plan.json"
    plan_path.write_text(json.dumps(plan), encoding="utf-8")
    monkeypatch.setenv("ARTIFACTS_DIR", str(artifacts))
    monkeypatch.setenv("COMPONENT_TEST_PLAN_JSON", str(plan_path))
    monkeypatch.setenv("COMPONENT_TEST_COMPONENT_ID", "model_registry")
    monkeypatch.setenv("PRODUCT", "rhoai")
    assert run_component_pytest.main() == 0

def test_main_runs_single_component_without_workbenches_in_plan(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Subset COMPONENTS / plan without workbenches still runs the selected component."""
    monkeypatch.setenv("TEST_ARTIFACTS_DIR", str(tmp_path))
    artifacts = tmp_path / "artifacts"
    artifacts.mkdir()
    plan = {
        "component_test_phases": ["smoke"],
        "components": [
            {
                "id": "kuberay",
                "pytest_marker": "smoke",
                "pytest_extra_args": "",
                "tests_subdir": "tests/kuberay/",
                "artifact_prefix": "kuberay-smoke",
            }
        ],
    }
    plan_path = artifacts / "plan.json"
    plan_path.write_text(json.dumps(plan), encoding="utf-8")
    monkeypatch.setenv("ARTIFACTS_DIR", str(artifacts))
    monkeypatch.setenv("COMPONENT_TEST_PLAN_JSON", str(plan_path))
    monkeypatch.setenv("COMPONENT_TEST_COMPONENT_ID", "kuberay")
    monkeypatch.setenv("PRODUCT", "rhoai")
    monkeypatch.delenv("TEST_OUTPUT_PATH", raising=False)

    with (
        mock.patch("runners.run_component_pytest.prepare_kubeconfig_auth_for_tests"),
        mock.patch("runners.run_component_pytest.cluster_smoke_infra_blocked_reason", return_value=""),
        mock.patch("runners.run_component_pytest.load_shift_left_env_from_mount"),
        mock.patch("runners.run_component_pytest.apply_cluster_router_ca_from_kubeconfig"),
        mock.patch("runners.orchestrator.stage_git_for_prereqs"),
        mock.patch("runners.orchestrator.prepare_oc_binary_path_for_pytest") as prep_oc,
        mock.patch("runners.run_component_pytest.prepare_component_for_smoke"),
        mock.patch("runners.run_component_pytest.run_single_pytest", return_value=0) as run_mock,
    ):
        assert run_component_pytest.main() == 0
    prep_oc.assert_called_once()

    run_mock.assert_called_once()


def test_pooled_external_smoke_prep_runs_when_cluster_prep_done(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("TEST_ARTIFACTS_DIR", str(tmp_path))
    artifacts = tmp_path / "artifacts"
    artifacts.mkdir()
    from steps.cluster_prep_state import mark_cluster_prep_done

    mark_cluster_prep_done(artifacts)
    plan = {
        "component_test_phases": ["smoke"],
        "components": [
            {
                "id": "kuberay",
                "pytest_marker": "smoke",
                "pytest_extra_args": "",
                "tests_subdir": "tests/kuberay/",
                "artifact_prefix": "kuberay-smoke",
            }
        ],
    }
    plan_path = artifacts / "plan.json"
    plan_path.write_text(json.dumps(plan), encoding="utf-8")
    monkeypatch.setenv("ARTIFACTS_DIR", str(artifacts))
    monkeypatch.setenv("COMPONENT_TEST_PLAN_JSON", str(plan_path))
    monkeypatch.setenv("COMPONENT_TEST_COMPONENT_ID", "kuberay")
    monkeypatch.setenv("PRODUCT", "rhoai")
    monkeypatch.setenv("CLUSTER_SOURCE", "olminstall-kubeconfig-nmanos-konflux1-nmanos")
    monkeypatch.delenv("TEST_OUTPUT_PATH", raising=False)

    with (
        mock.patch("runners.run_component_pytest.prepare_kubeconfig_auth_for_tests"),
        mock.patch("runners.run_component_pytest.cluster_smoke_infra_blocked_reason", return_value=""),
        mock.patch("runners.run_component_pytest.load_shift_left_env_from_mount"),
        mock.patch("runners.run_component_pytest.apply_cluster_router_ca_from_kubeconfig"),
        mock.patch("runners.orchestrator.stage_git_for_prereqs"),
        mock.patch("runners.run_component_pytest.run_pooled_external_smoke_prep") as pooled,
        mock.patch("runners.run_component_pytest.prepare_component_for_smoke") as full_prep,
        mock.patch("runners.run_component_pytest.run_single_pytest", return_value=0),
    ):
        assert run_component_pytest.main() == 0

    pooled.assert_called_once_with("kuberay")
    full_prep.assert_not_called()


def test_maas_billing_passes_htpasswd_overlay_to_pytest(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("TEST_ARTIFACTS_DIR", str(tmp_path))
    artifacts = tmp_path / "artifacts"
    artifacts.mkdir()
    plan = {
        "component_test_phases": ["smoke"],
        "components": [
            {
                "id": "maas_billing",
                "pytest_marker": "smoke",
                "pytest_extra_args": "",
                "tests_subdir": "tests/model_serving/maas_billing/",
                "artifact_prefix": "maas_billing-smoke",
            }
        ],
    }
    plan_path = artifacts / "plan.json"
    plan_path.write_text(json.dumps(plan), encoding="utf-8")
    overlay = {
        "TEST_USER_USERNAME": "htpasswd-cluster-admin-user",
        "TEST_USER_PASSWORD": "admin-pass",
        "CLUSTER_AUTH": "htpasswd-cluster-admin",
    }
    monkeypatch.setenv("ARTIFACTS_DIR", str(artifacts))
    monkeypatch.setenv("COMPONENT_TEST_PLAN_JSON", str(plan_path))
    monkeypatch.setenv("COMPONENT_TEST_COMPONENT_ID", "maas_billing")
    monkeypatch.setenv("PRODUCT", "rhoai")
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "test-key")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "test-secret")
    monkeypatch.delenv("TEST_OUTPUT_PATH", raising=False)

    with (
        mock.patch("runners.run_component_pytest.prepare_kubeconfig_auth_for_tests"),
        mock.patch("runners.run_component_pytest.cluster_smoke_infra_blocked_reason", return_value=""),
        mock.patch("runners.run_component_pytest.load_shift_left_env_from_mount"),
        mock.patch("runners.run_component_pytest.promote_shift_left_aws_env"),
        mock.patch("runners.run_component_pytest.apply_cluster_router_ca_from_kubeconfig"),
        mock.patch("runners.orchestrator.stage_git_for_prereqs"),
        mock.patch("runners.run_component_pytest.refresh_maas_smoke_before_pytest"),
        mock.patch("runners.run_component_pytest.prepare_component_for_smoke"),
        mock.patch("components.maas_billing.wait.require_dsc_ready_for_bvt"),
        mock.patch(
            "runners.run_component_pytest.apply_maas_billing_htpasswd_test_user_overrides",
            return_value=overlay,
        ),
        mock.patch("runners.run_component_pytest.run_single_pytest", return_value=0) as run_mock,
    ):
        assert run_component_pytest.main() == 0

    run_mock.assert_called_once_with(extra_env=overlay)


def test_ogx_forces_rh_dev_distribution_after_shift_left(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("distribution_name", "rh")
    extra = run_component_pytest._apply_ogx_pytest_extra_args(
        "--tc use_unprivileged_client:False --tc distribution_name:rh-dev"
    )
    assert "distribution_name:rh-dev" in extra
    assert "-p ogx_ea_distribution_plugin" in extra
    assert os.environ["distribution_name"] == "rh-dev"


def test_ogx_keeps_vector_stores_in_k_filter(monkeypatch: pytest.MonkeyPatch) -> None:
    """Smoke+not pgvector already narrows to vector_stores; extra skip empties the suite."""
    monkeypatch.setenv("CLUSTER_SOURCE", "EPHC")
    monkeypatch.setenv("distribution_name", "rh-dev")
    extra = run_component_pytest._apply_ogx_pytest_extra_args("-k 'not pgvector'")
    assert "vector_stores" not in extra
    assert "not pgvector" in extra


def test_ensure_olminstall_on_pythonpath(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("PYTHONPATH", raising=False)
    run_component_pytest._ensure_olminstall_on_pythonpath()
    root = str(run_component_pytest._OLMINSTALL_ROOT)
    assert root in os.environ["PYTHONPATH"].split(os.pathsep)


def test_materialize_ogx_ea_conftest(tmp_path: Path) -> None:
    run_component_pytest._materialize_ogx_ea_conftest(tmp_path)
    work = tmp_path / "pytest-work-cwd"
    conf = work / "conftest.py"
    assert conf.is_file()
    text = conf.read_text(encoding="utf-8")
    assert "ogx_ea_distribution_plugin" in text
    assert "pytest_configure" in text
    assert (work / "ogx_ea_distribution_plugin.py").is_file()
    assert (work / "sitecustomize.py").is_file()
    assert "apply_ogx_ea_distribution_patch" in (work / "sitecustomize.py").read_text(encoding="utf-8")
