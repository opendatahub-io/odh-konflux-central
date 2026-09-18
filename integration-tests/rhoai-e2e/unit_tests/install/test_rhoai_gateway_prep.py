#!/usr/bin/env python3
"""Unit tests for RHOAI gateway install prep (§15)."""

from __future__ import annotations

import json
import unittest
from unittest import mock

from install import approve_transitive_installplans as approve_mod
from install import gateway_config as gw_mod
from install import rhoai_gateway_prep as gw_prep_mod
from install.dsc_install import _dsci_yaml, _initial_dsci_servicemesh_state, _smoke_components_need_servicemesh

class ApproveInstallPlansTest(unittest.TestCase):
    def test_approves_unapproved_plans(self) -> None:
        list_json = {
            "items": [
                {"metadata": {"name": "install-a"}, "spec": {"approved": False, "clusterServiceVersionNames": ["servicemeshoperator.v2.6.0"]}},
                {"metadata": {"name": "install-b"}, "spec": {"approved": True}},
                {"metadata": {"name": "install-c"}, "spec": {"approved": False, "clusterServiceVersionNames": ["unrelated-operator.v1.0.0"]}},
            ]
        }
        with mock.patch.object(approve_mod, "oc_run") as oc_run:
            oc_run.side_effect = [
                mock.Mock(returncode=0, stdout=json.dumps(list_json)),
                mock.Mock(returncode=0, stdout="", stderr=""),
            ]
            count = approve_mod.approve_pending_installplans("openshift-operators")
        self.assertEqual(count, 1)
        patch_call = oc_run.call_args_list[1][0][0]
        self.assertIn("patch", patch_call)
        self.assertIn("install-a", patch_call)

