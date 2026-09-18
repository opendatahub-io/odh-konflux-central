#!/usr/bin/env python3
"""Unit tests for RHCL operator MaaS pin (no cluster)."""

from __future__ import annotations

import os
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from install import rhcl_deps as mod  # noqa: E402

class RhclDepsTest(unittest.TestCase):
    def test_rhcl_starting_csv_prefers_env(self) -> None:
        with patch.dict(os.environ, {"RHCL_OPERATOR_STARTING_CSV": "rhcl-operator.v9.9.9"}, clear=True):
            self.assertEqual(mod.rhcl_starting_csv(), "rhcl-operator.v9.9.9")

    def test_rhcl_starting_csv_reads_manifest(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            olm = Path(tmp)
            resources = olm / "resources"
            resources.mkdir()
            (resources / "install-rhcl-operator.yaml").write_text(
                'startingCSV: "rhcl-operator.v2.0.0"\n',
                encoding="utf-8",
            )
            with patch.dict(os.environ, {}, clear=True):
                self.assertEqual(mod.rhcl_starting_csv(olm_dir=olm), "rhcl-operator.v2.0.0")

    @patch("install.rhcl_deps._subscription_current_csv", return_value=mod._FALLBACK_RHCL_CSV)
    @patch("install.rhcl_deps.rhcl_operators_ready", return_value=True)
    def test_skips_when_already_ready(self, ready, _current) -> None:
        with patch("install.rhcl_deps._ensure_kuadrant_namespace_exists"):
            with patch("install.rhcl_deps.reconcile_kuadrant_operator_groups"):
                with patch("install.rhcl_deps.oc_run") as oc_run:
                    mod.ensure_rhcl_operator_for_maas()
                    oc_run.assert_not_called()
        ready.assert_called_once()

    @patch("install.rhcl_deps._apply_rhcl_manifest")
    @patch("install.rhcl_deps.rhcl_operators_ready", return_value=False)
    def test_applies_manifest_when_subscription_missing(
        self,
        _ready,
        apply_manifest,
    ) -> None:
        missing = MagicMock(returncode=1)
        with patch("install.rhcl_deps._ensure_kuadrant_namespace_exists"):
            with patch("install.rhcl_deps.reconcile_kuadrant_operator_groups"):
                with patch("install.rhcl_deps.approve_pending_installplans", return_value=1):
                    with patch("install.rhcl_deps._wait_rhcl_operators_ready"):
                        with patch("install.rhcl_deps.oc_run", return_value=missing):
                            mod.ensure_rhcl_operator_for_maas()
        apply_manifest.assert_called_once()

    @patch("install.dependency_operators.unblock_terminating_namespace")
    @patch("install.dependency_operators._namespace_phase", side_effect=["Terminating", ""])
    def test_ensure_kuadrant_namespace_ready_unblocks_terminating(
        self,
        _phase,
        unblock,
    ) -> None:
        with patch("install.rhcl_deps.time.sleep"):
            mod._ensure_kuadrant_namespace_ready()
        unblock.assert_called_once_with(mod._RHCL_NS)

    @patch("install.rhcl_deps._wait_rhcl_operators_ready")
    @patch("install.rhcl_deps.approve_pending_installplans", return_value=1)
    @patch("install.rhcl_deps._reconcile_stuck_rhcl_subscription")
    @patch("install.rhcl_deps.rhcl_operators_ready", return_value=False)
    def test_reconciles_when_subscription_stuck(
        self,
        _ready,
        reconcile,
        approve,
        wait_ready,
    ) -> None:
        found = MagicMock(returncode=0)
        with patch("install.rhcl_deps._ensure_kuadrant_namespace_exists"):
            with patch("install.rhcl_deps.reconcile_kuadrant_operator_groups"):
                with patch("install.rhcl_deps.oc_run", return_value=found):
                    mod.ensure_rhcl_operator_for_maas()
        reconcile.assert_called_once()
        approve.assert_called_once_with(mod._RHCL_NS)
        wait_ready.assert_called_once()

    @patch("install.rhcl_deps._apply_rhcl_manifest")
    @patch("install.rhcl_deps._delete_rhcl_subscription")
    @patch("install.rhcl_deps._purge_blocking_rhcl_installplans")
    @patch("install.rhcl_deps._delete_stuck_kuadrant_installplans")
    @patch("install.rhcl_deps._delete_stuck_kuadrant_stack_csvs")
    @patch("install.rhcl_deps._delete_stuck_rhcl_csv")
    @patch("install.rhcl_deps._csv_exists", side_effect=lambda name: name != "rhcl-operator.v1.4.0")
    @patch("install.rhcl_deps._subscription_current_csv", return_value="rhcl-operator.v1.4.0")
    def test_reconcile_phantom_current_csv_resets_subscription(
        self,
        _current,
        _exists,
        delete_csv,
        delete_stack,
        delete_ips,
        purge_ips,
        delete_sub,
        apply_manifest,
    ) -> None:
        mod._reconcile_stuck_rhcl_subscription(mod._FALLBACK_RHCL_CSV)
        delete_stack.assert_called_once()
        delete_csv.assert_called()
        purge_ips.assert_called_once_with(mod._FALLBACK_RHCL_CSV)
        delete_ips.assert_called_once()
        delete_sub.assert_called_once()
        apply_manifest.assert_called_once()

    def test_installplan_blocks_rhcl_pin(self) -> None:
        target = mod._FALLBACK_RHCL_CSV
        self.assertTrue(
            mod._installplan_blocks_rhcl_pin(
                ["rhcl-operator.v1.4.0", "authorino-operator.v0.18.0"],
                target,
            )
        )
        self.assertFalse(
            mod._installplan_blocks_rhcl_pin(
                ["rhcl-operator.v1.3.4", "authorino-operator.v0.17.0"],
                target,
            )
        )

    @patch("install.rhcl_deps.oc_run")
    def test_purge_blocking_rhcl_installplans_removes_upgrade_plan(self, oc_run) -> None:
        target = mod._FALLBACK_RHCL_CSV
        ip_doc = {
            "items": [
                {
                    "metadata": {"name": "install-5dh6n"},
                    "spec": {
                        "approved": True,
                        "clusterServiceVersionNames": [
                            "rhcl-operator.v1.4.0",
                            "authorino-operator.v0.18.0",
                        ],
                    },
                    "status": {"phase": "RequiresApproval"},
                }
            ]
        }
        oc_run.side_effect = [
            MagicMock(returncode=0, stdout=json.dumps(ip_doc)),
            MagicMock(returncode=0),
        ]
        mod._purge_blocking_rhcl_installplans(target)
        delete_calls = [c for c in oc_run.call_args_list if c.args and c.args[0][0] == "delete"]
        self.assertEqual(len(delete_calls), 1)
        self.assertEqual(delete_calls[0].args[0][1], "installplan")

    @patch("install.rhcl_deps.oc_run")
    def test_delete_stuck_kuadrant_installplans_removes_upgrade_plan(self, oc_run) -> None:
        target = mod._FALLBACK_RHCL_CSV
        ip_doc = {
            "items": [
                {
                    "metadata": {"name": "install-5dh6n"},
                    "spec": {
                        "approved": False,
                        "clusterServiceVersionNames": [
                            "rhcl-operator.v1.4.0",
                            "authorino-operator.v0.18.0",
                        ],
                    },
                    "status": {"phase": "RequiresApproval"},
                }
            ]
        }
        oc_run.side_effect = [
            MagicMock(returncode=0, stdout=json.dumps(ip_doc)),
            MagicMock(returncode=0),
        ]
        mod._delete_stuck_kuadrant_installplans(target)
        delete_calls = [c for c in oc_run.call_args_list if c.args and c.args[0][0] == "delete"]
        self.assertEqual(len(delete_calls), 1)
        self.assertEqual(delete_calls[0].args[0][1], "installplan")

    @patch("install.rhcl_deps.ensure_rhcl_operator_for_maas")
    def test_reconcile_after_gitops_apply_delegates_to_ensure(self, ensure_rhcl) -> None:
        mod.reconcile_rhcl_after_gitops_apply()
        ensure_rhcl.assert_called_once()

    def test_rhcl_manifest_apply_text_skips_operatorgroup(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            manifest = Path(tmp) / "install-rhcl-operator.yaml"
            manifest.write_text(
                "kind: Namespace\nmetadata:\n  name: kuadrant-system\n---\n"
                "kind: OperatorGroup\nmetadata:\n  name: kuadrant-system\n"
                "  namespace: kuadrant-system\n---\n"
                'kind: Subscription\nmetadata:\n  name: rhcl-operator\n'
                "  namespace: kuadrant-system\nspec:\n  startingCSV: \"\"\n",
                encoding="utf-8",
            )
            text = mod._rhcl_manifest_apply_text(manifest, mod._FALLBACK_RHCL_CSV)
            self.assertIn("kind: Subscription", text)
            self.assertNotIn("kind: OperatorGroup", text)

    @patch("install.rhcl_deps._apply_gitops_operatorgroup")
    @patch("install.rhcl_deps._operatorgroup_multiple_flag", return_value=True)
    @patch("install.rhcl_deps._operatorgroup_names", return_value=["kuadrant"])
    @patch("install.rhcl_deps.oc_run")
    def test_reconcile_operator_groups_recreates_stale_multiple_flag(
        self,
        oc_run,
        _names,
        _flag,
        apply_og,
    ) -> None:
        mod.reconcile_kuadrant_operator_groups()
        delete_calls = [c for c in oc_run.call_args_list if c.args and c.args[0][0] == "delete"]
        self.assertEqual(len(delete_calls), 1)
        apply_og.assert_called_once()

    @patch("install.rhcl_deps._apply_gitops_operatorgroup")
    @patch("install.rhcl_deps._ensure_kuadrant_namespace_exists")
    @patch("install.rhcl_deps._operatorgroup_names", return_value=[])
    def test_reconcile_operator_groups_creates_when_none(
        self,
        _names,
        ensure_ns,
        apply_og,
    ) -> None:
        mod.reconcile_kuadrant_operator_groups()
        ensure_ns.assert_called_once()
        apply_og.assert_called_once()

    @patch("install.dependency_operators._namespace_phase", return_value="")
    @patch("install.rhcl_deps.oc_run")
    def test_ensure_kuadrant_namespace_exists_creates_when_missing(
        self,
        oc_run,
        _phase,
    ) -> None:
        oc_run.side_effect = [
            MagicMock(returncode=0, stdout="apiVersion: v1\nkind: Namespace\n"),
            MagicMock(returncode=0),
        ]
        mod._ensure_kuadrant_namespace_exists()
        self.assertEqual(oc_run.call_count, 2)
        self.assertEqual(oc_run.call_args_list[1].args[0][0], "apply")

    @patch("install.rhcl_deps.oc_run")
    def test_reconcile_operator_groups_deletes_olminstall_duplicate(self, oc_run) -> None:
        oc_run.side_effect = [
            MagicMock(returncode=0, stdout="kuadrant\nkuadrant-system\n"),
            MagicMock(returncode=0),
            MagicMock(returncode=0, stdout='{"status":{"conditions":[]}}'),
        ]
        mod.reconcile_kuadrant_operator_groups()
        delete_calls = [c for c in oc_run.call_args_list if c.args and c.args[0][0] == "delete"]
        self.assertEqual(len(delete_calls), 1)
        self.assertEqual(delete_calls[0].args[0][2], mod._OLMINSTALL_OPERATORGROUP)

    @patch(
        "components.maas_billing.gateway.wait_openshift_default_gateway_class_accepted",
        return_value=True,
    )
    @patch("install.rhcl_deps.run_post_install_rhcl_operator")
    @patch("install.rhcl_deps.ensure_rhcl_operator_for_maas")
    def test_dependency_stack_runs_post_install(self, ensure_rhcl, post_install, _gateway) -> None:
        mod.ensure_maas_rhcl_dependency_stack()
        ensure_rhcl.assert_called_once()
        post_install.assert_called_once_with(fatal=True)

    @patch(
        "components.maas_billing.gateway.wait_openshift_default_gateway_class_accepted",
        return_value=True,
    )
    @patch("install.dependency_operators.product_install_path", return_value=True)
    @patch(
        "components.maas_billing.auth.recover_kuadrant_after_gateway_api_provider",
        return_value=False,
    )
    @patch("helpers.gateway_stack_marker.write_gateway_stack_incomplete_marker")
    @patch("install.rhcl_deps.run_post_install_rhcl_operator", return_value=False)
    @patch("install.rhcl_deps.ensure_rhcl_operator_for_maas")
    def test_dependency_stack_warns_when_post_install_incomplete_on_product_install(
        self,
        ensure_rhcl,
        post_install,
        write_marker,
        _recover,
        _product_install,
        _gateway,
    ) -> None:
        mod.ensure_maas_rhcl_dependency_stack()
        ensure_rhcl.assert_called_once()
        self.assertEqual(post_install.call_count, 3)
        post_install.assert_any_call(fatal=False)
        post_install.assert_any_call(fatal=False, timeout_sec=900)
        post_install.assert_any_call(fatal=False, timeout_sec=600)
        write_marker.assert_called_once()

    @patch.dict(os.environ, {"PRODUCT": "", "INSTALL_DEPENDENCIES": "true"}, clear=False)
    @patch(
        "components.maas_billing.gateway.wait_openshift_default_gateway_class_accepted",
        return_value=True,
    )
    @patch(
        "components.maas_billing.auth.recover_kuadrant_after_gateway_api_provider",
        return_value=False,
    )
    @patch("helpers.gateway_stack_marker.write_gateway_stack_incomplete_marker")
    @patch("install.rhcl_deps.run_post_install_rhcl_operator", return_value=False)
    @patch("install.rhcl_deps.ensure_rhcl_operator_for_maas")
    def test_dependency_stack_warns_when_post_install_incomplete_on_install_dependencies(
        self,
        ensure_rhcl,
        post_install,
        write_marker,
        _recover,
        _gateway,
    ) -> None:
        mod.ensure_maas_rhcl_dependency_stack()
        ensure_rhcl.assert_called_once()
        self.assertEqual(post_install.call_count, 3)
        write_marker.assert_called_once()

    @patch.dict(
        os.environ,
        {"PRODUCT": "", "RUN_COMPONENT_CLUSTER_PREP_IN_DEP_OPERATORS": "true"},
        clear=False,
    )
    @patch(
        "components.maas_billing.gateway.wait_openshift_default_gateway_class_accepted",
        return_value=True,
    )
    @patch(
        "components.maas_billing.auth.recover_kuadrant_after_gateway_api_provider",
        return_value=False,
    )
    @patch("helpers.gateway_stack_marker.write_gateway_stack_incomplete_marker")
    @patch("install.rhcl_deps.run_post_install_rhcl_operator", return_value=False)
    @patch("install.rhcl_deps.ensure_rhcl_operator_for_maas")
    def test_dependency_stack_defers_post_install_when_component_prep_in_dep_operators(
        self,
        ensure_rhcl,
        post_install,
        write_marker,
        _recover,
        _gateway,
    ) -> None:
        mod.ensure_maas_rhcl_dependency_stack()
        ensure_rhcl.assert_called_once()
        self.assertEqual(post_install.call_count, 3)
        write_marker.assert_called_once()

    @patch(
        "components.maas_billing.gateway.wait_openshift_default_gateway_class_accepted",
        return_value=True,
    )
    @patch("install.dependency_operators.product_install_path", return_value=True)
    @patch(
        "components.maas_billing.auth.recover_kuadrant_after_gateway_api_provider",
        return_value=True,
    )
    @patch("helpers.gateway_stack_marker.write_gateway_stack_incomplete_marker")
    @patch("install.rhcl_deps.run_post_install_rhcl_operator", return_value=False)
    @patch("install.rhcl_deps.ensure_rhcl_operator_for_maas")
    def test_dependency_stack_recovers_kuadrant_before_incomplete_marker(
        self,
        ensure_rhcl,
        post_install,
        write_marker,
        recover,
        _product_install,
        _gateway,
    ) -> None:
        mod.ensure_maas_rhcl_dependency_stack()
        ensure_rhcl.assert_called_once()
        self.assertEqual(post_install.call_count, 3)
        recover.assert_called_once()
        write_marker.assert_not_called()

    @patch.dict(os.environ, {"RHCL_POST_INSTALL_RETRY_TIMEOUT_SEC": "120"}, clear=False)
    @patch(
        "components.maas_billing.gateway.wait_openshift_default_gateway_class_accepted",
        return_value=True,
    )
    @patch("install.dependency_operators.product_install_path", return_value=True)
    @patch("install.rhcl_deps.run_post_install_rhcl_operator", side_effect=[False, True])
    @patch("install.rhcl_deps.ensure_rhcl_operator_for_maas")
    def test_dependency_stack_succeeds_when_post_install_retry_passes(
        self,
        ensure_rhcl,
        post_install,
        _product_install,
        _gateway,
    ) -> None:
        mod.ensure_maas_rhcl_dependency_stack()
        ensure_rhcl.assert_called_once()
        self.assertEqual(post_install.call_count, 2)
        post_install.assert_any_call(fatal=False, timeout_sec=120)

    @patch(
        "install.rhcl_deps.pick_succeeded_csv_version",
        return_value="1.3.4",
    )
    def test_rhcl_operators_ready_requires_target_csv_and_authorino(self, authorino) -> None:
        target = mod._FALLBACK_RHCL_CSV
        with patch("install.rhcl_deps._subscription_current_csv", return_value=target):
            with patch("install.rhcl_deps._csv_phase", return_value="Succeeded"):
                self.assertTrue(mod.rhcl_operators_ready(target))
        authorino.assert_called_once()

    @patch(
        "install.rhcl_deps.pick_succeeded_csv_version",
        return_value="1.4.0",
    )
    def test_rhcl_operators_ready_accepts_gitops_csv_when_functional(self, authorino) -> None:
        gitops_csv = "rhcl-operator.v1.4.0"
        target = mod._FALLBACK_RHCL_CSV
        with patch("install.rhcl_deps._subscription_current_csv", return_value=gitops_csv):
            with patch("install.rhcl_deps._csv_phase", return_value="Succeeded"):
                self.assertTrue(mod.rhcl_operators_ready(target))
        authorino.assert_called_once()

    @patch(
        "install.rhcl_deps.pick_succeeded_csv_version",
        side_effect=lambda _ns, op, **_: "1.4.1" if op.endswith("operator") else None,
    )
    def test_rhcl_operators_ready_accepts_installed_csv_when_current_unset(self, _pick) -> None:
        target = mod._FALLBACK_RHCL_CSV
        with patch("install.rhcl_deps._subscription_current_csv", return_value=""):
            with patch("install.rhcl_deps._csv_phase", return_value="Succeeded"):
                self.assertTrue(mod.rhcl_operators_ready(target))


if __name__ == "__main__":
    raise SystemExit(unittest.main())
