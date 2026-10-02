#!/usr/bin/env python3
"""Unit tests for scoped OLM reset before reinstall."""

from __future__ import annotations

import json
import unittest
from unittest.mock import patch

from install import install_and_verify as iav


class ResetStaleOperatorInstallTest(unittest.TestCase):
    @patch.object(iav, "delete_failed_olm_bundle_unpack_jobs")
    @patch.object(iav, "oc_run")
    def test_reset_deletes_only_operator_csvs_not_all(self, oc_run, _unpack_jobs) -> None:
        csv_doc = {
            "items": [
                {"metadata": {"name": "rhods-operator.v3.6.0"}},
                {"metadata": {"name": "loki-operator.v0.1.0"}},
            ]
        }
        ip_doc = {
            "items": [
                {
                    "metadata": {"name": "install-rhods"},
                    "spec": {"clusterServiceVersionNames": ["rhods-operator.v3.6.0"]},
                },
                {
                    "metadata": {"name": "install-loki"},
                    "spec": {"clusterServiceVersionNames": ["loki-operator.v0.1.0"]},
                },
            ]
        }

        def oc_side_effect(args, **kwargs):
            if args[:3] == ["get", "clusterserviceversion", "-n"]:
                return type("R", (), {"returncode": 0, "stdout": json.dumps(csv_doc), "stderr": ""})()
            if args[:3] == ["get", "installplan", "-n"]:
                return type("R", (), {"returncode": 0, "stdout": json.dumps(ip_doc), "stderr": ""})()
            if args[0] == "get" and args[1] == "subscription":
                return type("R", (), {"returncode": 1, "stdout": "", "stderr": "not found"})()
            return type("R", (), {"returncode": 0, "stdout": "", "stderr": ""})()

        oc_run.side_effect = oc_side_effect
        iav.reset_stale_operator_install("redhat-ods-operator", "rhods-operator", "rhoai-catalog")

        delete_csv = [
            call
            for call in oc_run.call_args_list
            if call.args[0][:2] == ["delete", "clusterserviceversion"]
        ]
        self.assertEqual(len(delete_csv), 1)
        self.assertIn("rhods-operator.v3.6.0", delete_csv[0].args[0])

        delete_ip = [
            call for call in oc_run.call_args_list if call.args[0][:2] == ["delete", "installplan"]
        ]
        self.assertEqual(len(delete_ip), 1)
        self.assertIn("install-rhods", delete_ip[0].args[0])


if __name__ == "__main__":
    raise SystemExit(unittest.main())
