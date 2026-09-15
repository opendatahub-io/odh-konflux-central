"""MaaS prep skips RHCL when install-dep-operators already ran."""

from __future__ import annotations

from contextlib import ExitStack
from pathlib import Path
from typing import Any

from unittest.mock import patch


def _enter_patch(stack: ExitStack, target: str, **kwargs: Any):
    return stack.enter_context(patch(target, **kwargs))

from components.maas_billing import prep as maas_prep
from runners.component_prereqs import (
    _ensure_dsc_managed_for_component,
    prepare_component_for_smoke,
)
from steps.cluster_prep_state import mark_cluster_prep_done, mark_dep_operators_done

def test_maas_prep_skips_rhcl_when_dep_operators_done(tmp_path, monkeypatch) -> None:
    payload = tmp_path / "results"
    payload.mkdir()
    monkeypatch.setenv("ARTIFACTS_DIR", str(payload))
    mark_dep_operators_done(payload)

    with ExitStack() as stack:
        _enter_patch(
            stack,
            "suite.component_catalog.load_components_smoke_catalog",
            return_value=type("Cat", (), {"components": {}})(),
        )
        _enter_patch(
            stack,
            "suite.component_catalog.default_components_smoke_config_path",
            return_value=Path("/dev/null"),
        )
        _enter_patch(
            stack,
            "suite.component_version_gate.version_skip_reason_for_component",
            return_value="",
        )
        rhcl = _enter_patch(stack, "components.maas_billing.prep.ensure_maas_rhcl_dependency_stack")
        authorino = _enter_patch(stack, "components.maas_billing.prep.require_maas_dependency_operators")
        _enter_patch(stack, "runners.component_prereqs.ensure_dsc_component_managed")
        _enter_patch(stack, "suite.component_dsc_gate.wait_for_smoke_dsc_ready_after_patch")
        _enter_patch(stack, "components.maas_billing.prep.ensure_maas_gateway")
        _enter_patch(stack, "components.maas_billing.prep.ensure_maas_gateway_route")
        _enter_patch(stack, "components.maas_billing.prep.ensure_maas_gateway_before_models_as_service")
        _enter_patch(stack, "components.maas_billing.prep.ensure_maas_database")
        _enter_patch(stack, "components.maas_billing.prep.ensure_maas_bbr_pre_processing")
        _enter_patch(stack, "components.maas_billing.prep.cleanup_maas_smoke_leaked_rbac")
        _enter_patch(stack, "components.maas_billing.prep.ensure_maas_gateway_auth_policy_alias")
        _enter_patch(stack, "components.maas_billing.prep.ensure_user_workload_monitoring")
        _enter_patch(
            stack,
            "components.maas_billing.prep.ensure_maas_authorino_ready",
            return_value="authorino",
        )
        _enter_patch(stack, "components.maas_billing.prep.ensure_dsc_models_as_service")
        _enter_patch(stack, "components.maas_billing.prep.ensure_maas_api_auth_policy")
        _enter_patch(stack, "components.maas_billing.prep.ensure_maas_auth_policy_ready")
        _enter_patch(stack, "components.maas_billing.prep._wait_for_maas_smoke_ready")
        _enter_patch(
            stack,
            "components.maas_billing.prep.maas_smoke_surface_already_done",
            return_value=False,
        )
        _enter_patch(stack, "components.maas_billing.prep.mark_maas_smoke_surface_done")
        assert prepare_component_for_smoke("model_server") is True
        rhcl.assert_not_called()
        authorino.assert_not_called()

def test_ensure_dsc_managed_for_maas_billing_defers_models_as_service() -> None:
    with (
        patch("runners.component_prereqs.ensure_dsc_component_managed"),
        patch("install.dsc_install.ensure_dsc_models_as_service") as models_as_service,
        patch("suite.component_dsc_gate.wait_for_smoke_dsc_ready_after_patch") as wait_dsc,
    ):
        _ensure_dsc_managed_for_component("maas_billing")
        models_as_service.assert_not_called()
        wait_dsc.assert_not_called()

