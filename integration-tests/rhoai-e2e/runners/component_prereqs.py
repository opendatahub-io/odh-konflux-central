"""Per-component cluster prep before pytest (idempotent; failures do not block other components)."""

from __future__ import annotations

import os
import sys
import time
from pathlib import Path

from components.dashboard_cypress.verify_route import verify_dashboard_route_for_prepare
from suite.constants import is_test_only_product
from install.dsc_install import (
    _dsc_smoke_managed_components,
    batch_ensure_dsc_managed_for_smoke,
    components_need_models_as_service,
    dsc_component_management_state,
    ensure_dsc_component_managed,
    ensure_dsc_component_removed,
    reconcile_stale_dsc_components_for_smoke,
    smoke_enables_models_as_service,
    oc_run,
)
from install.ldap import install_identity_providers
from install.rhoai_gateway_prep import ensure_rhoai_gateway_stack_for_components
from install.rosa_hcp_imagestream_mirror import ensure_rosa_hcp_imagestream_mirror
from suite.cluster_api_health import cluster_smoke_infra_blocked_reason, is_definitive_infra_error
from steps.cluster_prep_state import (
    cluster_prep_already_done,
    mark_cluster_prep_done,
    resolve_artifacts_dir,
)

_IDP_COMPONENTS = frozenset({"dashboard_cypress", "maas_billing", "llama_stack", "codeflare_sdk"})
_dsc_batch_applied = False
_MODEL_REGISTRY_NS = "rhoai-model-registries"
_MODEL_CATALOG_DEPLOY = "model-catalog"
_MODEL_CATALOG_WAIT_SEC = 120
_MODEL_CATALOG_POLL_SEC = 15

__all__ = [
    "cluster_prep_already_done",
    "mark_cluster_prep_done",
    "prepare_component_for_smoke",
    "prepare_components_for_smoke",
    "refresh_maas_smoke_before_pytest",
    "run_pooled_external_smoke_prep",
]


def _modelregistry_managed_on_cluster() -> bool:
    from components.dashboard_cypress.config import _dsc_component_management_state

    return _dsc_component_management_state("modelregistry") == "Managed"


def _wait_model_catalog_for_dashboard() -> None:
    """SmokeSet1 Model Catalog tests need modelregistry Managed and model-catalog up."""
    if not _modelregistry_managed_on_cluster():
        print(
            "WARN: modelregistry not Managed on cluster; skipping model-catalog wait",
            file=sys.stderr,
            flush=True,
        )
        return
    deadline = time.time() + _MODEL_CATALOG_WAIT_SEC
    while time.time() < deadline:
        ns = oc_run(["get", "ns", _MODEL_REGISTRY_NS], check=False, capture_output=True, timeout=30)
        if ns.returncode != 0:
            time.sleep(_MODEL_CATALOG_POLL_SEC)
            continue
        dep = oc_run(
            [
                "get",
                "deployment",
                _MODEL_CATALOG_DEPLOY,
                "-n",
                _MODEL_REGISTRY_NS,
                "-o",
                "jsonpath={.status.availableReplicas}",
            ],
            check=False,
            capture_output=True,
            timeout=30,
        )
        if dep.returncode == 0:
            try:
                if int((dep.stdout or "0").strip() or "0") >= 1:
                    print(f"✓ {_MODEL_CATALOG_DEPLOY} ready in {_MODEL_REGISTRY_NS}", flush=True)
                    return
            except ValueError:
                pass
        time.sleep(_MODEL_CATALOG_POLL_SEC)
    raise RuntimeError(
        f"{_MODEL_CATALOG_DEPLOY} not ready in {_MODEL_REGISTRY_NS} after {_MODEL_CATALOG_WAIT_SEC}s"
    )


def _ensure_dsc_managed_for_component(smoke_id: str) -> None:
    if _dsc_batch_applied:
        return
    from install.dsc_install import _resolve_operator_version_for_dsc

    keys = list(
        _dsc_smoke_managed_components(
            smoke_id,
            operator_version=_resolve_operator_version_for_dsc(),
        )
    )
    if not keys:
        return
    if smoke_id == "ogx" and dsc_component_management_state("llamastackoperator") == "Managed":
        ensure_dsc_component_removed("llamastackoperator")
    for key in keys:
        ensure_dsc_component_managed(key)
    # model_server/model_runtime need spec.modelsAsService=Managed before the pytest gate;
    # maas_billing defers until maas-default-gateway exists (_try_prepare_maas_smoke).
    if smoke_id in ("model_server", "model_runtime") and "kserve" in keys:
        pass  # modelsAsService enabled after gateway HTTPS in ensure_maas_gateway_before_models_as_service
    # modelsAsService for maas_billing is enabled in _try_prepare_maas_smoke after gateway exists.
    from suite.component_dsc_gate import wait_for_smoke_dsc_ready_after_patch

    if smoke_id == "maas_billing":
        return
    wait_for_smoke_dsc_ready_after_patch(smoke_id)


