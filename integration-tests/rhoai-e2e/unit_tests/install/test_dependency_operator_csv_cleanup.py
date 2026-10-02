"""Unit tests for bulk dependency operator CSV pre-cleanup."""

from __future__ import annotations

import json
import unittest
from unittest.mock import patch

from install.dependency_operator_csv_cleanup import (
    _SUBSCRIPTION_NAMESPACES,
    _matches_dependency_csv_prefix,
    cleanup_dependency_operator_csvs,
)


class DependencyOperatorCsvCleanupTest(unittest.TestCase):
    def test_matches_dependency_csv_prefixes(self) -> None:
        prefixes = ("servicemeshoperator", "loki-operator", "rhcl-operator")
        self.assertTrue(_matches_dependency_csv_prefix("servicemeshoperator3.v3.1.0", prefixes))
        self.assertTrue(_matches_dependency_csv_prefix("loki-operator.v6.5.2", prefixes))
        self.assertTrue(_matches_dependency_csv_prefix("rhcl-operator.v1.3.4", prefixes))
        self.assertFalse(_matches_dependency_csv_prefix("rhods-operator.v2.19.0", prefixes))

    @patch("install.dependency_operator_csv_cleanup.oc_run")
    def test_bulk_deletes_matching_subscriptions_and_csvs(self, oc_run) -> None:
        sub_json = {
            "items": [
                {
                    "metadata": {"name": "servicemeshoperator3"},
                    "spec": {"name": "servicemeshoperator3"},
                }
            ]
        }
        csv_json = {
            "items": [
                {
                    "metadata": {
                        "name": "servicemeshoperator3.v3.1.0",
                        "namespace": "default",
                    }
                },
                {
                    "metadata": {
                        "name": "servicemeshoperator3.v3.1.0",
                        "namespace": "openshift-ingress",
                    }
                },
                {
                    "metadata": {
                        "name": "rhods-operator.v2.19.0",
                        "namespace": "redhat-ods-operator",
                    }
                },
            ]
        }

        empty = json.dumps({"items": []})
        delete_csv_calls = 0

        def _oc_run(args, **kwargs):
            nonlocal delete_csv_calls
            if args[:3] == ["get", "subscription", "-n"]:
                if args[3] == "openshift-operators":
                    return type("R", (), {"returncode": 0, "stdout": json.dumps(sub_json), "stderr": ""})()
                return type("R", (), {"returncode": 0, "stdout": empty, "stderr": ""})()
            if args[:3] == ["get", "csv", "-A"]:
                return type("R", (), {"returncode": 0, "stdout": json.dumps(csv_json), "stderr": ""})()
            if args[0] == "delete" and args[1] == "subscription":
                self.assertEqual(args[2], "servicemeshoperator3")
                self.assertEqual(args[3], "-n")
                self.assertEqual(args[4], "openshift-operators")
                self.assertEqual(args[-2:], ["--ignore-not-found", "--wait=false"])
                return type("R", (), {"returncode": 0, "stdout": "", "stderr": ""})()
            if args[0] == "delete" and args[1] == "csv":
                self.assertEqual(args[-4], "-n")
                self.assertIn(args[-1], ("--wait=false",))
                self.assertEqual(set(args[2:-4]), {"servicemeshoperator3.v3.1.0"})
                delete_csv_calls += 1
                return type("R", (), {"returncode": 0, "stdout": "", "stderr": ""})()
            raise AssertionError(f"unexpected oc_run args: {args}")

        oc_run.side_effect = _oc_run

        cleanup_dependency_operator_csvs()

        self.assertEqual(delete_csv_calls, 2)

    @patch("install.dependency_operator_csv_cleanup.oc_run")
    def test_skips_when_disabled(self, oc_run) -> None:
        with patch.dict("os.environ", {"CLEANUP_DEPENDENCY_OPERATOR_CSVS": "0"}, clear=False):
            cleanup_dependency_operator_csvs()
        oc_run.assert_not_called()

    @patch("install.dependency_operator_csv_cleanup.oc_run")
    def test_no_matches_is_noop(self, oc_run) -> None:
        empty = json.dumps({"items": []})

        def _oc_run(args, **kwargs):
            if args[0] == "get":
                return type("R", (), {"returncode": 0, "stdout": empty, "stderr": ""})()
            raise AssertionError(f"unexpected oc_run args: {args}")

        oc_run.side_effect = _oc_run
        cleanup_dependency_operator_csvs()
        self.assertEqual(oc_run.call_count, len(_SUBSCRIPTION_NAMESPACES) + 1)


if __name__ == "__main__":
    unittest.main()
