"""ROSA HCP Kyverno readiness checks."""

from __future__ import annotations

import unittest
from unittest import mock

class RosaHcpPullSetupReadyTests(unittest.TestCase):
    def test_accepts_vault_registry_policy_when_legacy_name_expected(self) -> None:
        from install import rosa_hcp_pull_setup as rhps

        def fake_run_oc(args, **kwargs):
            cmd = " ".join(args)
            if cmd == "get ns kyverno":
                return mock.Mock(returncode=0, stdout="")
            if "clusterpolicy sync-secrets" in cmd:
                return mock.Mock(returncode=0, stdout="true")
            if "clusterpolicy add-imagepullsecrets" in cmd:
                return mock.Mock(returncode=0, stdout="true")
            if "clusterpolicy replace-rhoai-registry" in cmd:
                return mock.Mock(returncode=1, stdout="")
            if "clusterpolicy replace-image-registry" in cmd:
                return mock.Mock(returncode=0, stdout="true")
            if cmd == "get secret pull-secret-quay -n openshift-config":
                return mock.Mock(returncode=0, stdout="")
            return mock.Mock(returncode=1, stdout="")

        with (
            mock.patch.object(rhps, "run_oc", side_effect=fake_run_oc),
            mock.patch.object(
                rhps,
                "active_kyverno_policy_names",
                return_value=("sync-secrets", "add-imagepullsecrets", "replace-rhoai-registry"),
            ),
        ):
            self.assertTrue(rhps.rosa_hcp_pull_setup_ready())

