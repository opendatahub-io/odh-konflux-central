"""BBR/IPP EnvoyFilter naming for MaaS smoke."""

from __future__ import annotations

from unittest.mock import patch

from components.maas_billing import bbr_pre_processing as mod

def test_normalize_envoy_filter_spec_renames_bbr_pre_to_ipp_pre() -> None:
    spec = {
        "configPatches": [
            {"patch": {"value": {"name": "envoy.filters.http.ext_proc.bbr-pre"}}},
            {"patch": {"value": {"name": "envoy.filters.http.ext_proc.bbr"}}},
        ]
    }
    out = mod._normalize_envoy_filter_spec(spec)
    names = [p["patch"]["value"]["name"] for p in out["configPatches"]]
    assert names == [
        "envoy.filters.http.ext_proc.ipp-pre",
        "envoy.filters.http.ext_proc.bbr",
    ]

def test_legacy_pre_auth_filter_only() -> None:
    assert mod._legacy_pre_auth_filter_only(["envoy.filters.http.ext_proc.bbr-pre"])
    assert not mod._legacy_pre_auth_filter_only(["envoy.filters.http.ext_proc.ipp-pre"])


def test_models_as_service_selector_conflict_detects_immutable_selector() -> None:
    with patch.object(
        mod,
        "_dsc_condition",
        return_value=(
            "False",
            "PlatformReconcileFailed",
            'apply Deployment openshift-ingress/payload-pre-processing: spec.selector: Invalid value: field is immutable',
        ),
    ):
        assert mod._models_as_service_selector_conflict()


def test_repair_payload_pre_processing_deletes_stale_deployment() -> None:
    with (
        patch.object(mod, "_models_as_service_selector_conflict", return_value=True),
        patch.object(mod, "_wait_models_as_service_after_repair"),
        patch.object(mod, "oc_run") as oc_run,
    ):
        oc_run.side_effect = [
            type("R", (), {"returncode": 0})(),
            type("R", (), {"returncode": 0})(),
        ]
        assert mod.repair_payload_pre_processing_selector_conflict() is True
        assert oc_run.call_args_list[-1][0][0][:2] == ["delete", "deployment"]


def test_cleanup_stale_maas_ingress_workloads_deletes_both_deployments() -> None:
    with (
        patch.object(mod, "ensure_maas_controller_openshift_ingress_hpa_rbac"),
        patch.object(mod, "oc_run") as oc_run,
    ):
        oc_run.return_value = type("R", (), {"returncode": 0, "stdout": "deleted", "stderr": ""})()
        mod.cleanup_stale_maas_ingress_workloads()
    assert oc_run.call_count == 4


def test_ensure_maas_controller_openshift_ingress_hpa_rbac_skips_when_can_i_yes() -> None:
    with (
        patch.object(mod, "_maas_controller_can_manage_openshift_ingress_hpa", return_value=True),
        patch.object(mod, "oc_run") as oc_run,
    ):
        mod.ensure_maas_controller_openshift_ingress_hpa_rbac()
        oc_run.assert_not_called()


def test_ensure_maas_controller_openshift_ingress_hpa_rbac_checks_every_verb() -> None:
    with (
        patch.object(mod, "_HPA_RBAC_RULE", {"verbs": ["get", "patch"]}),
        patch.object(mod, "oc_run") as oc_run,
    ):
        oc_run.return_value = type("R", (), {"returncode": 0, "stdout": "yes", "stderr": ""})()
        mod._maas_controller_can_manage_openshift_ingress_hpa()
        assert oc_run.call_count == 2
        assert oc_run.call_args_list[0][0][0][2] == "get"
        assert oc_run.call_args_list[1][0][0][2] == "patch"


def test_ensure_maas_controller_openshift_ingress_hpa_rbac_applies_role_binding() -> None:
    with (
        patch.object(
            mod,
            "_maas_controller_can_manage_openshift_ingress_hpa",
            side_effect=[False, True],
        ),
        patch.object(mod, "oc_run") as oc_run,
    ):
        oc_run.return_value = type("R", (), {"returncode": 0, "stdout": "", "stderr": ""})()
        mod.ensure_maas_controller_openshift_ingress_hpa_rbac()
        assert oc_run.call_count == 2
        assert oc_run.call_args_list[0][0][0][:2] == ["apply", "-f"]
        assert mod._OLMINSTALL_MAAS_INGRESS_RBAC_ROLE in oc_run.call_args_list[0].kwargs["stdin_text"]


def test_repair_payload_pre_processing_noop_when_dsc_ready() -> None:
    with patch.object(mod, "_models_as_service_selector_conflict", return_value=False):
        assert mod.repair_payload_pre_processing_selector_conflict() is False


def test_ensure_maas_bbr_pre_processing_skips_without_envoyfilter_crd() -> None:
    with (
        patch.object(mod, "ensure_maas_controller_openshift_ingress_hpa_rbac"),
        patch.object(mod, "repair_payload_pre_processing_selector_conflict"),
        patch.object(mod, "_envoyfilter_crd_available", return_value=False),
        patch.object(mod, "_envoy_filter_stage_names") as stages,
    ):
        mod.ensure_maas_bbr_pre_processing()
        stages.assert_not_called()


def test_ensure_maas_bbr_pre_processing_skips_without_payload_image() -> None:
    with (
        patch.object(mod, "ensure_maas_controller_openshift_ingress_hpa_rbac"),
        patch.object(mod, "repair_payload_pre_processing_selector_conflict"),
        patch.object(mod, "_envoyfilter_crd_available", return_value=True),
        patch.object(mod, "_envoy_filter_stage_names", return_value=[]),
        patch.object(mod, "oc_run", return_value=type("R", (), {"returncode": 1})()),
        patch.object(mod, "_resolve_payload_processing_image", return_value=""),
    ):
        mod.ensure_maas_bbr_pre_processing()


def test_resolve_payload_processing_image_prefers_post_auth() -> None:
    with patch.object(mod, "_deployment_image", side_effect=lambda _ns, name: f"img:{name}"):
        assert mod._resolve_payload_processing_image() == f"img:{mod._BBR_POST_DEPLOY}"