def test_ensure_dsc_managed_for_model_server_defers_models_as_service() -> None:
    with (
        patch(
            "runners.component_prereqs._dsc_smoke_managed_components",
            return_value=["kserve"],
        ),
        patch("runners.component_prereqs.ensure_dsc_component_managed"),
        patch("install.dsc_install.ensure_dsc_models_as_service") as models_as_service,
        patch("suite.component_dsc_gate.wait_for_smoke_dsc_ready_after_patch") as wait_dsc,
    ):
        _ensure_dsc_managed_for_component("model_server")
        models_as_service.assert_not_called()
        wait_dsc.assert_called_once_with("model_server")


def test_ensure_maas_database_before_smoke_prep_for_maas_ids() -> None:
    with (
        patch("components.maas_billing.database.ensure_maas_database") as db,
        patch("components.maas_billing.uwm.ensure_user_workload_monitoring") as uwm,
        patch("install.dsc_install.ensure_dsc_models_as_service") as mas,
    ):
        from runners.component_prereqs import _ensure_maas_database_before_smoke_prep

        _ensure_maas_database_before_smoke_prep({"maas_billing"})
        db.assert_called_once()
        uwm.assert_called_once()
        mas.assert_not_called()


def test_ensure_maas_database_before_smoke_prep_for_model_server_ids() -> None:
    with (
        patch("components.maas_billing.database.ensure_maas_database") as db,
        patch("components.maas_billing.uwm.ensure_user_workload_monitoring") as uwm,
        patch("install.dsc_install.ensure_dsc_models_as_service") as mas,
    ):
        from runners.component_prereqs import _ensure_maas_database_before_smoke_prep

        _ensure_maas_database_before_smoke_prep({"model_server", "workbenches"})
        db.assert_called_once()
        uwm.assert_called_once()
        mas.assert_not_called()


def test_ensure_maas_database_before_smoke_prep_skips_without_maas_ids() -> None:
    with (
        patch("components.maas_billing.database.ensure_maas_database") as db,
        patch("components.maas_billing.uwm.ensure_user_workload_monitoring") as uwm,
        patch("install.dsc_install.ensure_dsc_models_as_service") as mas,
        patch("components.maas_billing.prep.try_prepare_maas_smoke") as prep,
    ):
        from runners.component_prereqs import _ensure_maas_database_before_smoke_prep

        _ensure_maas_database_before_smoke_prep({"workbenches"})
        db.assert_not_called()
        uwm.assert_not_called()
        mas.assert_not_called()
        prep.assert_not_called()


def test_ensure_dsc_managed_for_ogx_removes_llamastackoperator() -> None:
    with (
        patch(
            "runners.component_prereqs._dsc_smoke_managed_components",
            return_value=["ogx"],
        ),
        patch(
            "runners.component_prereqs.dsc_component_management_state",
            return_value="Managed",
        ),
        patch("runners.component_prereqs.ensure_dsc_component_removed") as removed,
        patch("runners.component_prereqs.ensure_dsc_component_managed"),
        patch("suite.component_dsc_gate.wait_for_smoke_dsc_ready_after_patch"),
    ):
        _ensure_dsc_managed_for_component("ogx")
        removed.assert_called_once_with("llamastackoperator")

def test_maas_resync_when_global_cluster_prep_done(tmp_path, monkeypatch) -> None:
    payload = tmp_path / "results"
    payload.mkdir()
    monkeypatch.setenv("ARTIFACTS_DIR", str(payload))
    mark_cluster_prep_done(payload)

    with (
        patch("runners.component_prereqs._ensure_dsc_managed_for_component") as dsc,
        patch("components.maas_billing.wait._wait_for_maas_smoke_ready") as wait_maas,
        patch("install.dsc_install.ensure_dsc_models_as_service") as models_as_service,
    ):
        assert prepare_component_for_smoke("model_server") is True
        dsc.assert_called_once_with("model_server")
        wait_maas.assert_called_once()
        models_as_service.assert_not_called()

        dsc.reset_mock()
        wait_maas.reset_mock()
        assert prepare_component_for_smoke("maas_billing") is True
        dsc.assert_called_once_with("maas_billing")
        models_as_service.assert_called_once()
        wait_maas.assert_called_once()