def refresh_maas_smoke_before_pytest(*, component_id: str = "") -> None:
    """Re-apply MaaS state maas-controller may revert between dep-operators and pytest."""
    api_reason = cluster_smoke_infra_blocked_reason()
    if api_reason:
        raise RuntimeError(api_reason)

    from steps.cluster_prep_state import maas_gateway_https_blocked_reason

    blocked = maas_gateway_https_blocked_reason()
    if blocked:
        raise RuntimeError(blocked)

    from components.maas_billing.bbr_pre_processing import (
        ensure_maas_bbr_pre_processing,
        repair_payload_pre_processing_selector_conflict,
    )
    from components.maas_billing.cluster_cleanup import (
        cleanup_maas_smoke_leaked_rbac,
        cleanup_maas_smoke_stale_gateway_leaks,
        ensure_maas_gateway_auth_policy_alias,
    )
    from components.maas_billing.common import maas_smoke_acceptable_for_run
    from components.maas_billing.gateway import ensure_maas_gateway
    from components.maas_billing.prep import ensure_maas_gateway_before_models_as_service
    from components.maas_billing.timeouts import maas_gateway_prep_programmed_wait_sec
    from components.maas_billing.wait import _wait_maas_gateway_https_for_models_as_service
    from steps.cluster_prep_state import maas_gateway_mas_already_done

    cleanup_maas_smoke_leaked_rbac()
    if component_id == "maas_billing":
        cleanup_maas_smoke_stale_gateway_leaks()
    ensure_maas_gateway()
    repair_payload_pre_processing_selector_conflict()
    ensure_maas_bbr_pre_processing()
    ensure_maas_gateway_auth_policy_alias()
    if not maas_gateway_mas_already_done():
        try:
            ensure_maas_gateway_before_models_as_service()
            return
        except Exception as exc:
            if is_definitive_infra_error(str(exc)):
                raise
            print(
                f"WARN: MaaS gateway/modelsAsService refresh before pytest failed ({exc}); "
                "continuing with gateway Programmed wait",
                file=sys.stderr,
                flush=True,
            )
    acceptable, accept_reason = maas_smoke_acceptable_for_run()
    if acceptable:
        print(
            "Skipping MaaS gateway Programmed wait (smoke prerequisites already acceptable"
            f"{': ' + accept_reason[:120] if accept_reason else ''})",
            flush=True,
        )
        return
    _wait_maas_gateway_https_for_models_as_service(
        timeout_sec=maas_gateway_prep_programmed_wait_sec(),
    )


def _resync_maas_smoke_after_global_prep(smoke_id: str) -> bool:
    """Re-patch MaaS when prepare-components-prerequisites ran but DSC drifted before pytest."""
    ok = True
    print(f"MaaS resync for {smoke_id} (global cluster prep already done)", flush=True)
    try:
        _ensure_dsc_managed_for_component(smoke_id)
    except Exception as exc:
        ok = False
        print(
            f"WARN: MaaS DSC resync for {smoke_id}: {exc}",
            file=sys.stderr,
            flush=True,
        )
    if smoke_id == "maas_billing":
        try:
            from install.dsc_install import ensure_dsc_models_as_service

            ensure_dsc_models_as_service()
        except Exception as exc:
            ok = False
            print(
                f"WARN: could not re-enable modelsAsService for {smoke_id}: {exc}",
                file=sys.stderr,
                flush=True,
            )
    try:
        from components.maas_billing.timeouts import maas_resync_timeout_sec
        from components.maas_billing.wait import _wait_for_maas_smoke_ready

        _wait_for_maas_smoke_ready(timeout_sec=maas_resync_timeout_sec())
    except Exception as exc:
        ok = False
        print(
            f"WARN: MaaS resync wait for {smoke_id} timed out ({exc})",
            file=sys.stderr,
            flush=True,
        )
    return ok


