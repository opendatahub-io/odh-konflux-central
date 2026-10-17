"""Unit tests for JobSet/LWS operator CR ensure (Jenkins InstallDeps parity)."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from install.dependency_operators import ensure_jobset_and_lws_operator_crs


class EnsureJobsetLwsCrsTest(unittest.TestCase):
    def test_skips_when_crd_missing(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            olm = Path(tmp)
            (olm / "resources").mkdir()
            with patch(
                "install.dependency_operators._cluster_operator_crd_available",
                return_value=False,
            ):
                with patch(
                    "install.dependency_operators._run_operator_install_post_install_script",
                ) as run:
                    ensure_jobset_and_lws_operator_crs(olm_dir=olm)
            run.assert_not_called()

    def test_runs_post_install_when_cr_missing(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            olm = Path(tmp)
            (olm / "resources").mkdir()
            with patch(
                "install.dependency_operators._cluster_operator_crd_available",
                return_value=True,
            ):
                with patch(
                    "install.dependency_operators._cluster_operator_cr_exists",
                    side_effect=[False, True, False, True],
                ):
                    with patch(
                        "install.dependency_operators._run_operator_install_post_install_script",
                        return_value=True,
                    ) as run:
                        ensure_jobset_and_lws_operator_crs(olm_dir=olm)
            self.assertEqual(run.call_count, 2)

    def test_already_present_skips_script(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            olm = Path(tmp)
            with patch(
                "install.dependency_operators._cluster_operator_crd_available",
                return_value=True,
            ):
                with patch(
                    "install.dependency_operators._cluster_operator_cr_exists",
                    return_value=True,
                ):
                    with patch(
                        "install.dependency_operators._run_operator_install_post_install_script",
                    ) as run:
                        ensure_jobset_and_lws_operator_crs(olm_dir=olm)
            run.assert_not_called()


class FinalizeProductHardFailTest(unittest.TestCase):
    def test_product_install_does_not_soft_continue(self) -> None:
        from install.dependency_operators import finalize_dependency_operators_after_setup_script

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
                        return_value=2,
                    ):
                        with patch(
                            "install.dependency_operators._authorino_crd_available",
                            return_value=True,
                        ):
                            with patch(
                                "install.dependency_operators.product_install_path",
                                return_value=True,
                            ):
                                with patch(
                                    "install.dependency_operators._ensure_authorino_operators_after_setup",
                                ) as authorino:
                                    rc = finalize_dependency_operators_after_setup_script(
                                        olm_dir, 2
                                    )
        self.assertEqual(rc, 2)
        authorino.assert_not_called()


if __name__ == "__main__":
    raise SystemExit(unittest.main())