def test_maas_billing_prep_enables_models_as_service_after_gateway() -> None:
    call_order: list[str] = []

    def _track(name: str):
        def _fn(*_a, **_k):
            call_order.append(name)

        return _fn

    with (
        patch("helpers.hypershift_admission_webhooks.neutralize_broken_hypershift_admission_webhooks"),
        patch("components.maas_billing.gateway.ensure_openshift_default_gateway_class"),
        patch("components.maas_billing.auth.recover_kuadrant_after_gateway_api_provider"),
        patch("components.maas_billing.common._dsc_condition", return_value=("False", "", "")),
        patch("steps.cluster_prep_state.maas_gateway_https_blocked_reason", return_value=""),
        patch("components.maas_billing.prep.maas_gateway_mas_already_done", return_value=False),
        patch("components.maas_billing.prep.ensure_maas_gateway_ingress_tls_secret"),
        patch("components.maas_billing.prep.ensure_authorino_tls"),
        patch("components.maas_billing.prep.ensure_maas_gateway", side_effect=_track("gateway")),
        patch("components.maas_billing.prep.ensure_maas_gateway_route"),
        patch("components.maas_billing.prep._wait_maas_gateway_https_for_models_as_service"),
        patch("components.maas_billing.prep.maas_api_deployment_exists", return_value=True),
        patch(
            "components.maas_billing.prep.ensure_dsc_models_as_service",
            side_effect=_track("models_as_service"),
        ) as models_as_service,
        patch("components.maas_billing.prep._restart_maas_api_after_gateway"),
        patch("components.maas_billing.prep.mark_maas_gateway_mas_done"),
    ):
        maas_prep.ensure_maas_gateway_before_models_as_service()
        assert call_order.index("gateway") < call_order.index("models_as_service")
        models_as_service.assert_called_once_with(
            wait_timeout_sec=900,
            wait_for_aigateway=True,
        )


def test_maas_gateway_prep_defers_aigateway_when_maas_api_missing() -> None:
    with (
        patch("helpers.hypershift_admission_webhooks.neutralize_broken_hypershift_admission_webhooks"),
        patch("components.maas_billing.gateway.ensure_openshift_default_gateway_class"),
        patch("components.maas_billing.auth.recover_kuadrant_after_gateway_api_provider"),
        patch("components.maas_billing.common._dsc_condition", return_value=("False", "", "")),
        patch("steps.cluster_prep_state.maas_gateway_https_blocked_reason", return_value=""),
        patch("components.maas_billing.prep.maas_gateway_mas_already_done", return_value=False),
        patch("components.maas_billing.prep.ensure_maas_gateway_ingress_tls_secret"),
        patch("components.maas_billing.prep.ensure_authorino_tls"),
        patch("components.maas_billing.prep.ensure_maas_gateway"),
        patch("components.maas_billing.prep.ensure_maas_gateway_route"),
        patch("components.maas_billing.prep._wait_maas_gateway_https_for_models_as_service"),
        patch("components.maas_billing.prep.maas_api_deployment_exists", return_value=False),
        patch("components.maas_billing.prep.dep_operators_already_done", return_value=False),
        patch("components.maas_billing.prep.ensure_dsc_models_as_service") as models_as_service,
        patch("components.maas_billing.prep._restart_maas_api_after_gateway"),
        patch("components.maas_billing.prep.mark_maas_gateway_mas_done"),
    ):
        maas_prep.ensure_maas_gateway_before_models_as_service()
        models_as_service.assert_called_once_with(
            wait_timeout_sec=900,
            wait_for_aigateway=False,
        )


