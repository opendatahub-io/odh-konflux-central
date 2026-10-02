"""Platform golang e2e prereqs (MaaS before group_4 modelsasservice smoke tests)."""

from __future__ import annotations


def ensure_platform_smoke_prereqs() -> None:
    """Ensure modelsAsAService is Managed and MaaS smoke surface is ready on RHOAI 3.5+."""
    from install.dsc_install import ensure_dsc_models_as_service, uses_aigateway_models_as_a_service

    if not uses_aigateway_models_as_a_service():
        return
    ensure_dsc_models_as_service()
    from components.maas_billing.prep import try_prepare_maas_smoke

    try_prepare_maas_smoke(force_retry=True)