class GatewayConfigHelpersTest(unittest.TestCase):
    def test_gateway_oidc_client_id_prefers_odh_client_from_audiences(self) -> None:
        with (
            mock.patch.dict("os.environ", {}, clear=True),
            mock.patch.object(
                gw_mod,
                "_gateway_oidc_audiences",
                return_value=["ocp-console", "oc-cli", "odh-client"],
            ),
            mock.patch.object(gw_mod, "_cluster_is_byoidc", return_value=True),
        ):
            self.assertEqual(gw_mod._gateway_oidc_client_id(), "odh-client")

    def test_gateway_oidc_client_id_uses_first_byoidc_audience_when_no_odh_client(self) -> None:
        with (
            mock.patch.dict("os.environ", {}, clear=True),
            mock.patch.object(
                gw_mod,
                "_gateway_oidc_audiences",
                return_value=["ocp-console", "oc-cli"],
            ),
            mock.patch.object(gw_mod, "_cluster_is_byoidc", return_value=True),
        ):
            self.assertEqual(gw_mod._gateway_oidc_client_id(), "ocp-console")

    def test_gateway_config_ready_all_conditions(self) -> None:
        doc = {
            "status": {
                "conditions": [
                    {"type": "Ready", "status": "True"},
                    {"type": "ProvisioningSucceeded", "status": "True"},
                    {"type": "GatewayConfigReady", "status": "True"},
                ]
            }
        }
        with mock.patch.object(gw_mod, "_gateway_config_doc", return_value=doc):
            self.assertTrue(gw_mod.gateway_config_ready())

    def test_patch_skips_when_oidc_already_set(self) -> None:
        doc = {
            "spec": {
                "oidc": {
                    "issuerURL": "https://issuer",
                    "clientID": "odh-client",
                    "clientSecretRef": {"name": "keycloak-client-secret"},
                }
            }
        }
        with (
            mock.patch.object(gw_mod, "_cluster_is_byoidc", return_value=True),
            mock.patch.object(gw_mod, "_byoidc_issuer_url", return_value="https://issuer"),
            mock.patch.object(gw_mod, "_gateway_oidc_client_id", return_value="odh-client"),
            mock.patch.object(gw_mod, "_gateway_config_doc", return_value=doc),
            mock.patch.object(gw_mod, "sync_kube_auth_proxy_oidc_client", return_value=False) as sync_mock,
            mock.patch.object(gw_mod, "oc_run") as oc_run,
        ):
            changed = gw_mod.patch_gateway_config_oidc()
        self.assertFalse(changed)
        oc_run.assert_not_called()
        sync_mock.assert_called_once_with("odh-client")

    def test_patch_syncs_kube_auth_proxy_when_gateway_ok_but_secret_wrong(self) -> None:
        doc = {
            "spec": {
                "oidc": {
                    "issuerURL": "https://issuer",
                    "clientID": "odh-client",
                    "clientSecretRef": {"name": "keycloak-client-secret"},
                }
            }
        }
        with (
            mock.patch.object(gw_mod, "_cluster_is_byoidc", return_value=True),
            mock.patch.object(gw_mod, "_byoidc_issuer_url", return_value="https://issuer"),
            mock.patch.object(gw_mod, "_gateway_oidc_client_id", return_value="odh-client"),
            mock.patch.object(gw_mod, "_gateway_config_doc", return_value=doc),
            mock.patch.object(gw_mod, "sync_kube_auth_proxy_oidc_client", return_value=True) as sync_mock,
            mock.patch.object(gw_mod, "oc_run") as oc_run,
        ):
            changed = gw_mod.patch_gateway_config_oidc()
        self.assertTrue(changed)
        oc_run.assert_not_called()
        sync_mock.assert_called_once_with("odh-client")

    def test_malformed_oidc_client_id_detects_json_array(self) -> None:
        self.assertTrue(gw_mod._malformed_oidc_client_id('["ocp-console","odh-client"]'))
        self.assertFalse(gw_mod._malformed_oidc_client_id("odh-client"))

    def test_patch_ephc_waits_for_byoidc_signals(self) -> None:
        doc = {
            "spec": {
                "oidc": {
                    "issuerURL": "https://issuer",
                    "clientID": "odh-client",
                    "clientSecretRef": {"name": "keycloak-client-secret"},
                }
            }
        }
        with (
            mock.patch.dict("os.environ", {"CLUSTER_SOURCE": "EPHC"}, clear=False),
            mock.patch.object(gw_mod, "_cluster_is_byoidc", side_effect=[False, False, True]),
            mock.patch("install.ldap._byoidc_credentials_ready", return_value=False),
            mock.patch.object(gw_mod, "_cluster_has_oidc_provider", return_value=True),
            mock.patch.object(gw_mod, "_wait_for_byoidc_cluster_signals", return_value=True),
            mock.patch.object(gw_mod, "_byoidc_issuer_url", return_value="https://issuer"),
            mock.patch.object(gw_mod, "_gateway_oidc_client_id", return_value="odh-client"),
            mock.patch.object(gw_mod, "_gateway_config_doc", return_value=doc),
            mock.patch.object(gw_mod, "sync_kube_auth_proxy_oidc_client", return_value=False),
            mock.patch.object(gw_mod, "oc_run") as oc_run,
        ):
            changed = gw_mod.patch_gateway_config_oidc()
        self.assertFalse(changed)
        oc_run.assert_not_called()

    def test_patch_ephc_skips_byoidc_wait_when_no_oidc_provider(self) -> None:
        with (
            mock.patch.dict("os.environ", {"CLUSTER_SOURCE": "EPHC"}, clear=False),
            mock.patch.object(gw_mod, "_cluster_is_byoidc", return_value=False),
            mock.patch("install.ldap._byoidc_credentials_ready", return_value=False),
            mock.patch.object(gw_mod, "_cluster_has_oidc_provider", return_value=False),
            mock.patch.object(gw_mod, "_wait_for_byoidc_cluster_signals") as wait_mock,
        ):
            self.assertFalse(gw_mod.patch_gateway_config_oidc())
        wait_mock.assert_not_called()

    def test_gateway_oidc_configured_false_when_issuer_missing(self) -> None:
        with mock.patch.object(gw_mod, "_gateway_config_doc", return_value={"spec": {"oidc": {}}}):
            self.assertFalse(gw_mod.gateway_oidc_configured())