def test_maas_gateway_prep_waits_aigateway_after_install_rhoai_when_maas_api_missing() -> None:
    with (
        patch("helpers.hypershift_admission_webhooks.neutralize_broken_hypershift_admission_webhooks"),
        patch("components.maas_billing.gateway.ensure_openshift_default_gateway_class"),
        patch("components.maas_billing.auth.recover_kuadrant_after_gateway_api_provider"),
        patch("components.maas_billing.common._dsc_condition", return_value=("False", "", "")),
        patch("steps.cluster_prep_state.maas_gateway_https_blocked_reason", return_value=""),
        patch("components.maas_billing.prep.maas_gateway_mas_already_done", return_value=False),
        patch("components.maas_billing.prep.ensure_maas_gateway_ingress_tls_secret"),
        patch("components.maas_billing.prep.ensure_authorino_tls"),
        patch("components.maas_billing.prep.ensure_maas_gateway"),
        patch("components.maas_billing.prep.ensure_maas_gateway_route"),
        patch("components.maas_billing.prep._wait_maas_gateway_https_for_models_as_service"),
        patch("components.maas_billing.prep.maas_api_deployment_exists", return_value=False),
        patch("components.maas_billing.prep.dep_operators_already_done", return_value=True),
        patch("components.maas_billing.prep.ensure_dsc_models_as_service") as models_as_service,
        patch("components.maas_billing.prep._restart_maas_api_after_gateway"),
        patch("components.maas_billing.prep.mark_maas_gateway_mas_done"),
    ):
        maas_prep.ensure_maas_gateway_before_models_as_service()
        models_as_service.assert_called_once_with(
            wait_timeout_sec=900,
            wait_for_aigateway=True,
        )

def test_maas_prep_probes_only_on_existing_without_install_dependencies(monkeypatch) -> None:
    monkeypatch.setenv("PRODUCT", "")
    monkeypatch.delenv("INSTALL_DEPENDENCIES", raising=False)

    with ExitStack() as stack:
        _enter_patch(stack, "install.dsc_install.dsc_crd_available", return_value=True)
        _enter_patch(stack, "components.maas_billing.prep.dep_operators_already_done", return_value=False)
        rhcl = _enter_patch(stack, "components.maas_billing.prep.ensure_maas_rhcl_dependency_stack")
        probe = _enter_patch(stack, "components.maas_billing.prep.require_maas_dependency_operators")
        _enter_patch(stack, "components.maas_billing.prep.repair_payload_pre_processing_selector_conflict", return_value=False)
        _enter_patch(stack, "components.maas_billing.prep.ensure_maas_gateway")
        _enter_patch(stack, "components.maas_billing.prep.ensure_maas_gateway_route")
        _enter_patch(stack, "components.maas_billing.prep.ensure_maas_gateway_before_models_as_service")
        _enter_patch(stack, "components.maas_billing.prep.cleanup_maas_smoke_stale_gateway_leaks")
        _enter_patch(stack, "components.maas_billing.prep.maas_api_deployment_exists", return_value=True)
        _enter_patch(stack, "components.maas_billing.prep.ensure_maas_database")
        _enter_patch(stack, "components.maas_billing.prep.cleanup_maas_smoke_leaked_rbac")
        _enter_patch(stack, "components.maas_billing.prep.ensure_maas_bbr_pre_processing")
        _enter_patch(stack, "components.maas_billing.prep.ensure_user_workload_monitoring")
        _enter_patch(
            stack,
            "components.maas_billing.prep.ensure_maas_authorino_ready",
            return_value="authorino",
        )
        _enter_patch(stack, "components.maas_billing.prep.ensure_dsc_models_as_service")
        _enter_patch(stack, "components.maas_billing.prep.ensure_maas_api_auth_policy")
        _enter_patch(stack, "components.maas_billing.prep.ensure_maas_gateway_auth_policy_alias")
        _enter_patch(stack, "components.maas_billing.prep.ensure_maas_auth_policy_ready")
        _enter_patch(stack, "components.maas_billing.prep._wait_for_maas_smoke_ready")
        _enter_patch(
            stack,
            "components.maas_billing.prep.maas_smoke_surface_already_done",
            return_value=False,
        )
        _enter_patch(stack, "components.maas_billing.prep.mark_maas_smoke_surface_done")
        maas_prep.try_prepare_maas_smoke()
    rhcl.assert_not_called()
    probe.assert_called_once()

