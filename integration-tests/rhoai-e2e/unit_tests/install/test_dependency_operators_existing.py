#!/usr/bin/env python3
"""Tests for MaaS dependency probe messaging on PRODUCT=existing."""

from __future__ import annotations

import unittest
from unittest import mock

from install.dependency_operators import (  # noqa: E402
    _maas_deps_missing_message,
    custom_metrics_autoscaler_operator_ready,
    existing_smoke_without_install_dependencies,
    patch_odh_gitops_keda_pod_selector,
    require_maas_dependency_operators,
)

class DependencyOperatorsExistingTest(unittest.TestCase):
    def test_existing_smoke_without_install_dependencies(self) -> None:
        with mock.patch.dict("os.environ", {"PRODUCT": ""}, clear=False):
            self.assertTrue(existing_smoke_without_install_dependencies())
        with mock.patch.dict(
            "os.environ",
            {"PRODUCT": "", "INSTALL_DEPENDENCIES": "true"},
            clear=False,
        ):
            self.assertFalse(existing_smoke_without_install_dependencies())
        with mock.patch.dict("os.environ", {"PRODUCT": "rhoai"}, clear=False):
            self.assertFalse(existing_smoke_without_install_dependencies())

    def test_message_mentions_install_dependencies_flag(self) -> None:
        with mock.patch.dict("os.environ", {"PRODUCT": ""}, clear=False):
            msg = _maas_deps_missing_message()
        self.assertIn("--install-dependencies", msg)

    def test_require_raises_with_retrigger_hint_on_existing(self) -> None:
        with (
            mock.patch.dict("os.environ", {"PRODUCT": ""}, clear=False),
            mock.patch(
                "install.dependency_operators.maas_dependency_operators_ready",
                return_value=False,
            ),
            mock.patch(
                "install.dependency_operators.authorino_deferred_to_component_prep",
                return_value=False,
            ),
        ):
            with self.assertRaisesRegex(RuntimeError, "--install-dependencies"):
                require_maas_dependency_operators()

    def test_patch_odh_gitops_keda_pod_selector(self) -> None:
        import tempfile
        from pathlib import Path

        with tempfile.TemporaryDirectory() as tmp:
            olm_root = Path(tmp)
            gitops = olm_root / "odh-gitops" / "scripts"
            gitops.mkdir(parents=True)
            script = gitops / "verify.sh"
            script.write_text('wait pods -l app=keda-operator\n', encoding="utf-8")
            patch_odh_gitops_keda_pod_selector(olm_root)
            self.assertIn(
                "name=custom-metrics-autoscaler-operator",
                script.read_text(encoding="utf-8"),
            )

    def test_custom_metrics_autoscaler_ready_checks_csv_and_pod(self) -> None:
        with mock.patch("install.dependency_operators.oc_run") as oc_run:
            oc_run.side_effect = [
                mock.Mock(returncode=0, stdout="custom-metrics-autoscaler.v2.19.0-1\tSucceeded\n"),
                mock.Mock(returncode=0, stdout="custom-metrics-autoscaler-operator-abc"),
            ]
            self.assertTrue(custom_metrics_autoscaler_operator_ready())

