"""Unit tests for install phase loading (no cluster)."""

from __future__ import annotations

import json
import os
import unittest
from subprocess import CompletedProcess
from unittest.mock import patch

from install.install_and_verify import (validate_dns_label,
                                        validate_operator_namespace)


class InstallValidationTest(unittest.TestCase):
    def test_validate_operator_namespace_accepts_default(self) -> None:
        validate_operator_namespace("redhat-ods-operator")

    def test_validate_dns_label_rejects_empty(self) -> None:
        with self.assertRaises(SystemExit):
            validate_dns_label("", "TEST")

    def test_manifest_source_namespace_defaults_to_marketplace(self) -> None:
        import tempfile
        from pathlib import Path

        from install.install_and_verify import manifest_source_namespace

        with tempfile.TemporaryDirectory() as directory:
            manifest = Path(directory) / "subscription.yaml"
            manifest.write_text("kind: Subscription\nspec:\n  source: rhoai-catalog\n", encoding="utf-8")
            self.assertEqual(manifest_source_namespace(manifest), "openshift-marketplace")

            manifest.write_text(
                "kind: Subscription\nspec:\n  sourceNamespace: catalog-tenant\n",
                encoding="utf-8",
            )
            self.assertEqual(manifest_source_namespace(manifest), "catalog-tenant")

    def test_guest_access_preflight_checks_task_identity(self) -> None:
        from install import install_and_verify as iav

        calls: list[list[str]] = []

        def fake_oc(args: list[str], **_kwargs: object) -> CompletedProcess[str]:
            calls.append(args)
            if args == ["whoami"]:
                return CompletedProcess(args, 0, stdout="guest-user\n", stderr="")
            if args == ["get", "clusterversion"]:
                return CompletedProcess(args, 0, stdout="version info\n", stderr="")
            if args[:4] == ["auth", "can-i", "get", "subscriptions"]:
                return CompletedProcess(args, 0, stdout="yes\n", stderr="")
            if args[:4] == ["auth", "can-i", "create", "jobs"]:
                return CompletedProcess(args, 1, stdout="no\n", stderr="")
            raise AssertionError(f"unexpected oc command: {args}")

        with patch.object(iav, "oc_run", side_effect=fake_oc):
            iav.verify_guest_access("redhat-ods-operator", "catalog-tenant")

        self.assertIn(["whoami"], calls)
        self.assertIn(["get", "clusterversion"], calls)
        self.assertIn(
            ["auth", "can-i", "get", "subscriptions", "-n", "redhat-ods-operator"],
            calls,
        )
        self.assertIn(["auth", "can-i", "create", "jobs", "-n", "catalog-tenant"], calls)

    def test_bundle_unpack_logger_uses_subscription_source_namespace(self) -> None:
        from install import install_and_verify as iav

        calls: list[list[str]] = []

        def fake_oc(args: list[str], **_kwargs: object) -> CompletedProcess[str]:
            calls.append(args)
            if args[:2] == ["get", "subscription"]:
                stdout = json.dumps({"spec": {"sourceNamespace": "catalog-tenant"}})
            elif args[:2] == ["get", "jobs"] and args[-1] == "json":
                stdout = json.dumps(
                    {
                        "items": [
                            {
                                "metadata": {"name": "unpack-job"},
                                "spec": {
                                    "template": {
                                        "spec": {
                                            "containers": [
                                                {"name": "extract"},
                                                {"name": "pull"},
                                            ]
                                        }
                                    }
                                },
                            }
                        ]
                    }
                )
            else:
                stdout = ""
            return CompletedProcess(args, 0, stdout=stdout, stderr="")

        with patch.object(iav, "oc_run", side_effect=fake_oc):
            iav.log_marketplace_bundle_unpack_state(
                operator_name="rhods-operator",
                operator_namespace="redhat-ods-operator",
            )

        self.assertIn(
            [
                "get", "jobs", "-n", "catalog-tenant", "-l",
                "operatorframework.io/bundle-unpack-ref", "-o", "wide",
            ],
            calls,
        )
        self.assertIn(
            ["get", "pods", "-n", "catalog-tenant", "-l", "job-name=unpack-job", "-o", "wide"],
            calls,
        )

class LoadInstallContextTest(unittest.TestCase):
    def test_load_install_context_missing_env(self) -> None:
        env = {k: v for k, v in os.environ.items() if not k.startswith("INSTALL_")}
        with patch.dict(os.environ, env, clear=True):
            with self.assertRaises(SystemExit):
                from install.install_phases import load_install_context

                load_install_context()


class OlmBundleUnpackTimeoutTest(unittest.TestCase):
    def test_default_timeout_ephc(self) -> None:
        from install.install_phases import \
          default_olm_bundle_unpack_timeout_sec

        with patch("install.install_phases.cluster_source_is_ephc", return_value=True):
            self.assertEqual(default_olm_bundle_unpack_timeout_sec(), 80 * 60)

    def test_default_timeout_external(self) -> None:
        from install.install_phases import \
          default_olm_bundle_unpack_timeout_sec

        with patch("install.install_phases.cluster_source_is_ephc", return_value=False):
            self.assertEqual(default_olm_bundle_unpack_timeout_sec(), 1800)

    def test_resolved_timeout_empty_tekton_param_uses_default(self) -> None:
        from install.install_phases import resolved_olm_bundle_unpack_timeout_sec

        with patch.dict(os.environ, {"OLM_BUNDLE_UNPACK_TIMEOUT_SEC": ""}, clear=False):
            with patch("install.install_phases.cluster_source_is_ephc", return_value=True):
                self.assertEqual(resolved_olm_bundle_unpack_timeout_sec(), 80 * 60)


class PostInstallDscTest(unittest.TestCase):
    def test_phase_post_install_dsc_defers_aigateway_when_maas_api_missing(self) -> None:
        from install.install_phases import phase_post_install_dsc

        with patch.dict(os.environ, {"COMPONENTS_CSV": "maas_billing"}, clear=False):
            with patch("install.install_phases.setup_dsc_resources"):
                with patch("install.install_phases._ensure_gateway_before_dsc_ready"):
                    with patch("install.install_phases.wait_dsc_ready", return_value=True):
                        with patch("install.install_phases.ensure_rhoai_gateway_for_install"):
                            with patch("install.install_phases.gateway_config_ready", return_value=True):
                                with patch(
                                    "components.maas_billing.common.maas_api_deployment_exists",
                                    return_value=False,
                                ):
                                    with patch(
                                        "install.install_phases.ensure_dsc_models_as_service",
                                    ) as dsc:
                                        phase_post_install_dsc(None)  # type: ignore[arg-type]
                                        dsc.assert_called_once_with(wait_for_aigateway=False)