def test_maas_prep_runs_once_per_prepare_pass(tmp_path, monkeypatch) -> None:
    payload = tmp_path / "tests-payload" / "results"
    payload.mkdir(parents=True)
    monkeypatch.setenv("TESTS_SHARED", str(tmp_path))
    monkeypatch.delenv("ARTIFACTS_DIR", raising=False)
    monkeypatch.setenv("PIPELINE_RUN_NAME", "pr-maas-once")

    with ExitStack() as stack:
        _enter_patch(stack, "install.dsc_install.dsc_crd_available", return_value=True)
        gateway_mas = _enter_patch(
            stack,
            "components.maas_billing.prep.ensure_maas_gateway_before_models_as_service",
        )
        _enter_patch(stack, "components.maas_billing.prep.cleanup_maas_smoke_stale_gateway_leaks")
        _enter_patch(stack, "components.maas_billing.prep.maas_api_deployment_exists", return_value=True)
        _enter_patch(stack, "components.maas_billing.prep.dep_operators_already_done", return_value=True)
        _enter_patch(
            stack,
            "components.maas_billing.prep.maas_smoke_surface_already_done",
            side_effect=[False, True],
        )
        _enter_patch(stack, "components.maas_billing.prep.ensure_maas_database")
        _enter_patch(stack, "components.maas_billing.prep.cleanup_maas_smoke_leaked_rbac")
        _enter_patch(stack, "components.maas_billing.prep.ensure_maas_bbr_pre_processing")
        _enter_patch(stack, "components.maas_billing.prep.ensure_user_workload_monitoring")
        _enter_patch(
            stack,
            "components.maas_billing.prep.ensure_maas_authorino_ready",
            return_value="authorino",
        )
        _enter_patch(stack, "components.maas_billing.prep.ensure_dsc_models_as_service")
        _enter_patch(stack, "components.maas_billing.prep.ensure_maas_api_auth_policy")
        _enter_patch(stack, "components.maas_billing.prep.ensure_maas_gateway_auth_policy_alias")
        _enter_patch(stack, "components.maas_billing.prep.ensure_maas_auth_policy_ready")
        _enter_patch(stack, "components.maas_billing.prep._wait_for_maas_smoke_ready")
        maas_prep.try_prepare_maas_smoke()
        maas_prep.try_prepare_maas_smoke()
    gateway_mas.assert_called_once()