def _external_existing_cluster() -> bool:
    from suite.its_trigger_params import is_external_cluster_source

    return is_external_cluster_source(os.environ.get("CLUSTER_SOURCE", "")) and is_test_only_product(
        os.environ.get("PRODUCT", "")
    )


def _external_cluster() -> bool:
    from suite.its_trigger_params import is_external_cluster_source

    return is_external_cluster_source(os.environ.get("CLUSTER_SOURCE", ""))


def run_pooled_external_smoke_prep(smoke_id: str) -> bool:
    """Per-component leak cleanup on external clusters (rhoai reinstall or existing)."""
    if not _external_cluster():
        return True
    ok = True
    if smoke_id == "model_registry":
        try:
            from components.model_registry.cluster_cleanup import cleanup_model_registry_smoke_leaks

            cleanup_model_registry_smoke_leaks()
        except Exception as exc:
            ok = False
            print(
                f"WARN: Model Registry cleanup for {smoke_id} failed ({exc}); "
                "pytest fixtures may hit AlreadyExists on pooled clusters",
                file=sys.stderr,
                flush=True,
            )
    if smoke_id == "model_runtime":
        try:
            from components.model_runtime.smoke_prep import cleanup_model_runtime_smoke_leaks

            cleanup_model_runtime_smoke_leaks()
        except Exception as exc:
            ok = False
            print(
                f"WARN: model_runtime cleanup for {smoke_id} failed ({exc}); "
                "vLLM fixtures may hit Namespace AlreadyExists (409)",
                file=sys.stderr,
                flush=True,
            )
    if smoke_id == "mlflow" and _external_cluster():
        try:
            from components.mlflow.smoke_prep import ensure_mlflow_smoke_ready_on_existing

            ensure_mlflow_smoke_ready_on_existing()
        except Exception as exc:
            ok = False
            print(
                f"WARN: MLflow smoke prep for {smoke_id} failed ({exc}); "
                "mlflow-tests may time out with 0/0 deployment replicas",
                file=sys.stderr,
                flush=True,
            )
    if smoke_id == "ai_pipelines":
        try:
            from components.ai_pipelines.smoke_prep import cleanup_ai_pipelines_smoke_leaks

            cleanup_ai_pipelines_smoke_leaks()
        except Exception as exc:
            ok = False
            print(
                f"WARN: AI Pipelines cleanup for {smoke_id} failed ({exc}); "
                "DSPA deploy may time out on pooled clusters",
                file=sys.stderr,
                flush=True,
            )
    if smoke_id == "kuberay":
        try:
            from components.kuberay.smoke_prep import cleanup_kuberay_smoke_leaks

            cleanup_kuberay_smoke_leaks()
        except Exception as exc:
            ok = False
            print(
                f"WARN: KubeRay cleanup for {smoke_id} failed ({exc}); "
                "RayCluster smoke may time out on pooled clusters",
                file=sys.stderr,
                flush=True,
            )
    return ok


def _ensure_maas_database_before_smoke_prep(component_ids: set[str]) -> None:
    """Prepare MaaS DB/UWM before BVT and DSC batch waits.

    install-dep-operators defers DB setup until ``redhat-ods-applications`` exists
    (post install-rhoai). modelsAsService is enabled only after gateway HTTPS in
    ``ensure_maas_gateway_before_models_as_service`` (before DSC batch).
    """
    from install.dsc_install import components_need_models_as_service

    if not components_need_models_as_service(component_ids):
        return
    from components.maas_billing.database import ensure_maas_database
    from components.maas_billing.uwm import ensure_user_workload_monitoring

    ensure_maas_database()
    ensure_user_workload_monitoring()


