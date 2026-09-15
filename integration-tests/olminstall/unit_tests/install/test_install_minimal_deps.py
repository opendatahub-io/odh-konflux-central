"""Unit tests for install_minimal_deps skip/run gating (no cluster)."""

from __future__ import annotations

import os
import subprocess
import tempfile
import unittest
from contextlib import ExitStack
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

from install.install_minimal_deps import main

def _minimal_env(*, olm_dir: Path, kubeconfig: Path, **extra: str) -> dict[str, str]:
    env = {
        "KUBECONFIG": str(kubeconfig),
        "OLMINSTALL_DIR": str(olm_dir),
    }
    env.update(extra)
    return env

_MAAS_COMMON_PATCHES = (
    "install.install_minimal_deps.ensure_maas_rhcl_dependency_stack",
    "install.install_minimal_deps.reconcile_rhcl_after_gitops_apply",
    "install.install_minimal_deps.require_maas_dependency_operators",
)


def _bash_setup_calls(mock_run: MagicMock) -> list:
    return [c for c in mock_run.call_args_list if c.args and c.args[0] and c.args[0][0] == "bash"]


class _PatchStack(ExitStack):
    """Flatten env + patch.context nesting in install_minimal_deps tests."""

    def with_env(self, env: dict[str, str]) -> _PatchStack:
        self.enter_context(patch.dict(os.environ, env, clear=True))
        return self

    def with_patch(self, target: str, **kwargs: Any) -> MagicMock:
        return self.enter_context(patch(target, **kwargs))

    def with_maas_common_patches(self) -> _PatchStack:
        for target in _MAAS_COMMON_PATCHES:
            self.with_patch(target)
        return self


class InstallMinimalDepsTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls._ns_ready_patcher = patch(
            "install.install_minimal_deps.ensure_setup_dependency_namespaces_ready",
        )
        cls._ns_ready_patcher.start()
        cls._maas_bvt_patcher = patch(
            "install.install_minimal_deps._ensure_maas_bvt_prerequisites",
        )
        cls._maas_bvt_patcher.start()
        cls._existing_stack_patcher = patch(
            "install.install_minimal_deps.existing_dependency_stack_ready",
            return_value=False,
        )
        cls._existing_stack_patcher.start()
        cls._serverless_patcher = patch(
            "install.install_minimal_deps.ensure_serverless_operator",
        )
        cls._serverless_patcher.start()
        cls._maas_prep_patcher = patch(
            "components.maas_billing.prep.try_prepare_maas_smoke",
        )
        cls._maas_prep_patcher.start()
        cls._jobset_lws_patcher = patch(
            "install.install_minimal_deps.ensure_jobset_and_lws_operator_crs",
        )
        cls._jobset_lws_patcher.start()
        cls._openshift_gateway_patcher = patch(
            "install.install_minimal_deps.ensure_openshift_gateway_istio_for_dep_operators",
            return_value=True,
        )
        cls._openshift_gateway_patcher.start()
        cls._reconcile_servicemesh_patcher = patch(
            "install.install_minimal_deps.reconcile_servicemesh_olm_conflicts",
            return_value=0,
        )
        cls._reconcile_servicemesh_patcher.start()
        cls._approve_installplans_patcher = patch(
            "install.install_minimal_deps.approve_pending_installplans",
            return_value=0,
        )
        cls._approve_installplans_patcher.start()
        cls._wait_servicemesh_csv_patcher = patch(
            "install.install_minimal_deps.wait_servicemesh_csv_succeeded",
            return_value=True,
        )
        cls._wait_servicemesh_csv_patcher.start()

    @classmethod
    def tearDownClass(cls) -> None:
        cls._wait_servicemesh_csv_patcher.stop()
        cls._approve_installplans_patcher.stop()
        cls._reconcile_servicemesh_patcher.stop()
        cls._openshift_gateway_patcher.stop()
        cls._jobset_lws_patcher.stop()
        cls._maas_prep_patcher.stop()
        cls._serverless_patcher.stop()
        cls._existing_stack_patcher.stop()
        cls._maas_bvt_patcher.stop()
        cls._ns_ready_patcher.stop()

    def test_skips_when_maas_operators_already_ready_without_extra_args(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            kubeconfig = root / "kubeconfig"
            kubeconfig.write_text("apiVersion: v1\nkind: Config\n", encoding="utf-8")
            olm_dir = root / "olminstall"
            olm_dir.mkdir()
            (olm_dir / "setup-dependencies.sh").write_text("#!/bin/bash\nexit 1\n", encoding="utf-8")

            env = _minimal_env(
                olm_dir=olm_dir,
                kubeconfig=kubeconfig,
                COMPONENTS_CSV="model_server",
            )
            env.pop("SETUP_DEPENDENCIES_ARGS", None)
            with _PatchStack() as stack:
                stack.with_env(env)
                stack.with_patch(
                    "install.install_minimal_deps.maas_dependency_operators_ready",
                    return_value=True,
                )
                stack.with_maas_common_patches()
                run = stack.with_patch("install.install_minimal_deps.subprocess.run")
                self.assertEqual(main(), 0)
                self.assertEqual(_bash_setup_calls(run), [])

    def test_runs_setup_when_extra_args_and_stack_not_ready(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            kubeconfig = root / "kubeconfig"
            kubeconfig.write_text("apiVersion: v1\nkind: Config\n", encoding="utf-8")
            olm_dir = root / "olminstall"
            olm_dir.mkdir()
            (olm_dir / "setup-dependencies.sh").write_text("#!/bin/bash\nexit 0\n", encoding="utf-8")

            env = _minimal_env(
                olm_dir=olm_dir,
                kubeconfig=kubeconfig,
                SETUP_DEPENDENCIES_ARGS="-M",
                COMPONENTS_CSV="model_server",
            )
            with _PatchStack() as stack:
                stack.with_env(env)
                stack.with_patch(
                    "install.install_minimal_deps.existing_dependency_stack_ready",
                    return_value=False,
                )
                stack.with_patch(
                    "install.install_minimal_deps.maas_dependency_operators_ready",
                    return_value=True,
                )
                stack.with_maas_common_patches()
                stack.with_patch("install.install_minimal_deps.patch_odh_gitops_keda_pod_selector")
                stack.with_patch(
                    "install.install_minimal_deps._prepare_setup_env",
                    side_effect=lambda e: e,
                )
                run = stack.with_patch("install.install_minimal_deps.subprocess.run")
                run.return_value = MagicMock(returncode=0)
                self.assertEqual(main(), 0)
                self.assertEqual(len(_bash_setup_calls(run)), 1)

    def test_skips_setup_on_existing_when_dependency_stack_ready(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            kubeconfig = root / "kubeconfig"
            kubeconfig.write_text("apiVersion: v1\nkind: Config\n", encoding="utf-8")
            olm_dir = root / "olminstall"
            olm_dir.mkdir()
            (olm_dir / "setup-dependencies.sh").write_text("#!/bin/bash\nexit 1\n", encoding="utf-8")

            env = _minimal_env(
                olm_dir=olm_dir,
                kubeconfig=kubeconfig,
                PRODUCT="",
                SETUP_DEPENDENCIES_ARGS="-M",
                COMPONENTS_CSV="maas_billing",
            )
            with patch.dict(os.environ, env, clear=True):
                with patch(
                    "install.install_minimal_deps.existing_dependency_stack_ready",
                    return_value=True,
                ):
                    with patch("install.install_minimal_deps.ensure_maas_rhcl_dependency_stack"):
                        with patch("install.install_minimal_deps.require_maas_dependency_operators"):
                            with patch("install.install_minimal_deps.mark_dep_operators_done"):
                                with patch(
                                    "install.install_minimal_deps.patch_odh_gitops_keda_pod_selector",
                                ):
                                    with patch("install.install_minimal_deps.subprocess.run") as run:
                                        self.assertEqual(main(), 0)
                                        self.assertEqual(_bash_setup_calls(run), [])

    def test_reconcile_rhcl_after_setup_dependencies_on_maas_smoke(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            kubeconfig = root / "kubeconfig"
            kubeconfig.write_text("apiVersion: v1\nkind: Config\n", encoding="utf-8")
            olm_dir = root / "olminstall"
            olm_dir.mkdir()
            (olm_dir / "setup-dependencies.sh").write_text("#!/bin/bash\nexit 0\n", encoding="utf-8")

            env = _minimal_env(
                olm_dir=olm_dir,
                kubeconfig=kubeconfig,
                SETUP_DEPENDENCIES_ARGS="-M",
                COMPONENTS_CSV="model_server",
            )
            with patch.dict(os.environ, env, clear=True):
                with patch(
                    "install.install_minimal_deps.maas_dependency_operators_ready",
                    side_effect=[False, True],
                ):
                    with patch("install.install_minimal_deps.ensure_maas_rhcl_dependency_stack"):
                        with patch(
                            "install.install_minimal_deps.reconcile_rhcl_after_gitops_apply",
                        ) as reconcile:
                            with patch(
                                "install.install_minimal_deps.require_maas_dependency_operators",
                            ):
                                with patch(
                                    "install.install_minimal_deps._prepare_setup_env",
                                    side_effect=lambda e: e,
                                ):
                                    with patch(
                                        "install.install_minimal_deps.subprocess.run",
                                        return_value=subprocess.CompletedProcess(args=[], returncode=0),
                                    ):
                                        self.assertEqual(main(), 0)
                                        reconcile.assert_called_once()

    def test_product_install_warns_and_continues_when_setup_dependencies_fails(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            kubeconfig = root / "kubeconfig"
            kubeconfig.write_text("apiVersion: v1\nkind: Config\n", encoding="utf-8")
            olm_dir = root / "olminstall"
            olm_dir.mkdir()
            (olm_dir / "setup-dependencies.sh").write_text("#!/bin/bash\nexit 2\n", encoding="utf-8")

            env = _minimal_env(
                olm_dir=olm_dir,
                kubeconfig=kubeconfig,
                SETUP_DEPENDENCIES_ARGS="-M",
                PRODUCT="rhoai",
            )
            with patch.dict(os.environ, env, clear=True):
                with patch(
                    "install.install_minimal_deps._prepare_setup_env",
                    side_effect=lambda e: e,
                ):
                    with patch(
                        "install.install_minimal_deps.subprocess.run",
                        return_value=subprocess.CompletedProcess(args=[], returncode=2),
                    ):
                        with patch(
                            "install.install_minimal_deps.finalize_dependency_operators_after_setup_script",
                            return_value=2,
                        ) as finalize:
                            self.assertEqual(main(), 2)
                            finalize.assert_called_once()

    def test_product_install_falls_through_to_rhcl_when_finalize_warns(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            kubeconfig = root / "kubeconfig"
            kubeconfig.write_text("apiVersion: v1\nkind: Config\n", encoding="utf-8")
            olm_dir = root / "olminstall"
            olm_dir.mkdir()
            (olm_dir / "setup-dependencies.sh").write_text("#!/bin/bash\nexit 2\n", encoding="utf-8")

            env = _minimal_env(
                olm_dir=olm_dir,
                kubeconfig=kubeconfig,
                SETUP_DEPENDENCIES_ARGS="-M",
                PRODUCT="rhoai",
                COMPONENTS_CSV="maas_billing",
            )
            with patch.dict(os.environ, env, clear=True):
                with patch(
                    "install.install_minimal_deps._prepare_setup_env",
                    side_effect=lambda e: e,
                ):
                    with patch(
                        "install.install_minimal_deps.subprocess.run",
                        return_value=subprocess.CompletedProcess(args=[], returncode=2),
                    ):
                        with patch(
                            "install.install_minimal_deps.finalize_dependency_operators_after_setup_script",
                            return_value=2,
                        ):
                            with patch(
                                "install.install_minimal_deps.reconcile_rhcl_after_gitops_apply",
                            ):
                                with patch(
                                    "install.install_minimal_deps.ensure_maas_rhcl_dependency_stack",
                                ) as ensure:
                                    with patch(
                                        "install.install_minimal_deps.require_maas_dependency_operators",
                                    ):
                                        self.assertEqual(main(), 0)
                                        self.assertEqual(ensure.call_count, 2)

    def test_product_install_fails_when_maas_smoke_requires_authorino(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            kubeconfig = root / "kubeconfig"
            kubeconfig.write_text("apiVersion: v1\nkind: Config\n", encoding="utf-8")
            olm_dir = root / "olminstall"
            olm_dir.mkdir()
            (olm_dir / "setup-dependencies.sh").write_text("#!/bin/bash\nexit 2\n", encoding="utf-8")

            env = _minimal_env(
                olm_dir=olm_dir,
                kubeconfig=kubeconfig,
                SETUP_DEPENDENCIES_ARGS="-M",
                PRODUCT="",
                COMPONENTS_CSV="maas_billing",
            )
            with patch.dict(os.environ, env, clear=True):
                with patch("install.install_minimal_deps.ensure_maas_rhcl_dependency_stack"):
                    with patch(
                        "install.install_minimal_deps._prepare_setup_env",
                        side_effect=lambda e: e,
                    ):
                        with patch(
                            "install.install_minimal_deps.subprocess.run",
                            return_value=subprocess.CompletedProcess(args=[], returncode=2),
                        ):
                            with patch(
                                "install.install_minimal_deps.finalize_dependency_operators_after_setup_script",
                                return_value=2,
                            ):
                                with patch(
                                    "install.install_minimal_deps.reconcile_rhcl_after_gitops_apply",
                                ):
                                    self.assertEqual(main(), 1)

    def test_runs_setup_dependencies_when_maas_operators_missing(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            kubeconfig = root / "kubeconfig"
            kubeconfig.write_text("apiVersion: v1\nkind: Config\n", encoding="utf-8")
            olm_dir = root / "olminstall"
            olm_dir.mkdir()
            (olm_dir / "setup-dependencies.sh").write_text("#!/bin/bash\nexit 0\n", encoding="utf-8")

            env = _minimal_env(
                olm_dir=olm_dir,
                kubeconfig=kubeconfig,
                SETUP_DEPENDENCIES_ARGS="-M",
                COMPONENTS_CSV="maas_billing",
            )
            with patch.dict(os.environ, env, clear=True):
                with patch(
                    "install.install_minimal_deps.maas_dependency_operators_ready",
                    side_effect=[False, True],
                ):
                    with patch("install.install_minimal_deps.ensure_maas_rhcl_dependency_stack"):
                        with patch("install.install_minimal_deps.reconcile_rhcl_after_gitops_apply"):
                            with patch("install.install_minimal_deps.require_maas_dependency_operators"):
                                with patch(
                                    "install.install_minimal_deps._prepare_setup_env",
                                    side_effect=lambda e: e,
                                ):
                                    with patch(
                                        "install.install_minimal_deps.finalize_dependency_operators_after_setup_script",
                                    ) as finalize:
                                        with patch(
                                            "install.install_minimal_deps.subprocess.run",
                                            return_value=subprocess.CompletedProcess(args=[], returncode=0),
                                        ) as run:
                                            self.assertEqual(main(), 0)
                                            self.assertEqual(len(_bash_setup_calls(run)), 1)
                                            finalize.assert_not_called()

    def test_product_install_recovers_authorino_when_setup_succeeds_but_cr_missing(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            kubeconfig = root / "kubeconfig"
            kubeconfig.write_text("apiVersion: v1\nkind: Config\n", encoding="utf-8")
            olm_dir = root / "olminstall"
            olm_dir.mkdir()
            (olm_dir / "setup-dependencies.sh").write_text("#!/bin/bash\nexit 0\n", encoding="utf-8")

            env = _minimal_env(
                olm_dir=olm_dir,
                kubeconfig=kubeconfig,
                SETUP_DEPENDENCIES_ARGS="-M",
                PRODUCT="rhoai",
                COMPONENTS_CSV="maas_billing",
            )
            with patch.dict(os.environ, env, clear=True):
                with patch(
                    "install.install_minimal_deps.maas_dependency_operators_ready",
                    return_value=False,
                ):
                    with patch(
                        "install.install_minimal_deps.recover_authorino_after_setup_script",
                    ) as recover:
                        with patch(
                            "install.install_minimal_deps.reconcile_rhcl_after_gitops_apply",
                        ):
                            with patch(
                                "install.install_minimal_deps.ensure_maas_rhcl_dependency_stack",
                            ):
                                with patch(
                                    "install.install_minimal_deps.require_maas_dependency_operators",
                                ) as require:
                                    with patch(
                                        "install.install_minimal_deps._prepare_setup_env",
                                        side_effect=lambda e: e,
                                    ):
                                        with patch(
                                            "install.install_minimal_deps.subprocess.run",
                                            return_value=subprocess.CompletedProcess(args=[], returncode=0),
                                        ):
                                            self.assertEqual(main(), 0)
                                            recover.assert_called_once()
                                            require.assert_called_once_with(
                                                allow_deferred_authorino=True,
                                            )

    def test_product_install_fails_after_finalize_nonzero(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            kubeconfig = root / "kubeconfig"
            kubeconfig.write_text("apiVersion: v1\nkind: Config\n", encoding="utf-8")
            olm_dir = root / "olminstall"
            olm_dir.mkdir()
            (olm_dir / "setup-dependencies.sh").write_text("#!/bin/bash\nexit 2\n", encoding="utf-8")

            env = _minimal_env(
                olm_dir=olm_dir,
                kubeconfig=kubeconfig,
                SETUP_DEPENDENCIES_ARGS="-M",
                PRODUCT="rhoai",
                COMPONENTS_CSV="maas_billing",
            )
            with patch.dict(os.environ, env, clear=True):
                with patch(
                    "install.install_minimal_deps._prepare_setup_env",
                    side_effect=lambda e: e,
                ):
                    with patch(
                        "install.install_minimal_deps.subprocess.run",
                        return_value=subprocess.CompletedProcess(args=[], returncode=2),
                    ):
                        with patch(
                            "install.install_minimal_deps.finalize_dependency_operators_after_setup_script",
                            return_value=1,
                        ):
                            with patch(
                                "install.install_minimal_deps.reconcile_rhcl_after_gitops_apply",
                            ):
                                with patch(
                                    "install.install_minimal_deps.ensure_maas_rhcl_dependency_stack",
                                ) as ensure:
                                    self.assertEqual(main(), 1)
                                    # Preflight RHCL may run once before setup; no post-fail recovery.
                                    self.assertLessEqual(ensure.call_count, 1)

    def test_product_install_deferred_authorino_when_rhcl_functional(self) -> None:
        with patch.dict(
            os.environ,
            {"PRODUCT": "rhoai"},
            clear=False,
        ):
            with patch(
                "install.dependency_operators.maas_dependency_operators_ready",
                return_value=False,
            ):
                with patch(
                    "install.rhcl_deps.rhcl_stack_functional",
                    return_value=True,
                ):
                    from install.dependency_operators import require_maas_dependency_operators

                    require_maas_dependency_operators(allow_deferred_authorino=True)

    def test_install_dependencies_deferred_authorino_when_rhcl_functional(self) -> None:
        with patch.dict(
            os.environ,
            {"PRODUCT": "", "INSTALL_DEPENDENCIES": "true"},
            clear=False,
        ):
            with patch(
                "install.dependency_operators.maas_dependency_operators_ready",
                return_value=False,
            ):
                with patch(
                    "install.rhcl_deps.rhcl_stack_functional",
                    return_value=True,
                ):
                    from install.dependency_operators import require_maas_dependency_operators

                    require_maas_dependency_operators(allow_deferred_authorino=True)

    def test_maas_smoke_calls_bvt_prerequisites_after_authorino(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            kubeconfig = root / "kubeconfig"
            kubeconfig.write_text("apiVersion: v1\nkind: Config\n", encoding="utf-8")
            olm_dir = root / "olminstall"
            olm_dir.mkdir()
            (olm_dir / "setup-dependencies.sh").write_text("#!/bin/bash\nexit 0\n", encoding="utf-8")

            env = _minimal_env(
                olm_dir=olm_dir,
                kubeconfig=kubeconfig,
                SETUP_DEPENDENCIES_ARGS="-M",
                COMPONENTS_CSV="maas_billing",
            )
            with patch.dict(os.environ, env, clear=True):
                with patch(
                    "install.install_minimal_deps.maas_dependency_operators_ready",
                    return_value=False,
                ):
                    with patch("install.install_minimal_deps.ensure_maas_rhcl_dependency_stack"):
                        with patch("install.install_minimal_deps.reconcile_rhcl_after_gitops_apply"):
                            with patch(
                                "install.install_minimal_deps.require_maas_dependency_operators",
                            ):
                                with patch(
                                    "install.install_minimal_deps._ensure_maas_bvt_prerequisites",
                                ) as bvt_prereq:
                                    with patch("install.install_minimal_deps.mark_dep_operators_done"):
                                        with patch(
                                            "install.install_minimal_deps._prepare_setup_env",
                                            side_effect=lambda e: e,
                                        ):
                                            with patch(
                                                "install.install_minimal_deps.subprocess.run",
                                                return_value=subprocess.CompletedProcess(
                                                    args=[], returncode=0
                                                ),
                                            ):
                                                self.assertEqual(main(), 0)
                                                bvt_prereq.assert_called_once()

    def test_rhoai_install_reconciles_openshift_gateway_istio_before_setup(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            kubeconfig = root / "kubeconfig"
            kubeconfig.write_text("apiVersion: v1\nkind: Config\n", encoding="utf-8")
            olm_dir = root / "olminstall"
            olm_dir.mkdir()

            env = _minimal_env(
                olm_dir=olm_dir,
                kubeconfig=kubeconfig,
                PRODUCT="rhoai",
                COMPONENTS_CSV="dashboard_cypress",
            )
            with patch.dict(os.environ, env, clear=True):
                with patch(
                    "install.install_minimal_deps.ensure_openshift_gateway_istio_for_dep_operators",
                    return_value=True,
                ) as reconcile:
                    self.assertEqual(main(), 0)
                    reconcile.assert_called_once()

    def test_maas_bvt_prereqs_defer_aigateway_when_maas_api_missing(self) -> None:
        self._maas_bvt_patcher.stop()
        try:
            with patch("components.maas_billing.database.ensure_maas_database"):
                with patch(
                    "components.maas_billing.common.maas_api_deployment_exists",
                    return_value=False,
                ):
                    with patch(
                        "install.install_minimal_deps.ensure_dsc_models_as_service",
                    ) as dsc:
                        from install.install_minimal_deps import _ensure_maas_bvt_prerequisites

                        _ensure_maas_bvt_prerequisites()
                        dsc.assert_called_once_with(wait_for_aigateway=False)
        finally:
            self._maas_bvt_patcher.start()


if __name__ == "__main__":
    raise SystemExit(unittest.main())