class ServicemeshOlmReconcileTest(unittest.TestCase):
    def test_removes_orphan_pending_csv_not_in_subscription_target(self) -> None:
        sub_json = {
            "items": [
                {
                    "metadata": {"name": "servicemeshoperator3"},
                    "status": {
                        "currentCSV": "servicemeshoperator3.v3.3.4",
                        "installedCSV": "servicemeshoperator3.v3.3.4",
                        "conditions": [{"type": "ResolutionFailed", "status": "True"}],
                    },
                }
            ]
        }
        csv_json = {
            "items": [
                {
                    "metadata": {"name": "servicemeshoperator3.v3.2.0"},
                    "status": {"phase": "Pending"},
                },
                {
                    "metadata": {"name": "servicemeshoperator3.v3.3.4"},
                    "status": {"phase": "Pending"},
                },
            ]
        }
        with mock.patch.object(gw_mod, "oc_run") as oc_run:
            oc_run.side_effect = [
                mock.Mock(returncode=0, stdout=json.dumps(sub_json)),
                mock.Mock(returncode=0, stdout=json.dumps(csv_json)),
                mock.Mock(returncode=0, stdout=json.dumps(sub_json)),
                mock.Mock(returncode=0, stdout=json.dumps(csv_json)),
                mock.Mock(returncode=0, stdout="", stderr=""),
                mock.Mock(returncode=0, stdout="", stderr=""),
                mock.Mock(returncode=0, stdout=json.dumps({"items": []})),
            ]
            removed = gw_mod.reconcile_servicemesh_olm_conflicts("openshift-operators")
        self.assertEqual(removed, 2)
        delete_call = oc_run.call_args_list[4][0][0]
        self.assertIn("delete", delete_call)
        self.assertIn("servicemeshoperator3.v3.2.0", delete_call)

    def test_removes_pending_installed_csv_during_upgrade(self) -> None:
        sub_json = {
            "items": [
                {
                    "metadata": {"name": "servicemeshoperator3"},
                    "status": {
                        "currentCSV": "servicemeshoperator3.v3.4.1",
                        "installedCSV": "servicemeshoperator3.v3.1.0",
                    },
                }
            ]
        }
        csv_json = {
            "items": [
                {
                    "metadata": {"name": "servicemeshoperator3.v3.1.0"},
                    "status": {"phase": "Pending"},
                },
                {
                    "metadata": {"name": "servicemeshoperator3.v3.4.1"},
                    "status": {"phase": "Installing"},
                },
            ]
        }
        with mock.patch.object(gw_mod, "oc_run") as oc_run:
            oc_run.side_effect = [
                mock.Mock(returncode=0, stdout=json.dumps(sub_json)),
                mock.Mock(returncode=0, stdout=json.dumps(csv_json)),
                mock.Mock(returncode=0, stdout=json.dumps(sub_json)),
                mock.Mock(returncode=0, stdout=json.dumps(csv_json)),
                mock.Mock(returncode=0, stdout="", stderr=""),
                mock.Mock(returncode=0, stdout=json.dumps({"items": []})),
            ]
            removed = gw_mod.reconcile_servicemesh_olm_conflicts("openshift-operators")
        self.assertEqual(removed, 1)
        delete_call = oc_run.call_args_list[4][0][0]
        self.assertIn("servicemeshoperator3.v3.1.0", delete_call)

    def test_recreate_subscription_when_installplan_missing(self) -> None:
        sub_json = {
            "items": [
                {
                    "metadata": {"name": "servicemeshoperator3"},
                    "spec": {
                        "channel": "stable",
                        "name": "servicemeshoperator3",
                        "source": "redhat-operators",
                        "sourceNamespace": "openshift-marketplace",
                        "installPlanApproval": "Manual",
                    },
                    "status": {
                        "currentCSV": "servicemeshoperator3.v3.3.5",
                        "installedCSV": "servicemeshoperator3.v3.3.5",
                        "conditions": [
                            {
                                "type": "InstallPlanMissing",
                                "status": "True",
                                "reason": "ReferencedInstallPlanNotFound",
                            }
                        ],
                    },
                }
            ]
        }
        with mock.patch.object(gw_mod, "oc_run") as oc_run:
            oc_run.side_effect = [
                mock.Mock(returncode=0, stdout=json.dumps(sub_json)),
                mock.Mock(returncode=0, stdout=json.dumps({"items": []})),
                mock.Mock(returncode=0, stdout="", stderr=""),
                mock.Mock(returncode=0, stdout="", stderr=""),
            ]
            repaired = gw_mod.repair_servicemesh_subscription_stale_refs("openshift-operators")
        self.assertEqual(repaired, 1)
        delete_call = oc_run.call_args_list[2][0][0]
        apply_payload = oc_run.call_args_list[3][1].get("stdin_text", "")
        self.assertIn("delete", delete_call)
        self.assertIn("subscription", delete_call)
        self.assertIn("servicemeshoperator3", apply_payload)

