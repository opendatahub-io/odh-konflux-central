"""MaaS smoke surface prep (gateway, DB, auth). Shared by prepare and install-dep-operators."""

from __future__ import annotations

from components.maas_billing.auth import (
    ensure_maas_auth_policy_ready,
    ensure_maas_authorino_ready,
)
from components.maas_billing.auth import ensure_authorino_tls
from components.maas_billing.bbr_pre_processing import (
    ensure_maas_bbr_pre_processing,
    repair_payload_pre_processing_selector_conflict,
)
from components.maas_billing.common import maas_api_deployment_exists
from components.maas_billing.cluster_cleanup import (
    cleanup_maas_smoke_leaked_rbac,
    cleanup_maas_smoke_stale_gateway_leaks,
    ensure_maas_gateway_auth_policy_alias,
)
from components.maas_billing.database import ensure_maas_database
from components.maas_billing.gateway import (
    ensure_maas_api_auth_policy,
    ensure_maas_gateway,
    ensure_maas_gateway_ingress_tls_secret,
    ensure_maas_gateway_route,
)
from components.maas_billing.timeouts import (
    maas_gateway_prep_programmed_wait_sec,
    maas_prep_timeout_sec,
)
from components.maas_billing.uwm import ensure_user_workload_monitoring
from components.maas_billing.wait import (
    _wait_for_maas_smoke_ready,
    _wait_maas_gateway_https_for_models_as_service,
)
from install.dependency_operators import (
    existing_smoke_without_install_dependencies,
    require_maas_dependency_operators,
)
from install.dsc_install import ensure_dsc_models_as_service
from install.rhcl_deps import ensure_maas_rhcl_dependency_stack
from steps.cluster_prep_state import (
    dep_operators_already_done,
    maas_gateway_mas_already_done,
    maas_smoke_prep_attempted,
    maas_smoke_surface_already_done,
    mark_maas_gateway_mas_done,
    mark_maas_smoke_prep_attempted,
    mark_maas_smoke_surface_done,
)


def _restart_maas_api_after_gateway() -> None:
    """Restart maas-api when it already exists so it picks up the gateway HTTPS service."""
    if not maas_api_deployment_exists():
        return
    from components.maas_billing.auth import _rollout_restart_deployment
    from components.maas_billing.common import maas_api_namespace

    _rollout_restart_deployment(maas_api_namespace(), "maas-api", timeout_sec=120)
    print("✓ Restarted maas-api after gateway HTTPS service prep", flush=True)


def ensure_maas_gateway_before_models_as_service(*, https_wait_sec: int | None = None) -> None:
    """Gateway HTTPS service must exist before modelsAsService enables maas-api."""
    from components.maas_billing.auth import recover_kuadrant_after_gateway_api_provider
    from components.maas_billing.gateway import ensure_openshift_default_gateway_class
    from helpers.hypershift_admission_webhooks import (
        neutralize_broken_hypershift_admission_webhooks,
    )
    from steps.cluster_prep_state import maas_gateway_https_blocked_reason

    # HyperShift stub webhooks fail-closed and can block gateway Service / Deployments.
    neutralize_broken_hypershift_admission_webhooks()
    ensure_openshift_default_gateway_class()
    # cleanup+reinstall: Kuadrant often stuck MissingDependency until GatewayClass exists;
    # restart operator once provider is present so MaaS smoke can run.
    recover_kuadrant_after_gateway_api_provider()
    blocked = maas_gateway_https_blocked_reason()
    if blocked:
        raise RuntimeError(blocked)
    from components.maas_billing.common import _dsc_condition, models_as_service_ready_condition_type

    maas_ready_type = models_as_service_ready_condition_type()

    maas_status, _, _ = _dsc_condition(maas_ready_type)
    if maas_gateway_mas_already_done() and maas_status == "True":
        print("Skipping duplicate MaaS gateway/modelsAsService prep (already done this run)", flush=True)
        return
    if maas_gateway_mas_already_done() and maas_status != "True":
        print(
            f"NOTE: MaaS gateway marker set but {maas_ready_type}≠True; "
            "re-running modelsAsService prep",
            flush=True,
        )
    ensure_maas_gateway_ingress_tls_secret()
    ensure_authorino_tls()
    ensure_maas_gateway()
    ensure_maas_gateway_route()
    timeout = https_wait_sec if https_wait_sec is not None else maas_gateway_prep_programmed_wait_sec()
    _wait_maas_gateway_https_for_models_as_service(timeout_sec=timeout)
    # A cleanup reinstall can leave kserve controllers still converging after the
    # gateway service is ready.  Keep the AIGateway reconcile within the full
    # MaaS preparation budget instead of the install helper's short default.
    wait_for_aigateway = maas_api_deployment_exists()
    if not wait_for_aigateway and dep_operators_already_done():
        # install-rhoai finished: operator should create maas-api once modelsAsAService
        # reconciles — waiting here is required (defer only applies pre-install-rhoai).
        wait_for_aigateway = True
        print(
            "NOTE: maas-api not deployed yet; install-rhoai complete — "
            "waiting for AIGateway modelsAsAService reconcile",
            flush=True,
        )
    elif not wait_for_aigateway:
        print(
            "NOTE: maas-api not deployed yet; patching DSC modelsAsAService only and "
            "deferring AIGateway reconcile wait to prepare-components-prerequisites",
            flush=True,
        )
    ensure_dsc_models_as_service(
        wait_timeout_sec=maas_prep_timeout_sec(),
        wait_for_aigateway=wait_for_aigateway,
    )
    _restart_maas_api_after_gateway()
    mark_maas_gateway_mas_done()


