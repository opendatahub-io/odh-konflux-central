#!/usr/bin/env python3
"""Unit tests for MaaS smoke cluster cleanup (stale gateways and AITenants)."""

from __future__ import annotations

import json
import unittest
from unittest.mock import patch

from components.maas_billing.cluster_cleanup import (  # noqa: E402
    _prune_stale_maas_aitenants,
    _prune_stale_maas_e2e_gateways,
)


class MaasBillingClusterCleanupTest(unittest.TestCase):
    def test_prunes_e2e_gateways_not_default(self) -> None:
        gateways = {
            "items": [
                {
                    "metadata": {"name": "maas-default-gateway", "namespace": "openshift-ingress"},
                },
                {
                    "metadata": {"name": "e2e-aigw-a124ef78", "namespace": "openshift-ingress"},
                },
                {"metadata": {"name": "other-gateway", "namespace": "openshift-ingress"}},
            ]
        }

        def _oc_run(args, **kwargs):
            cmd = list(args)
            if cmd[:3] == ["get", "gateway", "-A"]:
                return type("R", (), {"returncode": 0, "stdout": json.dumps(gateways), "stderr": ""})()
            return type("R", (), {"returncode": 0, "stdout": "", "stderr": ""})()

        with patch("components.maas_billing.cluster_cleanup.oc_run", side_effect=_oc_run) as oc_run:
            _prune_stale_maas_e2e_gateways()
            delete_cmds = [list(c.args[0]) for c in oc_run.call_args_list if c.args[0][0] == "delete"]
            self.assertIn(
                ["delete", "gateway", "e2e-aigw-a124ef78", "-n", "openshift-ingress", "--ignore-not-found"],
                delete_cmds,
            )
            self.assertFalse(
                any(cmd[2] == "maas-default-gateway" for cmd in delete_cmds),
                "must not delete maas-default-gateway",
            )
            self.assertFalse(
                any(cmd[2] == "other-gateway" for cmd in delete_cmds),
                "must not delete unrelated gateways",
            )

    def test_prunes_aitenants_on_stale_e2e_gateway_only(self) -> None:
        aitenants = {
            "items": [
                {
                    "metadata": {"name": "good-tenant", "namespace": "rhoai-model-serving"},
                    "spec": {"gatewayRef": {"name": "maas-default-gateway"}},
                },
                {
                    "metadata": {"name": "stale-tenant", "namespace": "rhoai-model-serving"},
                    "spec": {"gatewayRef": {"name": "e2e-aigw-deadbeef"}},
                },
                {
                    "metadata": {"name": "other-tenant", "namespace": "rhoai-model-serving"},
                    "spec": {"gatewayRef": {"name": "custom-gateway"}},
                },
            ]
        }

        def _oc_run(args, **kwargs):
            cmd = list(args)
            if cmd[:3] == ["get", "aitenants", "-A"]:
                return type("R", (), {"returncode": 0, "stdout": json.dumps(aitenants), "stderr": ""})()
            return type("R", (), {"returncode": 0, "stdout": "", "stderr": ""})()

        with patch("components.maas_billing.cluster_cleanup.oc_run", side_effect=_oc_run) as oc_run:
            _prune_stale_maas_aitenants()
            delete_cmds = [list(c.args[0]) for c in oc_run.call_args_list if c.args[0][0] == "delete"]
            self.assertIn(
                [
                    "delete",
                    "aitenants",
                    "stale-tenant",
                    "-n",
                    "rhoai-model-serving",
                    "--ignore-not-found",
                ],
                delete_cmds,
            )
            self.assertFalse(
                any(cmd[2] == "good-tenant" for cmd in delete_cmds),
                "must not delete tenant on maas-default-gateway",
            )
            self.assertFalse(
                any(cmd[2] == "other-tenant" for cmd in delete_cmds),
                "must not delete tenant on non-e2e gateway",
            )


if __name__ == "__main__":
    raise SystemExit(unittest.main())