def test_maas_prep_marks_surface_done_when_wait_raises(tmp_path, monkeypatch) -> None:
    payload = tmp_path / "tests-payload" / "results"
    payload.mkdir(parents=True)
    monkeypatch.setenv("TESTS_SHARED", str(tmp_path))
    monkeypatch.setenv("PIPELINE_RUN_NAME", "pr-maas-wait-fail")

    with ExitStack() as stack:
        _enter_patch(stack, "install.dsc_install.dsc_crd_available", return_value=True)
        _enter_patch(stack, "components.maas_billing.prep.dep_operators_already_done", return_value=True)
        _enter_patch(
            stack,
            "components.maas_billing.prep.repair_payload_pre_processing_selector_conflict",
            return_value=False,
        )
        _enter_patch(
            stack,
            "components.maas_billing.prep.maas_smoke_surface_already_done",
            return_value=False,
        )
        _enter_patch(stack, "components.maas_billing.prep.ensure_maas_gateway")
        _enter_patch(stack, "components.maas_billing.prep.ensure_maas_gateway_route")
        _enter_patch(stack, "components.maas_billing.prep.ensure_maas_gateway_before_models_as_service")
        _enter_patch(stack, "components.maas_billing.prep.cleanup_maas_smoke_stale_gateway_leaks")
        _enter_patch(stack, "components.maas_billing.prep.ensure_maas_database")
        _enter_patch(stack, "components.maas_billing.prep.cleanup_maas_smoke_leaked_rbac")
        _enter_patch(stack, "components.maas_billing.prep.ensure_maas_bbr_pre_processing")
        _enter_patch(stack, "components.maas_billing.prep.ensure_user_workload_monitoring")
        _enter_patch(
            stack,
            "components.maas_billing.prep.ensure_maas_authorino_ready",
            return_value="authorino",
        )
        _enter_patch(stack, "components.maas_billing.prep.ensure_dsc_models_as_service")
        _enter_patch(stack, "components.maas_billing.prep.maas_api_deployment_exists", return_value=True)
        _enter_patch(stack, "components.maas_billing.prep.ensure_maas_api_auth_policy")
        _enter_patch(stack, "components.maas_billing.prep.ensure_maas_gateway_auth_policy_alias")
        _enter_patch(stack, "components.maas_billing.prep.ensure_maas_auth_policy_ready")
        _enter_patch(
            stack,
            "components.maas_billing.prep._wait_for_maas_smoke_ready",
            side_effect=RuntimeError("not ready"),
        )
        mark_done = _enter_patch(stack, "components.maas_billing.prep.mark_maas_smoke_surface_done")
        mark_attempted = _enter_patch(
            stack,
            "components.maas_billing.prep.mark_maas_smoke_prep_attempted",
        )
        try:
            maas_prep.try_prepare_maas_smoke()
        except RuntimeError as exc:
            assert "not ready" in str(exc)
        else:
            raise AssertionError("expected RuntimeError from MaaS wait")
    mark_done.assert_not_called()
    mark_attempted.assert_called_once()

def test_maas_prep_retries_rhcl_when_incomplete_marker_after_dep_ops(tmp_path, monkeypatch) -> None:
    """cleanup+reinstall: retry RHCL post-install when Kuadrant marker survived dep-ops."""
    payload = tmp_path / "tests-payload" / "results"
    payload.mkdir(parents=True)
    monkeypatch.setenv("TESTS_SHARED", str(tmp_path))
    monkeypatch.setenv("PIPELINE_RUN_NAME", "pr-maas-retry-rhcl")
    (payload / ".gateway-auth-stack-incomplete").write_text(
        "rhcl post-install retry failed\n", encoding="utf-8"
    )

    with ExitStack() as stack:
        _enter_patch(stack, "install.dsc_install.dsc_crd_available", return_value=True)
        _enter_patch(stack, "components.maas_billing.prep.dep_operators_already_done", return_value=True)
        _enter_patch(
            stack,
            "components.maas_billing.prep.maas_smoke_surface_already_done",
            return_value=False,
        )
        _enter_patch(
            stack,
            "helpers.gateway_stack_marker.reconcile_gateway_stack_incomplete_marker",
            return_value=False,
        )
        rhcl = _enter_patch(stack, "components.maas_billing.prep.ensure_maas_rhcl_dependency_stack")
        require = _enter_patch(stack, "components.maas_billing.prep.require_maas_dependency_operators")
        _enter_patch(stack, "components.maas_billing.prep.ensure_maas_gateway_before_models_as_service")
        _enter_patch(stack, "components.maas_billing.prep.ensure_maas_database")
        _enter_patch(stack, "components.maas_billing.prep.cleanup_maas_smoke_leaked_rbac")
        _enter_patch(stack, "components.maas_billing.prep.cleanup_maas_smoke_stale_gateway_leaks")
        _enter_patch(stack, "components.maas_billing.prep.ensure_maas_bbr_pre_processing")
        _enter_patch(stack, "components.maas_billing.prep.ensure_user_workload_monitoring")
        _enter_patch(
            stack,
            "components.maas_billing.prep.ensure_maas_authorino_ready",
            return_value="kuadrant-system",
        )
        _enter_patch(stack, "components.maas_billing.prep.ensure_maas_api_auth_policy")
        _enter_patch(stack, "components.maas_billing.prep.ensure_maas_gateway_auth_policy_alias")
        _enter_patch(stack, "components.maas_billing.prep.ensure_maas_auth_policy_ready")
        _enter_patch(stack, "components.maas_billing.prep._wait_for_maas_smoke_ready")
        _enter_patch(stack, "components.maas_billing.prep.mark_maas_smoke_surface_done")
        _enter_patch(stack, "components.maas_billing.prep.maas_api_deployment_exists", return_value=False)
        maas_prep.try_prepare_maas_smoke()
    rhcl.assert_called_once()
    require.assert_called_once_with(allow_deferred_authorino=True)