class RhoaiGatewayPrepExistingTest(unittest.TestCase):
    @mock.patch.dict("os.environ", {"PRODUCT": ""}, clear=False)
    @mock.patch(
        "components.dashboard_cypress.verify_route.dashboard_cypress_accessible_for_smoke",
        return_value=True,
    )
    @mock.patch.object(gw_prep_mod, "gateway_config_ready", return_value=False)
    @mock.patch.object(gw_prep_mod, "ensure_rhoai_gateway_for_install")
    def test_skips_gateway_wait_when_existing_dashboard_ready(
        self, install_mock, _ready_mock, _accessible_mock
    ) -> None:
        gw_prep_mod.ensure_rhoai_gateway_stack_for_components({"dashboard_cypress"})
        install_mock.assert_not_called()

    @mock.patch.dict("os.environ", {"PRODUCT": "rhoai", "CLUSTER_SOURCE": "EPHC"}, clear=False)
    @mock.patch.object(gw_prep_mod, "gateway_oidc_configured", return_value=False)
    @mock.patch.object(gw_prep_mod, "gateway_config_ready", return_value=True)
    @mock.patch.object(gw_prep_mod, "ensure_dashboard_gateway_prereqs")
    @mock.patch.object(gw_prep_mod, "ensure_transitive_olm_deps_for_gateway", return_value=0)
    @mock.patch.object(gw_prep_mod, "ensure_rhoai_gateway_for_install")
    def test_ephc_runs_oidc_patch_when_gateway_ready_without_oidc(
        self,
        install_mock,
        _transitive_mock,
        _prereq_mock,
        _ready_mock,
        _oidc_mock,
    ) -> None:
        gw_prep_mod.ensure_rhoai_gateway_stack_for_components({"dashboard_cypress"})
        install_mock.assert_called_once()

