"""Unit tests for setup-dependencies finalize recovery (no cluster)."""

from __future__ import annotations

import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch, call

from install.dependency_operators import (  # noqa: E402
    ensure_setup_dependency_namespaces_ready,
    finalize_dependency_operators_after_setup_script,
    unblock_terminating_namespace,
)

class FinalizeDependencyOperatorsTest(unittest.TestCase):
    def test_complete_failure_when_authorino_crd_missing(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            olm_dir = Path(tmp) / "rhoai-e2e"
            olm_dir.mkdir()
            (olm_dir / "odh-gitops").mkdir()

            with patch(
                "install.dependency_operators.ensure_setup_dependency_namespaces_ready",
            ):
                with patch(
                    "install.dependency_operators._reconcile_rhcl_after_gitops_with_warning",
                    return_value=False,
                ):
                    with patch(
                        "install.dependency_operators._run_odh_gitops_make",
                        return_value=1,
                    ) as make_run:
                        with patch(
                            "install.dependency_operators._authorino_crd_available",
                            return_value=False,
                        ):
                            with patch(
                                "install.dependency_operators._ensure_authorino_operators_after_setup",
                            ) as authorino:
                                rc = finalize_dependency_operators_after_setup_script(olm_dir, 2)
            self.assertEqual(rc, 1)
            make_run.assert_called_once()
            authorino.assert_not_called()

    def test_partial_failure_runs_authorino_recovery(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            olm_dir = Path(tmp) / "rhoai-e2e"
            olm_dir.mkdir()
            (olm_dir / "odh-gitops").mkdir()

            with patch(
                "install.dependency_operators.ensure_setup_dependency_namespaces_ready",
            ):
                with patch(
                    "install.dependency_operators._reconcile_rhcl_after_gitops_with_warning",
                    return_value=False,
                ) as reconcile:
                    with patch(
                        "install.dependency_operators._run_odh_gitops_make",
                        return_value=1,
                    ):
                        with patch(
                            "install.dependency_operators._authorino_crd_available",
                            return_value=True,
                        ):
                            with patch(
                                "install.dependency_operators._ensure_authorino_operators_after_setup",
                            ) as authorino:
                                with patch(
                                    "install.dependency_operators.maas_dependency_operators_ready",
                                    return_value=True,
                                ):
                                    with patch(
                                        "install.dependency_operators.ensure_jobset_and_lws_operator_crs",
                                    ):
                                        rc = finalize_dependency_operators_after_setup_script(olm_dir, 2)
            self.assertEqual(rc, 2)
            self.assertEqual(reconcile.call_count, 2)
            authorino.assert_called_once()

    def test_product_install_hard_fail_still_ensures_jobset_crs(self) -> None:
        """After CLEANUP, setup-dependencies often exits ≠0; JobSet CR must still be ensured."""
        with tempfile.TemporaryDirectory() as tmp:
            olm_dir = Path(tmp) / "rhoai-e2e"
            olm_dir.mkdir()
            (olm_dir / "odh-gitops").mkdir()
            with (
                patch("install.dependency_operators.ensure_setup_dependency_namespaces_ready"),
                patch(
                    "install.dependency_operators._reconcile_rhcl_after_gitops_with_warning",
                    return_value=False,
                ),
                patch(
                    "install.dependency_operators._run_odh_gitops_make",
                    return_value=2,
                ),
                patch(
                    "install.dependency_operators._authorino_crd_available",
                    return_value=True,
                ),
                patch(
                    "install.dependency_operators.product_install_path",
                    return_value=True,
                ),
                patch(
                    "install.dependency_operators.ensure_jobset_and_lws_operator_crs",
                ) as ensure_js,
            ):
                rc = finalize_dependency_operators_after_setup_script(olm_dir, 2)
            self.assertEqual(rc, 2)
            ensure_js.assert_called_once_with(olm_dir=olm_dir)

    def test_partial_failure_reconciles_rhcl_before_gitops_retry(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            olm_dir = Path(tmp) / "rhoai-e2e"
            olm_dir.mkdir()
            (olm_dir / "odh-gitops").mkdir()
            call_order: list[str] = []

            def _reconcile(_olm: Path) -> bool:
                call_order.append("reconcile")
                return False

            def _make(_olm: Path, *_args: str) -> int:
                call_order.append("make")
                return 1

            with patch(
                "install.dependency_operators.ensure_setup_dependency_namespaces_ready",
            ):
                with patch(
                    "install.dependency_operators._reconcile_rhcl_after_gitops_with_warning",
                    side_effect=_reconcile,
                ):
                    with patch(
                        "install.dependency_operators._run_odh_gitops_make",
                        side_effect=_make,
                    ):
                        with patch(
                            "install.dependency_operators._authorino_crd_available",
                            return_value=False,
                        ):
                            finalize_dependency_operators_after_setup_script(olm_dir, 2)
            self.assertEqual(call_order, ["reconcile", "make", "reconcile"])

class EnsureSetupDependencyNamespacesTest(unittest.TestCase):
    @patch("install.dependency_operators.time.sleep")
    @patch("install.dependency_operators.unblock_terminating_namespace")
    @patch(
        "install.dependency_operators._namespace_phase",
        side_effect=["Terminating", "Active", "Active"],
    )
    def test_waits_until_terminating_namespace_cleared(
        self,
        _phase,
        unblock,
        _sleep,
    ) -> None:
        ensure_setup_dependency_namespaces_ready(
            ("openshift-keda",),
            timeout_sec=30,
        )
        unblock.assert_called_once_with("openshift-keda")

    @patch("install.dependency_operators.time.sleep")
    @patch("install.dependency_operators.unblock_terminating_namespace")
    @patch("install.dependency_operators._namespace_phase", return_value="Terminating")
    @patch("install.dependency_operators.time.time", side_effect=[100.0, 100.0, 131.0])
    def test_raises_when_namespace_stays_terminating(
        self,
        _time,
        _phase,
        unblock,
        _sleep,
    ) -> None:
        with self.assertRaisesRegex(RuntimeError, "openshift-kueue-operator"):
            ensure_setup_dependency_namespaces_ready(
                ("openshift-kueue-operator",),
                timeout_sec=30,
            )
        unblock.assert_called_with("openshift-kueue-operator")


class UnblockTerminatingNamespaceTest(unittest.TestCase):
    @patch("install.dependency_operators.subprocess.run")
    @patch("install.dependency_operators.oc_run")
    @patch(
        "install.dependency_operators._namespace_phase",
        side_effect=["Terminating"] * 8 + [""],
    )
    def test_force_deletes_workloads_before_finalize(
        self, _phase: object, oc_run_mock: object, subprocess_run: object
    ) -> None:
        oc_run_mock.return_value = type(
            "R",
            (),
            {"returncode": 0, "stdout": '{"metadata":{"name":"openshift-kueue-operator"},"spec":{}}', "stderr": ""},
        )()
        subprocess_run.return_value = type(
            "R", (), {"returncode": 0, "stdout": "", "stderr": ""}
        )()
        unblock_terminating_namespace("openshift-kueue-operator")
        delete_calls = [
            call.args[0]
            for call in subprocess_run.call_args_list
            if call.args and "delete" in call.args[0] and "pod" in call.args[0]
        ]
        self.assertTrue(delete_calls)
        finalize_calls = [
            call.args[0]
            for call in oc_run_mock.call_args_list
            if call.args and call.args[0][:2] == ["replace", "--raw"]
        ]
        self.assertTrue(finalize_calls)

    @patch("install.dependency_operators.subprocess.run", side_effect=subprocess.TimeoutExpired(cmd="oc", timeout=45))
    @patch("install.dependency_operators.oc_run")
    @patch(
        "install.dependency_operators._namespace_phase",
        side_effect=["Terminating"] * 6 + [""],
    )
    def test_force_delete_timeout_does_not_abort_unblock(
        self, _phase: object, oc_run_mock: object, _subprocess_run: object
    ) -> None:
        oc_run_mock.return_value = type(
            "R",
            (),
            {"returncode": 0, "stdout": '{"metadata":{"name":"redhat-ods-applications"},"spec":{}}', "stderr": ""},
        )()
        unblock_terminating_namespace("redhat-ods-applications")
        finalize_calls = [
            call.args[0]
            for call in oc_run_mock.call_args_list
            if call.args and call.args[0][:2] == ["replace", "--raw"]
        ]
        self.assertTrue(finalize_calls)

if __name__ == "__main__":
    raise SystemExit(unittest.main())