def test_maas_prep_skips_before_dsc_crd() -> None:
    with (
        patch("install.dsc_install.dsc_crd_available", return_value=False),
        patch("components.maas_billing.prep.ensure_maas_gateway") as gateway,
    ):
        maas_prep.try_prepare_maas_smoke()
    gateway.assert_not_called()


def test_maas_prep_skips_after_auth_wait_attempted(tmp_path, monkeypatch) -> None:
    payload = tmp_path / "tests-payload" / "results"
    payload.mkdir(parents=True)
    monkeypatch.setenv("TESTS_SHARED", str(tmp_path))
    monkeypatch.setenv("PIPELINE_RUN_NAME", "pr-maas-attempted")

    from steps.cluster_prep_state import mark_maas_smoke_prep_attempted

    mark_maas_smoke_prep_attempted(payload)

    with (
        patch("install.dsc_install.dsc_crd_available", return_value=True),
        patch("components.maas_billing.prep.ensure_maas_gateway") as gateway,
        patch("components.maas_billing.prep.dep_operators_already_done", return_value=True),
    ):
        maas_prep.try_prepare_maas_smoke()
    gateway.assert_not_called()


def test_maas_prep_force_retry_after_auth_wait_attempted(tmp_path, monkeypatch) -> None:
    payload = tmp_path / "tests-payload" / "results"
    payload.mkdir(parents=True)
    monkeypatch.setenv("TESTS_SHARED", str(tmp_path))
    monkeypatch.setenv("PIPELINE_RUN_NAME", "pr-maas-force-retry")

    from steps.cluster_prep_state import mark_maas_smoke_prep_attempted

    mark_maas_smoke_prep_attempted(payload)

    with ExitStack() as stack:
        _enter_patch(stack, "install.dsc_install.dsc_crd_available", return_value=True)
        _enter_patch(stack, "components.maas_billing.prep.dep_operators_already_done", return_value=True)
        _enter_patch(
            stack,
            "components.maas_billing.prep.maas_smoke_surface_already_done",
            return_value=False,
        )
        _enter_patch(stack, "components.maas_billing.prep.ensure_maas_gateway_before_models_as_service")
        _enter_patch(stack, "components.maas_billing.prep.ensure_maas_database")
        _enter_patch(stack, "components.maas_billing.prep.cleanup_maas_smoke_leaked_rbac")
        _enter_patch(stack, "components.maas_billing.prep.cleanup_maas_smoke_stale_gateway_leaks")
        _enter_patch(stack, "components.maas_billing.prep.ensure_maas_bbr_pre_processing")
        _enter_patch(stack, "components.maas_billing.prep.ensure_user_workload_monitoring")
        _enter_patch(stack, "components.maas_billing.prep.ensure_maas_authorino_ready", return_value="authorino")
        _enter_patch(stack, "components.maas_billing.prep.ensure_maas_api_auth_policy")
        _enter_patch(stack, "components.maas_billing.prep.ensure_maas_gateway_auth_policy_alias")
        _enter_patch(stack, "components.maas_billing.prep.ensure_maas_auth_policy_ready")
        wait_ready = _enter_patch(stack, "components.maas_billing.prep._wait_for_maas_smoke_ready")
        _enter_patch(stack, "components.maas_billing.prep.mark_maas_smoke_surface_done")
        maas_prep.try_prepare_maas_smoke(force_retry=True)
    wait_ready.assert_called_once()


