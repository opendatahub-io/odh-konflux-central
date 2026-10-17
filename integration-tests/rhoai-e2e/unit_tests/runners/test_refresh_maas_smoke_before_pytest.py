"""refresh_maas_smoke_before_pytest re-applies gateway annotations before pytest."""

from __future__ import annotations

from unittest.mock import patch

from runners.component_prereqs import refresh_maas_smoke_before_pytest


def test_refresh_maas_smoke_before_pytest_retries_gateway_mas_when_not_done() -> None:
    with (
        patch("components.maas_billing.cluster_cleanup.cleanup_maas_smoke_leaked_rbac") as cleanup,
        patch(
            "components.maas_billing.cluster_cleanup.cleanup_maas_smoke_stale_gateway_leaks"
        ) as gateway_leaks,
        patch("components.maas_billing.gateway.ensure_maas_gateway") as gateway,
        patch("components.maas_billing.bbr_pre_processing.ensure_maas_bbr_pre_processing") as bbr,
        patch(
            "components.maas_billing.cluster_cleanup.ensure_maas_gateway_auth_policy_alias"
        ) as alias,
        patch("steps.cluster_prep_state.maas_gateway_mas_already_done", return_value=False),
        patch(
            "components.maas_billing.prep.ensure_maas_gateway_before_models_as_service"
        ) as gateway_mas,
    ):
        refresh_maas_smoke_before_pytest(component_id="model_server")

    cleanup.assert_called_once()
    gateway_leaks.assert_not_called()
    gateway.assert_called_once()
    bbr.assert_called_once()
    alias.assert_called_once()
    gateway_mas.assert_called_once()


def test_refresh_maas_smoke_prunes_gateways_for_maas_billing() -> None:
    with (
        patch("components.maas_billing.cluster_cleanup.cleanup_maas_smoke_leaked_rbac"),
        patch(
            "components.maas_billing.cluster_cleanup.cleanup_maas_smoke_stale_gateway_leaks"
        ) as gateway_leaks,
        patch("components.maas_billing.gateway.ensure_maas_gateway"),
        patch("components.maas_billing.bbr_pre_processing.ensure_maas_bbr_pre_processing"),
        patch("components.maas_billing.cluster_cleanup.ensure_maas_gateway_auth_policy_alias"),
        patch("steps.cluster_prep_state.maas_gateway_mas_already_done", return_value=False),
        patch("components.maas_billing.prep.ensure_maas_gateway_before_models_as_service"),
    ):
        refresh_maas_smoke_before_pytest(component_id="maas_billing")

    gateway_leaks.assert_called_once()


def test_refresh_maas_smoke_before_pytest_waits_programmed_when_mas_done() -> None:
    with (
        patch("components.maas_billing.cluster_cleanup.cleanup_maas_smoke_leaked_rbac"),
        patch("components.maas_billing.gateway.ensure_maas_gateway"),
        patch("components.maas_billing.bbr_pre_processing.ensure_maas_bbr_pre_processing"),
        patch("components.maas_billing.cluster_cleanup.ensure_maas_gateway_auth_policy_alias"),
        patch("steps.cluster_prep_state.maas_gateway_mas_already_done", return_value=True),
        patch(
            "components.maas_billing.common.maas_smoke_acceptable_for_run",
            return_value=(False, "not ready"),
        ),
        patch("components.maas_billing.wait._wait_maas_gateway_https_for_models_as_service") as programmed,
        patch(
            "components.maas_billing.timeouts.maas_gateway_prep_programmed_wait_sec",
            return_value=300,
        ),
    ):
        refresh_maas_smoke_before_pytest()

    programmed.assert_called_once_with(timeout_sec=300)
