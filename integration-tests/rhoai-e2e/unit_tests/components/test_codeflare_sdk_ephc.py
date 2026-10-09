#!/usr/bin/env python3
"""Tests for CodeFlare SDK EPHC kubeconfig auth wrapper."""

from __future__ import annotations

import unittest
from unittest import mock

from components.codeflare_sdk.ephc import (  # noqa: E402
    codeflare_ephc_kubeconfig_run_prefix,
    codeflare_ephc_run_tests_auth_patch_shell,
    prepend_codeflare_ephc_kubeconfig_auth,
)

class CodeflareSdkEaasTest(unittest.TestCase):
    def test_prefix_unsets_legacy_vault_vars(self) -> None:
        with mock.patch.dict("os.environ", {"CLUSTER_SOURCE": "EPHC", "OPENSHIFT_TOKEN": "tok"}, clear=False):
            with mock.patch("components.codeflare_sdk.ephc._cluster_is_byoidc", return_value=False):
                with mock.patch("components.codeflare_sdk.ephc.cluster_has_htpasswd_identity", return_value=False):
                    prefix = codeflare_ephc_kubeconfig_run_prefix()
        self.assertIn("unset OCP_ADMIN_USER_USERNAME", prefix)
        self.assertIn("CLUSTER_AUTH=openshift", prefix)
        self.assertIn("oc login --token=", prefix)
        self.assertIn("OC_SERVER", prefix)

    def test_run_tests_patch_sets_byoidc_when_cluster_auth_openshift(self) -> None:
        shell = codeflare_ephc_run_tests_auth_patch_shell()
        self.assertIn("run-tests.sh", shell)
        self.assertIn("CLUSTER_AUTH=openshift", shell)
        self.assertIn("CLUSTER_IS_BYOIDC=true", shell)

    def test_prepend_wraps_run_command(self) -> None:
        with mock.patch(
            "components.codeflare_sdk.ephc.codeflare_ephc_kubeconfig_run_prefix",
            return_value="export CLUSTER_AUTH=openshift; ",
        ):
            out = prepend_codeflare_ephc_kubeconfig_auth("bash run-tests.sh -m smoke")
        self.assertTrue(out.startswith("export CLUSTER_AUTH=openshift; "))
        self.assertIn("run-tests.sh", out)
        self.assertTrue(out.endswith("bash run-tests.sh -m smoke"))