def test_maas_prep_waits_for_models_as_service_before_auth_policy() -> None:
    """Operator creates maas-api after ModelsAsService reconcile; wait DSC before auth (n2bqt/4sknz)."""
    call_order: list[str] = []

    def _track(name: str):
        def _inner(*_a, **_kw):
            call_order.append(name)

        return _inner

    with ExitStack() as stack:
        _enter_patch(stack, "install.dsc_install.dsc_crd_available", return_value=True)
        _enter_patch(stack, "components.maas_billing.prep.dep_operators_already_done", return_value=True)
        _enter_patch(stack, "components.maas_billing.prep.ensure_maas_gateway_before_models_as_service")
        _enter_patch(stack, "components.maas_billing.prep.ensure_maas_database")
        _enter_patch(stack, "components.maas_billing.prep.cleanup_maas_smoke_leaked_rbac")
        _enter_patch(stack, "components.maas_billing.prep.cleanup_maas_smoke_stale_gateway_leaks")
        _enter_patch(stack, "components.maas_billing.prep.ensure_maas_bbr_pre_processing")
        _enter_patch(stack, "components.maas_billing.prep.ensure_user_workload_monitoring")
        _enter_patch(
            stack,
            "components.maas_billing.prep.ensure_maas_authorino_ready",
            return_value="authorino",
        )
        _enter_patch(stack, "components.maas_billing.prep.maas_api_deployment_exists", return_value=False)
        stack.enter_context(
            patch(
                "components.maas_billing.prep._wait_for_maas_smoke_ready",
                side_effect=_track("wait_smoke"),
            )
        )
        stack.enter_context(
            patch(
                "components.maas_billing.prep.ensure_maas_api_auth_policy",
                side_effect=_track("auth_policy"),
            )
        )
        _enter_patch(stack, "components.maas_billing.prep.ensure_maas_gateway_auth_policy_alias")
        _enter_patch(stack, "components.maas_billing.prep.ensure_maas_auth_policy_ready")
        _enter_patch(stack, "components.maas_billing.prep.mark_maas_smoke_surface_done")
        maas_prep.try_prepare_maas_smoke()
    assert call_order.index("wait_smoke") < call_order.index("auth_policy")


def test_maas_prep_defers_auth_wait_before_install_rhoai_when_maas_api_missing() -> None:
    """install-dep-operators runs before install-rhoai; do not wait for maas-api there."""
    with ExitStack() as stack:
        _enter_patch(stack, "install.dsc_install.dsc_crd_available", return_value=True)
        _enter_patch(stack, "components.maas_billing.prep.dep_operators_already_done", return_value=False)
        _enter_patch(stack, "components.maas_billing.prep.ensure_maas_rhcl_dependency_stack")
        _enter_patch(stack, "components.maas_billing.prep.require_maas_dependency_operators")
        _enter_patch(stack, "components.maas_billing.prep.ensure_maas_gateway_before_models_as_service")
        _enter_patch(stack, "components.maas_billing.prep.ensure_maas_database")
        _enter_patch(stack, "components.maas_billing.prep.cleanup_maas_smoke_leaked_rbac")
        _enter_patch(stack, "components.maas_billing.prep.cleanup_maas_smoke_stale_gateway_leaks")
        _enter_patch(stack, "components.maas_billing.prep.ensure_maas_bbr_pre_processing")
        _enter_patch(stack, "components.maas_billing.prep.ensure_user_workload_monitoring")
        _enter_patch(
            stack,
            "components.maas_billing.prep.ensure_maas_authorino_ready",
            return_value="authorino",
        )
        _enter_patch(stack, "components.maas_billing.prep.maas_api_deployment_exists", return_value=False)
        auth_policy = _enter_patch(stack, "components.maas_billing.prep.ensure_maas_api_auth_policy")
        mark_attempted = _enter_patch(stack, "components.maas_billing.prep.mark_maas_smoke_prep_attempted")
        maas_prep.try_prepare_maas_smoke()
    auth_policy.assert_not_called()
    mark_attempted.assert_not_called()
