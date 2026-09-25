"""Unit tests for platform golang smoke prep and command tweaks."""

from __future__ import annotations

from unittest.mock import patch

from components.platform.prep import ensure_platform_smoke_prereqs
from components.platform.smoke import prepend_platform_smoke_command


def test_prepend_platform_smoke_command_creates_results_dir() -> None:
    assert prepend_platform_smoke_command("bash run_e2e_tests.sh") == (
        "mkdir -p results && bash run_e2e_tests.sh"
    )


def test_prepend_platform_smoke_command_empty() -> None:
    assert prepend_platform_smoke_command("") == ""


def test_ensure_platform_smoke_prereqs_skips_pre_35() -> None:
    with (
        patch("install.dsc_install.uses_aigateway_models_as_a_service", return_value=False),
        patch("install.dsc_install.ensure_dsc_models_as_service") as mas,
        patch("components.maas_billing.prep.try_prepare_maas_smoke") as prep,
    ):
        ensure_platform_smoke_prereqs()
    mas.assert_not_called()
    prep.assert_not_called()


def test_ensure_platform_smoke_prereqs_on_35_plus() -> None:
    with (
        patch("install.dsc_install.uses_aigateway_models_as_a_service", return_value=True),
        patch("install.dsc_install.ensure_dsc_models_as_service") as mas,
        patch("components.maas_billing.prep.try_prepare_maas_smoke") as prep,
    ):
        ensure_platform_smoke_prereqs()
    mas.assert_called_once()
    prep.assert_called_once_with(force_retry=True)