def prepare_component_for_smoke(smoke_id: str) -> bool:
    """Best-effort prereqs for one catalog component; warns and continues on failure."""
    smoke_id = smoke_id.strip()
    if not smoke_id:
        return True
    pooled_ok = run_pooled_external_smoke_prep(smoke_id)
    if cluster_prep_already_done():
        if smoke_enables_models_as_service({smoke_id}):
            return _resync_maas_smoke_after_global_prep(smoke_id) and pooled_ok
        return pooled_ok

    from suite.component_catalog import default_components_smoke_config_path, load_components_smoke_catalog
    from suite.component_version_gate import version_skip_reason_for_component

    try:
        catalog = load_components_smoke_catalog(default_components_smoke_config_path())
        comp = catalog.components.get(smoke_id)
        skip_reason = version_skip_reason_for_component(comp) if comp else ""
    except Exception as exc:
        print(f"WARN: could not load smoke catalog for {smoke_id}: {exc}", file=sys.stderr, flush=True)
        comp, skip_reason = None, ""
    if skip_reason:
        print(f"Skipping DSC prep for {smoke_id}: version gate ({skip_reason})", flush=True)
        return pooled_ok

    ok = True

    try:
        _ensure_dsc_managed_for_component(smoke_id)
    except Exception as exc:
        ok = False
        print(
            f"WARN: could not sync DSC for {smoke_id}: {exc}",
            file=sys.stderr,
            flush=True,
        )

    if smoke_enables_models_as_service({smoke_id}):
        try:
            from steps.cluster_prep_state import maas_gateway_https_blocked_reason

            blocked = maas_gateway_https_blocked_reason()
            if blocked:
                raise RuntimeError(blocked)
            from components.maas_billing.prep import try_prepare_maas_smoke

            try_prepare_maas_smoke()
        except Exception as exc:
            ok = False
            print(
                f"WARN: MaaS prerequisites for {smoke_id} not ready ({exc}); "
                "component task will record a failure if probes still fail",
                file=sys.stderr,
                flush=True,
            )

    if smoke_id in _IDP_COMPONENTS:
        try:
            install_identity_providers()
        except Exception as exc:
            ok = False
            print(
                f"WARN: identity providers for {smoke_id} not ready ({exc}); "
                "component task will record a failure if probes still fail",
                file=sys.stderr,
                flush=True,
            )

    if smoke_id == "dashboard_cypress":
        try:
            ensure_rhoai_gateway_stack_for_components({smoke_id})
        except Exception as exc:
            ok = False
            print(
                f"WARN: RHOAI gateway prep for {smoke_id} failed ({exc}); "
                "continuing with dashboard route verify",
                file=sys.stderr,
                flush=True,
            )
        try:
            _wait_model_catalog_for_dashboard()
        except Exception as exc:
            ok = False
            print(
                f"WARN: model catalog not ready for {smoke_id} ({exc}); "
                "Model Catalog Cypress tests may fail",
                file=sys.stderr,
                flush=True,
            )
        artifacts_raw = os.environ.get("ARTIFACTS_DIR", "").strip()
        artifacts = Path(artifacts_raw) if artifacts_raw else None
        existing_cfg = (artifacts / "dashboard-cypress-config.yml") if artifacts else None
        if existing_cfg and existing_cfg.is_file():
            print(
                f"Skipping dashboard route verify ({existing_cfg} from verify-operator-ready)",
                flush=True,
            )
        else:
            try:
                verify_dashboard_route_for_prepare(artifacts_dir=artifacts)
            except Exception as exc:
                ok = False
                print(
                    f"WARN: dashboard route verify for {smoke_id} failed ({exc}); "
                    "Cypress orchestrate may skip if DashboardReady or gateway URL stay unavailable",
                    file=sys.stderr,
                    flush=True,
                )

    if smoke_id == "workbenches" and is_test_only_product(os.environ.get("PRODUCT", "")):
        try:
            ensure_rosa_hcp_imagestream_mirror()
        except Exception as exc:
            ok = False
            print(
                f"WARN: ROSA HCP workbench ImageStream mirror for {smoke_id} failed ({exc}); "
                "notebook image imports may stay broken on HyperShift",
                file=sys.stderr,
                flush=True,
            )

    if smoke_id == "maas_billing":
        from components.maas_billing.oidc_users import (
            ensure_maas_oidc_keycloak_users,
            ensure_maas_tenant_external_oidc,
        )

        try:
            ensure_maas_oidc_keycloak_users()
        except Exception as exc:
            ok = False
            print(
                f"WARN: MaaS OIDC Keycloak users not ready ({exc}); "
                "OIDC smoke may fail until openshift-ai-maas realm users exist",
                file=sys.stderr,
                flush=True,
            )
        try:
            ensure_maas_tenant_external_oidc()
        except Exception as exc:
            ok = False
            print(
                f"WARN: MaaS Tenant externalOIDC not ready ({exc}); "
                "OIDC API-key smoke may fail until AuthPolicy JWT auth propagates",
                file=sys.stderr,
                flush=True,
            )

    if smoke_id == "codeflare_sdk":
        try:
            from components.codeflare_sdk.kueue_prep import ensure_codeflare_kueue_ready

            ensure_codeflare_kueue_ready()
        except Exception as exc:
            ok = False
            print(
                f"WARN: Kueue prerequisites for {smoke_id} not ready ({exc}); "
                "RayJob smoke may fail until Kueue CRDs are available",
                file=sys.stderr,
                flush=True,
            )

    if smoke_id == "platform":
        try:
            from components.platform.prep import ensure_platform_smoke_prereqs

            ensure_platform_smoke_prereqs()
        except Exception as exc:
            ok = False
            print(
                f"WARN: platform MaaS prerequisites not ready ({exc}); "
                "group_4 modelsasservice smoke may fail until modelsAsAService is ready",
                file=sys.stderr,
                flush=True,
            )

    return ok and pooled_ok


