"""Tests for DSC baseline snapshot and drift detection."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from suite.dsc_baseline import (
    _extract_management_states,
    check_dsc_drift,
    filter_drifts_for_component,
    finalize_component_dsc_hygiene,
    load_dsc_baseline,
    read_dsc_drift_marker,
    reconcile_baseline_dsc_before_component,
    restore_dsc_from_baseline,
    wait_for_baseline_spec,
    wait_for_ready_reconcile,
    write_dsc_drift_marker,
    check_baseline_managed_ready_stale,
    _dsc_reconcile_wait_timeout_sec,
)

class ExtractManagementStatesTest(unittest.TestCase):
    def test_extracts_states(self) -> None:
        spec = {
            "dashboard": {"managementState": "Managed"},
            "workbenches": {"managementState": "Removed"},
            "kserve": {"managementState": "Managed", "modelsAsService": {"managementState": "Managed"}},
        }
        states = _extract_management_states(spec)
        self.assertEqual(states["dashboard"], "Managed")
        self.assertEqual(states["workbenches"], "Removed")
        self.assertEqual(states["kserve"], "Managed")

    def test_empty_spec(self) -> None:
        self.assertEqual(_extract_management_states({}), {})

class LoadBaselineTest(unittest.TestCase):
    def test_no_baseline_returns_none(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            self.assertIsNone(load_dsc_baseline(Path(tmp)))

    def test_loads_saved_baseline(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            spec = {"dashboard": {"managementState": "Managed"}}
            (root / ".dsc-baseline.json").write_text(
                json.dumps(spec), encoding="utf-8"
            )
            loaded = load_dsc_baseline(root)
            self.assertEqual(loaded, spec)

class CheckDscDriftTest(unittest.TestCase):
    """Offline drift detection against a saved baseline (no cluster)."""

    def test_no_baseline_returns_empty(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual(check_dsc_drift(Path(tmp)), [])

    def test_detects_drift(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            baseline = {
                "dashboard": {"managementState": "Managed"},
                "workbenches": {"managementState": "Managed"},
            }
            (root / ".dsc-baseline.json").write_text(
                json.dumps(baseline), encoding="utf-8"
            )
            current = {
                "dashboard": {"managementState": "Removed"},
                "workbenches": {"managementState": "Managed"},
            }
            import unittest.mock as mock

            with mock.patch(
                "suite.dsc_baseline._oc_get_dsc_components_spec", return_value=current
            ):
                drifts = check_dsc_drift(root)
            self.assertEqual(len(drifts), 1)
            self.assertIn("dashboard", drifts[0])
            self.assertIn("Managed", drifts[0])
            self.assertIn("Removed", drifts[0])

    def test_no_drift_when_states_match(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            spec = {
                "dashboard": {"managementState": "Managed"},
                "workbenches": {"managementState": "Removed"},
            }
            (root / ".dsc-baseline.json").write_text(
                json.dumps(spec), encoding="utf-8"
            )
            import unittest.mock as mock

            with mock.patch(
                "suite.dsc_baseline._oc_get_dsc_components_spec", return_value=spec
            ):
                self.assertEqual(check_dsc_drift(root), [])

class DriftMarkerTest(unittest.TestCase):
    def test_roundtrip(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            write_dsc_drift_marker(root, "workbenches", ["dashboard: Managed\u2192Removed"])
            drifts = read_dsc_drift_marker(root, "workbenches")
            self.assertEqual(drifts, ["dashboard: Managed\u2192Removed"])

    def test_no_marker_returns_empty(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual(read_dsc_drift_marker(Path(tmp), "workbenches"), [])

class RestoreFromBaselineTest(unittest.TestCase):
    def test_no_baseline_returns_false(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            self.assertFalse(restore_dsc_from_baseline(Path(tmp)))

    def test_restore_calls_oc_patch(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            baseline = {"dashboard": {"managementState": "Managed"}}
            (root / ".dsc-baseline.json").write_text(
                json.dumps(baseline), encoding="utf-8"
            )
            import subprocess
            import unittest.mock as mock

            fake_result = subprocess.CompletedProcess(args=[], returncode=0, stdout="", stderr="")
            with (
                mock.patch(
                    "install.dsc_install.dsc_resource_kind",
                    return_value="datascienceclusters",
                ),
                mock.patch("install.dsc_install.run_oc", return_value=fake_result) as mock_oc,
            ):
                result = restore_dsc_from_baseline(root)
            self.assertTrue(result)
            call_args = mock_oc.call_args
            args_list = call_args[0][0] if call_args[0] else call_args[1].get("args", [])
            self.assertIn("patch", args_list)
            self.assertIn("datascienceclusters", args_list)

    def test_restore_retries_after_kind_cache_refresh(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            baseline = {"dashboard": {"managementState": "Managed"}}
            (root / ".dsc-baseline.json").write_text(
                json.dumps(baseline), encoding="utf-8"
            )
            import subprocess
            import unittest.mock as mock

            fail = subprocess.CompletedProcess(
                args=[],
                returncode=1,
                stdout="",
                stderr='error: the server doesn\'t have a resource type "datasciencecluster"',
            )
            ok = subprocess.CompletedProcess(args=[], returncode=0, stdout="", stderr="")
            kind_calls = iter(["datasciencecluster", "datascienceclusters"])

            with (
                mock.patch(
                    "install.dsc_install.dsc_resource_kind",
                    side_effect=lambda **kwargs: next(kind_calls),
                ),
                mock.patch("install.dsc_install.reset_dsc_resource_kind_cache"),
                mock.patch("install.dsc_install.run_oc", side_effect=[fail, ok]) as mock_oc,
            ):
                result = restore_dsc_from_baseline(root)
            self.assertTrue(result)
            self.assertEqual(mock_oc.call_count, 2)
            retry_args = mock_oc.call_args_list[1][0][0]
            self.assertIn("datascienceclusters", retry_args)

class DscResourceKindCacheTest(unittest.TestCase):
    def test_does_not_cache_kind_on_failed_api_resources_probe(self) -> None:
        import subprocess
        import unittest.mock as mock

        import install.dsc_install as dsc_install

        dsc_install.reset_dsc_resource_kind_cache()
        fail = subprocess.CompletedProcess(args=[], returncode=1, stdout="", stderr="timeout")
        with mock.patch("install.dsc_install.oc_run", return_value=fail) as mock_oc:
            self.assertEqual(dsc_install.dsc_resource_kind(), "datascienceclusters")
            self.assertEqual(dsc_install.dsc_resource_kind(), "datascienceclusters")
            self.assertEqual(mock_oc.call_count, 2)

    def test_caches_kind_after_successful_probe(self) -> None:
        import subprocess
        import unittest.mock as mock

        import install.dsc_install as dsc_install

        dsc_install.reset_dsc_resource_kind_cache()
        ok = subprocess.CompletedProcess(
            args=[], returncode=0, stdout="datascienceclusters\n", stderr=""
        )
        with mock.patch("install.dsc_install.oc_run", return_value=ok) as mock_oc:
            self.assertEqual(dsc_install.dsc_resource_kind(), "datascienceclusters")
            self.assertEqual(dsc_install.dsc_resource_kind(), "datascienceclusters")
            self.assertEqual(mock_oc.call_count, 1)

class FilterDriftsForComponentTest(unittest.TestCase):
    def test_filters_to_managed_keys(self) -> None:
        import unittest.mock as mock

        drifts = [
            "dashboard: Managed\u2192Removed",
            "workbenches: Managed\u2192Removed",
        ]
        with mock.patch(
            "suite.dsc_baseline._managed_dsc_keys_for_component",
            return_value={"dashboard"},
        ):
            out = filter_drifts_for_component("dashboard_cypress", drifts)
        self.assertEqual(out, ["dashboard: Managed\u2192Removed"])

    def test_empty_when_no_managed_overlap(self) -> None:
        import unittest.mock as mock

        with mock.patch(
            "suite.dsc_baseline._managed_dsc_keys_for_component",
            return_value={"ogx"},
        ):
            out = filter_drifts_for_component(
                "ai_safety_evalhub",
                ["dashboard: Managed\u2192Removed"],
            )
        self.assertEqual(out, [])

class CheckBaselineManagedReadyStaleTest(unittest.TestCase):
    def test_no_baseline_returns_empty(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual(check_baseline_managed_ready_stale(Path(tmp)), set())

    def test_deployments_not_ready_is_stale(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / ".dsc-baseline.json").write_text(
                json.dumps(
                    {
                        "dashboard": {"managementState": "Managed"},
                        "workbenches": {"managementState": "Removed"},
                    }
                ),
                encoding="utf-8",
            )
            import unittest.mock as mock

            def _cond(ctype: str) -> tuple[str, str, str]:
                if ctype == "DashboardReady":
                    return ("False", "DeploymentsNotReady", "0/1 deployments ready")
                return ("True", "", "")

            with mock.patch("suite.component_dsc_gate._dsc_condition", side_effect=_cond):
                stale = check_baseline_managed_ready_stale(root)
            self.assertEqual(stale, {"dashboard"})

    def test_ready_true_not_stale(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / ".dsc-baseline.json").write_text(
                json.dumps({"dashboard": {"managementState": "Managed"}}),
                encoding="utf-8",
            )
            import unittest.mock as mock

            with mock.patch(
                "suite.component_dsc_gate._dsc_condition",
                return_value=("True", "", ""),
            ):
                self.assertEqual(check_baseline_managed_ready_stale(root), set())


class WaitForBaselineSpecTest(unittest.TestCase):
    def test_no_baseline_returns_true(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            self.assertTrue(wait_for_baseline_spec(Path(tmp), timeout_sec=1))

    def test_returns_true_when_spec_matches(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            spec = {"dashboard": {"managementState": "Managed"}}
            (root / ".dsc-baseline.json").write_text(
                json.dumps(spec), encoding="utf-8"
            )
            import unittest.mock as mock

            with mock.patch(
                "suite.dsc_baseline.check_dsc_drift", return_value=[]
            ):
                self.assertTrue(wait_for_baseline_spec(root, timeout_sec=1))

class FinalizeComponentDscHygieneTest(unittest.TestCase):
    def test_no_drift_returns_empty(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            import unittest.mock as mock

            with (
                mock.patch("suite.dsc_baseline.check_dsc_drift", return_value=[]),
                mock.patch(
                    "suite.dsc_baseline.check_baseline_managed_ready_stale",
                    return_value=set(),
                ),
            ):
                out = finalize_component_dsc_hygiene("workbenches", Path(tmp))
            self.assertEqual(out, [])

    def test_unattributed_drift_restores_without_marker(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            import unittest.mock as mock

            drifts = ["dashboard: Managed\u2192Removed"]
            with (
                mock.patch("suite.dsc_baseline.check_dsc_drift", return_value=drifts),
                mock.patch(
                    "suite.dsc_baseline.check_baseline_managed_ready_stale",
                    return_value=set(),
                ),
                mock.patch(
                    "suite.dsc_baseline.filter_drifts_for_component", return_value=[]
                ),
                mock.patch(
                    "suite.dsc_baseline.restore_dsc_from_baseline", return_value=True
                ) as mock_restore,
                mock.patch(
                    "suite.dsc_baseline.wait_for_baseline_spec", return_value=True
                ),
                mock.patch(
                    "suite.dsc_baseline.wait_for_ready_reconcile", return_value=True
                ) as mock_ready,
            ):
                out = finalize_component_dsc_hygiene("ai_safety_evalhub", root)
            self.assertEqual(out, [])
            mock_restore.assert_called_once()
            mock_ready.assert_called_once_with({"dashboard"})
            self.assertEqual(read_dsc_drift_marker(root, "ai_safety_evalhub"), [])

    def test_attributed_drift_writes_marker(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            import unittest.mock as mock

            drifts = ["dashboard: Managed\u2192Removed"]
            with (
                mock.patch("suite.dsc_baseline.check_dsc_drift", return_value=drifts),
                mock.patch(
                    "suite.dsc_baseline.check_baseline_managed_ready_stale",
                    return_value=set(),
                ),
                mock.patch(
                    "suite.dsc_baseline.filter_drifts_for_component", return_value=drifts
                ),
                mock.patch(
                    "suite.dsc_baseline.restore_dsc_from_baseline", return_value=True
                ),
                mock.patch(
                    "suite.dsc_baseline.wait_for_baseline_spec", return_value=True
                ),
                mock.patch(
                    "suite.dsc_baseline.wait_for_ready_reconcile", return_value=True
                ),
            ):
                out = finalize_component_dsc_hygiene("dashboard_cypress", root)
            self.assertEqual(out, drifts)
            self.assertEqual(read_dsc_drift_marker(root, "dashboard_cypress"), drifts)

    def test_stale_ready_reconciles_without_marker(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            import unittest.mock as mock

            with (
                mock.patch("suite.dsc_baseline.check_dsc_drift", return_value=[]),
                mock.patch(
                    "suite.dsc_baseline.check_baseline_managed_ready_stale",
                    return_value={"dashboard", "mlflowoperator"},
                ),
                mock.patch(
                    "suite.dsc_baseline.restore_dsc_from_baseline", return_value=True
                ) as mock_restore,
                mock.patch(
                    "suite.dsc_baseline.wait_for_ready_reconcile", return_value=True
                ) as mock_ready,
            ):
                out = finalize_component_dsc_hygiene("maas_billing", root)
            self.assertEqual(out, [])
            mock_restore.assert_called_once()
            mock_ready.assert_called_once_with({"dashboard", "mlflowoperator"})

    def test_finalize_skips_ready_wait_when_restore_failed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            import unittest.mock as mock

            with (
                mock.patch("suite.dsc_baseline.check_dsc_drift", return_value=[]),
                mock.patch(
                    "suite.dsc_baseline.check_baseline_managed_ready_stale",
                    return_value={"dashboard"},
                ),
                mock.patch(
                    "suite.dsc_baseline.restore_dsc_from_baseline", return_value=False
                ) as mock_restore,
                mock.patch(
                    "suite.dsc_baseline.wait_for_ready_reconcile", return_value=True
                ) as mock_ready,
            ):
                out = finalize_component_dsc_hygiene("model_server", root)
            self.assertEqual(out, [])
            mock_restore.assert_called_once()
            mock_ready.assert_not_called()

class ReconcileBeforeComponentTest(unittest.TestCase):
    def test_skips_when_cluster_api_already_unreachable(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            import unittest.mock as mock

            with (
                mock.patch(
                    "suite.cluster_api_health.cluster_smoke_infra_blocked_reason",
                    return_value="cluster API unreachable: elb dead",
                ),
                mock.patch("suite.dsc_baseline.check_dsc_drift") as mock_drift,
            ):
                reconcile_baseline_dsc_before_component("mlflow", root)
            mock_drift.assert_not_called()

    def test_reconcile_when_stale_ready(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            import unittest.mock as mock

            with (
                mock.patch(
                    "suite.cluster_api_health.cluster_smoke_infra_blocked_reason",
                    return_value="",
                ),
                mock.patch("suite.dsc_baseline.check_dsc_drift", return_value=[]),
                mock.patch(
                    "suite.dsc_baseline.check_baseline_managed_ready_stale",
                    return_value={"mlflowoperator"},
                ),
                mock.patch(
                    "suite.dsc_baseline.restore_dsc_from_baseline", return_value=True
                ) as mock_restore,
                mock.patch(
                    "suite.dsc_baseline.wait_for_ready_reconcile", return_value=True
                ) as mock_ready,
            ):
                reconcile_baseline_dsc_before_component("mlflow", root)
            mock_restore.assert_called_once()
            mock_ready.assert_called_once_with({"mlflowoperator"})


class WaitForReadyReconcileTest(unittest.TestCase):
    def test_aborts_when_cluster_api_unreachable(self) -> None:
        import unittest.mock as mock

        with (
            mock.patch(
                "suite.component_dsc_gate._dsc_condition",
                return_value=("False", "DeploymentsNotReady", ""),
            ),
            mock.patch(
                "suite.cluster_api_health.cluster_api_unreachable_reason",
                return_value="cluster API unreachable: no such host",
            ),
            mock.patch(
                "steps.cluster_prep_state.mark_cluster_api_unreachable",
            ) as mock_mark,
        ):
            ok = wait_for_ready_reconcile({"dashboard"}, timeout_sec=60)
        self.assertFalse(ok)
        mock_mark.assert_called_once_with("cluster API unreachable: no such host")


class DscReconcileWaitTimeoutTest(unittest.TestCase):
    def test_ephc_caps_default_wait(self) -> None:
        import os

        prior_source = os.environ.get("CLUSTER_SOURCE")
        prior_default = os.environ.get("OLMINSTALL_DSC_RECONCILE_WAIT_SEC")
        prior_cap = os.environ.get("OLMINSTALL_EPHC_DSC_RECONCILE_WAIT_SEC")
        try:
            os.environ["CLUSTER_SOURCE"] = "EPHC"
            os.environ["OLMINSTALL_DSC_RECONCILE_WAIT_SEC"] = "600"
            os.environ["OLMINSTALL_EPHC_DSC_RECONCILE_WAIT_SEC"] = "120"
            self.assertEqual(_dsc_reconcile_wait_timeout_sec(), 120)
        finally:
            for key, val in (
                ("CLUSTER_SOURCE", prior_source),
                ("OLMINSTALL_DSC_RECONCILE_WAIT_SEC", prior_default),
                ("OLMINSTALL_EPHC_DSC_RECONCILE_WAIT_SEC", prior_cap),
            ):
                if val is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = val