def try_prepare_maas_smoke(*, force_retry: bool = False) -> None:
    """MaaS smoke surface prep (gateway, DB, auth policies). RHCL runs in install-dep-operators."""
    from install.dsc_install import dsc_crd_available

    if not dsc_crd_available():
        print(
            "NOTE: skipping MaaS smoke prep until DataScienceCluster CRD exists (post install-rhoai)",
            flush=True,
        )
        return
    repaired = repair_payload_pre_processing_selector_conflict()
    if maas_smoke_surface_already_done() and not repaired:
        print("Skipping duplicate MaaS smoke prep (surface already prepared this run)", flush=True)
        return
    if maas_smoke_prep_attempted() and not repaired and not force_retry:
        print(
            "Skipping duplicate MaaS smoke prep (auth/readiness wait already attempted this run)",
            flush=True,
        )
        return
    if not dep_operators_already_done():
        if existing_smoke_without_install_dependencies():
            require_maas_dependency_operators()
        else:
            ensure_maas_rhcl_dependency_stack()
            require_maas_dependency_operators()
    else:
        print(
            "Skipping RHCL/dependency-operator setup (install-dep-operators already completed)",
            flush=True,
        )
        # After cleanup+reinstall, install-dep-operators may leave a stale incomplete
        # marker (Kuadrant race before RHOAI). Re-probe live stack; if still incomplete,
        # retry RHCL post-install now that the operator apps namespace exists.
        from helpers.gateway_stack_marker import (
            gateway_stack_incomplete,
            reconcile_gateway_stack_incomplete_marker,
        )

        if gateway_stack_incomplete() and not reconcile_gateway_stack_incomplete_marker():
            print(
                "Retrying RHCL/Kuadrant post-install after install-dep incomplete marker "
                "(post install-rhoai / DSC available)...",
                flush=True,
            )
            ensure_maas_rhcl_dependency_stack()
            require_maas_dependency_operators(allow_deferred_authorino=True)
    ensure_maas_gateway_before_models_as_service()
    ensure_maas_database()
    cleanup_maas_smoke_leaked_rbac()
    cleanup_maas_smoke_stale_gateway_leaks()
    ensure_maas_bbr_pre_processing()
    ensure_user_workload_monitoring()
    authorino_ns = ensure_maas_authorino_ready()
    # install-dep-operators runs before install-rhoai; maas-api is created by the operator
    # after modelsAsAService reconcile. Defer auth-policy wait to post-install prep (wljpv fix
    # still applies when dep_operators_already_done).
    if not maas_api_deployment_exists() and not dep_operators_already_done():
        print(
            "NOTE: maas-api not deployed yet (install-rhoai pending); "
            "deferring MaaS API auth policy wait to prepare-components-prerequisites",
            flush=True,
        )
        return
    try:
        # Operator creates maas-api after ModelsAsService reconcile; wait DSC first (n2bqt/4sknz).
        _wait_for_maas_smoke_ready(timeout_sec=maas_prep_timeout_sec())
        ensure_maas_api_auth_policy()
        ensure_maas_gateway_auth_policy_alias()
        ensure_maas_auth_policy_ready(authorino_ns=authorino_ns)
        mark_maas_smoke_surface_done()
    finally:
        mark_maas_smoke_prep_attempted()