def prepare_components_for_smoke(component_ids: set[str]) -> bool:
    """Best-effort prereqs for all selected components (prepare task; never raises)."""
    global _dsc_batch_applied
    if not component_ids:
        return True
    if cluster_prep_already_done():
        return True
    from install.dsc_install import dsc_crd_available

    try:
        crd_available = dsc_crd_available()
    except Exception as exc:
        print(f"WARN: could not check DataScienceCluster CRD availability: {exc}", file=sys.stderr, flush=True)
        crd_available = False
    if not crd_available:
        print(
            "NOTE: skipping component cluster prep until DataScienceCluster CRD exists "
            "(defer to opendatahub-tests-prepare after install-rhoai)",
            flush=True,
        )
        return True
    try:
        from helpers.hypershift_admission_webhooks import (
            neutralize_broken_hypershift_admission_webhooks,
        )

        neutralize_broken_hypershift_admission_webhooks()
    except Exception as exc:
        print(
            f"WARN: HyperShift admission webhook neutralize failed ({exc}); "
            "OGX/Deployment creates may fail on stub webhooks",
            file=sys.stderr,
            flush=True,
        )
    try:
        _ensure_maas_database_before_smoke_prep(component_ids)
    except Exception as exc:
        print(
            f"WARN: MaaS DB/UWM prep before smoke matrix failed ({exc}); "
            "BVT and MaaS smoke may fail until maas-db-config exists",
            file=sys.stderr,
            flush=True,
        )
    if components_need_models_as_service(component_ids):
        try:
            # try_prepare_maas_smoke retries RHCL when the incomplete marker survives
            # install-dep-operators; ensure_maas_gateway_* alone skipped that path.
            from components.maas_billing.prep import try_prepare_maas_smoke

            try_prepare_maas_smoke()
        except Exception as exc:
            from steps.cluster_prep_state import mark_maas_gateway_https_failed

            mark_maas_gateway_https_failed(str(exc))
            print(
                f"WARN: MaaS gateway/modelsAsService prep before smoke matrix failed ({exc}); "
                "BVT and MaaS smoke may fail until maas-default-gateway is programmed",
                file=sys.stderr,
                flush=True,
            )
    try:
        reconcile_stale_dsc_components_for_smoke(component_ids)
    except Exception as exc:
        print(
            f"WARN: could not reconcile stale DSC components: {exc}",
            file=sys.stderr,
            flush=True,
        )
    try:
        batch_ensure_dsc_managed_for_smoke(component_ids)
        from suite.component_dsc_gate import wait_for_smoke_dsc_ready_batch

        wait_for_smoke_dsc_ready_batch(component_ids)
        _dsc_batch_applied = True
    except Exception as exc:
        _dsc_batch_applied = False
        print(
            f"WARN: batch DSC prep failed ({exc}); falling back to per-component DSC sync",
            file=sys.stderr,
            flush=True,
        )
    ok = True
    for cid in sorted(component_ids):
        try:
            component_ok = prepare_component_for_smoke(cid)
        except Exception as exc:
            component_ok = False
            print(
                f"WARN: prerequisites for {cid} raised ({exc}); continuing with remaining components",
                file=sys.stderr,
                flush=True,
            )
        ok = component_ok and ok
    try:
        reconcile_stale_dsc_components_for_smoke(component_ids)
    except Exception as exc:
        print(
            f"WARN: could not reconcile stale DSC components after prep: {exc}",
            file=sys.stderr,
            flush=True,
        )
    try:
        from suite.dsc_baseline import capture_dsc_baseline

        baseline_dir = resolve_artifacts_dir()
        if baseline_dir:
            capture_dsc_baseline(baseline_dir)
    except Exception as exc:
        print(
            f"WARN: could not capture DSC baseline: {exc}",
            file=sys.stderr,
            flush=True,
        )
    _dsc_batch_applied = False
    return ok