class OpenshiftGatewayIstioTest(unittest.TestCase):
    def test_reconcile_patches_eol_version_from_csv_alm_examples(self) -> None:
        istio_doc = {
            "spec": {"version": "v1.26.2"},
            "status": {
                "state": "ReconcileError",
                "conditions": [
                    {
                        "type": "Reconciled",
                        "status": "False",
                        "message": "version \"v1.26.2\" is end-of-life and cannot be installed",
                    }
                ],
            },
        }
        csv_doc = {
            "items": [
                {
                    "metadata": {
                        "name": "servicemeshoperator3.v3.4.1",
                        "annotations": {
                            "alm-examples": json.dumps(
                                [{"kind": "Istio", "spec": {"version": "v1.30.3"}}]
                            ),
                        },
                    },
                    "status": {"phase": "Succeeded"},
                }
            ]
        }
        with mock.patch.object(gw_mod, "oc_run") as oc_run:
            oc_run.side_effect = [
                mock.Mock(returncode=0, stdout=json.dumps(istio_doc)),
                mock.Mock(returncode=0, stdout=json.dumps(csv_doc)),
                mock.Mock(returncode=0, stdout="", stderr=""),
            ]
            patched = gw_mod.reconcile_openshift_gateway_istio_eol()
        self.assertTrue(patched)
        patch_call = oc_run.call_args_list[2][0][0]
        self.assertIn("patch", patch_call)
        self.assertIn("v1.30.3", patch_call[-1])

    def test_reconcile_skips_when_already_reconciled(self) -> None:
        istio_doc = {
            "spec": {"version": "v1.30.3"},
            "status": {
                "state": "Healthy",
                "conditions": [{"type": "Reconciled", "status": "True"}],
            },
        }
        with mock.patch.object(gw_mod, "oc_run") as oc_run:
            oc_run.return_value = mock.Mock(returncode=0, stdout=json.dumps(istio_doc))
            self.assertFalse(gw_mod.reconcile_openshift_gateway_istio_eol())

    def test_parse_istio_version_from_alm_examples(self) -> None:
        alm = json.dumps([{"kind": "Istio", "spec": {"version": "v1.30.3"}}])
        self.assertEqual(gw_mod._parse_istio_version_from_alm_examples(alm), "v1.30.3")

    def test_istio_reconciled_only_when_reconciled_true_or_healthy(self) -> None:
        reconciling = {"status": {"state": "Reconciling", "conditions": []}}
        self.assertFalse(gw_mod._openshift_gateway_istio_reconciled(reconciling))
        healthy = {"status": {"state": "Healthy", "conditions": []}}
        self.assertTrue(gw_mod._openshift_gateway_istio_reconciled(healthy))
        reconciled = {
            "status": {
                "state": "Reconciling",
                "conditions": [{"type": "Reconciled", "status": "True"}],
            }
        }
        self.assertTrue(gw_mod._openshift_gateway_istio_reconciled(reconciled))

    def test_stack_ready_requires_istio_cr_version_when_target_set(self) -> None:
        istio_doc = {
            "spec": {"version": "v1.26.2"},
            "status": {
                "state": "Healthy",
                "conditions": [{"type": "Reconciled", "status": "True"}],
            },
        }
        with mock.patch.object(gw_mod, "_openshift_gateway_istio_doc", return_value=istio_doc):
            self.assertFalse(
                gw_mod.openshift_gateway_istio_stack_ready(target_version="v1.30.3")
            )
            self.assertTrue(
                gw_mod.openshift_gateway_istio_stack_ready(target_version="v1.26.2")
            )

    def test_openshift_gateway_istio_wait_sec_invalid_env_uses_default(self) -> None:
        with mock.patch.dict("os.environ", {"OPENSHIFT_GATEWAY_ISTIO_WAIT_SEC": "not-a-number"}):
            self.assertEqual(gw_mod._openshift_gateway_istio_wait_sec(), 600)

    def test_ensure_istio_fails_when_istio_probe_errors(self) -> None:
        with mock.patch.object(
            gw_mod,
            "_servicemesh_istio_version_from_csv",
            return_value="v1.30.3",
        ), mock.patch.object(
            gw_mod,
            "_openshift_gateway_istio_stack_ready",
            return_value=False,
        ), mock.patch.object(
            gw_mod,
            "_fetch_openshift_gateway_istio_doc",
            return_value=(None, "error"),
        ):
            self.assertFalse(gw_mod.ensure_openshift_gateway_istio_for_dep_operators())

    def test_ensure_istio_succeeds_on_probe_error_when_revision_stack_ready(self) -> None:
        with mock.patch.object(
            gw_mod,
            "_servicemesh_istio_version_from_csv",
            return_value=None,
        ), mock.patch.object(
            gw_mod,
            "_openshift_gateway_istio_stack_ready",
            side_effect=[False, True],
        ), mock.patch.object(
            gw_mod,
            "_fetch_openshift_gateway_istio_doc",
            return_value=(None, "error"),
        ):
            self.assertTrue(gw_mod.ensure_openshift_gateway_istio_for_dep_operators())

    def test_ensure_istio_for_verify_patches_eol_and_waits_controllers(self) -> None:
        with mock.patch.object(
            gw_mod, "openshift_gateway_istio_stack_ready", side_effect=[False, True]
        ), mock.patch.object(
            gw_mod, "reconcile_openshift_gateway_istio_eol", return_value=True
        ), mock.patch.object(
            gw_mod, "wait_openshift_gateway_istio_ready", return_value=True
        ), mock.patch.object(
            gw_mod, "wait_openshift_gateway_controller_deployments", return_value=True
        ) as wait_dep:
            self.assertTrue(gw_mod.ensure_openshift_gateway_istio_for_verify())
        wait_dep.assert_called_once()

    def test_ensure_istio_for_verify_skips_eol_when_stack_ready(self) -> None:
        with mock.patch.object(
            gw_mod, "openshift_gateway_istio_stack_ready", return_value=True
        ), mock.patch.object(
            gw_mod, "reconcile_openshift_gateway_istio_eol"
        ) as eol, mock.patch.object(
            gw_mod, "wait_openshift_gateway_controller_deployments", return_value=True
        ) as wait_dep:
            self.assertTrue(gw_mod.ensure_openshift_gateway_istio_for_verify())
        eol.assert_not_called()
        wait_dep.assert_called_once()

    def test_fetch_istio_doc_uses_sailoperator_gvr_when_istio_kind_missing(self) -> None:
        istio_doc = {"metadata": {"name": "openshift-gateway"}, "status": {"state": "Healthy"}}
        with mock.patch.object(gw_mod, "oc_run") as oc_run:
            oc_run.side_effect = [
                mock.Mock(
                    returncode=1,
                    stdout="",
                    stderr='error: the server doesn\'t have a resource type "istio"',
                ),
                mock.Mock(returncode=0, stdout=json.dumps(istio_doc)),
            ]
            doc, status = gw_mod._fetch_openshift_gateway_istio_doc()
        self.assertEqual(status, "ok")
        self.assertEqual(doc, istio_doc)
        self.assertIn("istios.sailoperator.io", oc_run.call_args_list[1][0][0])

    def test_ensure_istio_does_not_short_circuit_on_stale_reconciled_version(self) -> None:
        istio_doc = {
            "spec": {"version": "v1.26.2"},
            "status": {
                "state": "Healthy",
                "conditions": [{"type": "Reconciled", "status": "True"}],
            },
        }
        with mock.patch.object(
            gw_mod,
            "_servicemesh_istio_version_from_csv",
            return_value="v1.30.3",
        ), mock.patch.object(
            gw_mod,
            "_openshift_gateway_istio_stack_ready",
            return_value=False,
        ), mock.patch.object(
            gw_mod,
            "_openshift_gateway_istio_doc",
            return_value=istio_doc,
        ), mock.patch.object(
            gw_mod,
            "_is_istio_eol_reconcile_error",
            return_value=False,
        ):
            self.assertFalse(gw_mod.ensure_openshift_gateway_istio_for_dep_operators())

    def test_stack_ready_when_revision_and_istiod_up_despite_istio_cr_error(self) -> None:
        istio_doc = {
            "spec": {"version": "v1.26.2"},
            "status": {
                "state": "ReconcileError",
                "conditions": [
                    {
                        "type": "Reconciled",
                        "status": "False",
                        "message": "version v1.26.2 is end-of-life",
                    }
                ],
            },
        }
        revision_doc = {
            "spec": {"version": "v1.30.3"},
            "status": {"conditions": [{"type": "Ready", "status": "True"}]},
        }
        with mock.patch.object(gw_mod, "_openshift_gateway_istio_doc", return_value=istio_doc):
            with mock.patch.object(
                gw_mod, "_openshift_gateway_istio_revision_doc", return_value=revision_doc
            ):
                with mock.patch.object(gw_mod, "_openshift_gateway_controller_ready", return_value=True):
                    self.assertTrue(gw_mod.openshift_gateway_istio_stack_ready(target_version="v1.30.3"))

    def test_wait_succeeds_on_revision_ready_after_eol_patch(self) -> None:
        istio_doc = {
            "spec": {"version": "v1.26.2"},
            "status": {
                "state": "ReconcileError",
                "conditions": [{"type": "Reconciled", "status": "False"}],
            },
        }
        revision_doc = {
            "spec": {"version": "v1.30.3"},
            "status": {"conditions": [{"type": "Ready", "status": "True"}]},
        }
        with mock.patch.object(gw_mod, "time") as time_mod:
            time_mod.monotonic.side_effect = [0.0, 1.0]
            time_mod.sleep = mock.Mock()
            with mock.patch.object(gw_mod, "_openshift_gateway_istio_doc", return_value=istio_doc):
                with mock.patch.object(
                    gw_mod, "_openshift_gateway_istio_revision_doc", return_value=revision_doc
                ):
                    with mock.patch.object(
                        gw_mod, "_openshift_gateway_controller_ready", return_value=True
                    ):
                        self.assertTrue(
                            gw_mod.wait_openshift_gateway_istio_ready(
                                timeout_sec=30, target_version="v1.30.3"
                            )
                        )


class DsciServiceMeshTest(unittest.TestCase):
    def test_dashboard_cypress_needs_servicemesh(self) -> None:
        self.assertTrue(_smoke_components_need_servicemesh("dashboard_cypress"))

    def test_dashboard_dsci_servicemesh_managed(self) -> None:
        self.assertEqual(_initial_dsci_servicemesh_state("dashboard_cypress"), "Managed")
        self.assertIn("managementState: Managed", _dsci_yaml("dashboard_cypress"))

    def test_workbenches_dsci_servicemesh_removed_without_product_install(self) -> None:
        with mock.patch.dict("os.environ", {"PRODUCT": ""}, clear=False):
            self.assertEqual(_initial_dsci_servicemesh_state("workbenches"), "Removed")
            self.assertIn("managementState: Removed", _dsci_yaml("workbenches"))

    @mock.patch.dict("os.environ", {"PRODUCT": "rhoai"}, clear=False)
    def test_model_registry_rhoai_install_servicemesh_managed(self) -> None:
        self.assertEqual(_initial_dsci_servicemesh_state("model_registry"), "Managed")
        self.assertIn("managementState: Managed", _dsci_yaml("model_registry"))

if __name__ == "__main__":
    raise SystemExit(unittest.main())
