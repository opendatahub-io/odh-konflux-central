#!/usr/bin/env python3
"""HyperShift stub admission webhook neutralization."""

from __future__ import annotations

import json
import unittest
from unittest.mock import patch

from helpers.hypershift_admission_webhooks import (
    broken_hypershift_admission_webhook_reason,
    neutralize_broken_hypershift_admission_webhooks,
)


def _vwc(*, name: str, svc_name: str, svc_ns: str = "default") -> dict:
    return {
        "metadata": {"name": name},
        "webhooks": [
            {
                "name": "block-resources.hypershift.openshift.io",
                "clientConfig": {
                    "service": {"name": svc_name, "namespace": svc_ns, "path": "/validate"}
                },
            }
        ],
    }


class HypershiftAdmissionWebhooksTest(unittest.TestCase):
    @patch("install.gateway_config.cluster_source_is_ephc", return_value=True)
    @patch(
        "install.rosa_hcp_pull_setup.is_hypershift_managed_cluster",
        return_value=True,
    )
    @patch("helpers.hypershift_admission_webhooks.oc_run")
    def test_deletes_stub_service_webhook(
        self,
        oc_run: object,
        *_mocks: object,
    ) -> None:
        listed = {
            "items": [_vwc(name="hypershift-block", svc_name="xxx-invalid-service-xxx")]
        }

        def _side_effect(args: list[str], **_kwargs: object) -> object:
            if args[:2] == ["get", "validatingwebhookconfiguration"]:
                return type("R", (), {"returncode": 0, "stdout": json.dumps(listed), "stderr": ""})()
            if args[:2] == ["get", "mutatingwebhookconfiguration"]:
                return type("R", (), {"returncode": 0, "stdout": '{"items":[]}', "stderr": ""})()
            if args[:2] == ["delete", "validatingwebhookconfiguration"]:
                return type("R", (), {"returncode": 0, "stdout": "", "stderr": ""})()
            return type("R", (), {"returncode": 1, "stdout": "", "stderr": "not found"})()

        oc_run.side_effect = _side_effect
        removed = neutralize_broken_hypershift_admission_webhooks()
        self.assertEqual(removed, 1)
        delete_calls = [
            c
            for c in oc_run.call_args_list
            if c.args and c.args[0][:2] == ["delete", "validatingwebhookconfiguration"]
        ]
        self.assertEqual(len(delete_calls), 1)
        self.assertEqual(delete_calls[0].args[0][2], "hypershift-block")

    @patch("install.gateway_config.cluster_source_is_ephc", return_value=False)
    @patch(
        "install.rosa_hcp_pull_setup.is_hypershift_managed_cluster",
        return_value=False,
    )
    @patch("helpers.hypershift_admission_webhooks.oc_run")
    def test_skips_non_hypershift(
        self,
        oc_run: object,
        *_mocks: object,
    ) -> None:
        self.assertEqual(neutralize_broken_hypershift_admission_webhooks(), 0)
        oc_run.assert_not_called()

    @patch("install.gateway_config.cluster_source_is_ephc", return_value=True)
    @patch(
        "install.rosa_hcp_pull_setup.is_hypershift_managed_cluster",
        return_value=True,
    )
    @patch("helpers.hypershift_admission_webhooks.oc_run")
    def test_reason_reports_stub(
        self,
        oc_run: object,
        *_mocks: object,
    ) -> None:
        listed = {
            "items": [_vwc(name="hypershift-block", svc_name="xxx-invalid-service-xxx")]
        }

        def _side_effect(args: list[str], **_kwargs: object) -> object:
            if args[:2] == ["get", "validatingwebhookconfiguration"]:
                return type("R", (), {"returncode": 0, "stdout": json.dumps(listed), "stderr": ""})()
            if args[:2] == ["get", "mutatingwebhookconfiguration"]:
                return type("R", (), {"returncode": 0, "stdout": '{"items":[]}', "stderr": ""})()
            return type("R", (), {"returncode": 1, "stdout": "", "stderr": ""})()

        oc_run.side_effect = _side_effect
        reason = broken_hypershift_admission_webhook_reason()
        self.assertIn("broken HyperShift admission webhook", reason)
        self.assertIn("xxx-invalid-service-xxx", reason)

    @patch("install.gateway_config.cluster_source_is_ephc", return_value=True)
    @patch(
        "install.rosa_hcp_pull_setup.is_hypershift_managed_cluster",
        return_value=True,
    )
    @patch("helpers.hypershift_admission_webhooks.oc_run")
    def test_skips_delete_when_service_probe_errors(
        self,
        oc_run: object,
        *_mocks: object,
    ) -> None:
        listed = {
            "items": [_vwc(name="real-webhook", svc_name="real-admission-svc", svc_ns="openshift")]
        }

        def _side_effect(args: list[str], **_kwargs: object) -> object:
            if args[:2] == ["get", "validatingwebhookconfiguration"]:
                return type("R", (), {"returncode": 0, "stdout": json.dumps(listed), "stderr": ""})()
            if args[:2] == ["get", "mutatingwebhookconfiguration"]:
                return type("R", (), {"returncode": 0, "stdout": '{"items":[]}', "stderr": ""})()
            if args[:2] == ["get", "svc"]:
                return type(
                    "R",
                    (),
                    {"returncode": 1, "stdout": "", "stderr": "Unable to connect to the server"},
                )()
            if args[:1] == ["delete"]:
                raise AssertionError("must not delete when svc probe fails transiently")
            return type("R", (), {"returncode": 1, "stdout": "", "stderr": ""})()

        oc_run.side_effect = _side_effect
        self.assertEqual(neutralize_broken_hypershift_admission_webhooks(), 0)


if __name__ == "__main__":
    unittest.main()
