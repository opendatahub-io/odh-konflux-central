"""Unit tests for bulk leaked component test namespace cleanup."""

from __future__ import annotations

import json
import unittest
from unittest.mock import patch

from install.e2e_leaked_namespace_patterns import matches_leaked_component_test_namespace
from install.leaked_component_namespace_cleanup import cleanup_leaked_component_test_namespaces


class LeakedComponentNamespaceCleanupTest(unittest.TestCase):
    def test_matches_known_component_patterns(self) -> None:
        self.assertTrue(matches_leaked_component_test_namespace("openldap"))
        self.assertTrue(matches_leaked_component_test_namespace("ovms-smoke"))
        self.assertTrue(matches_leaked_component_test_namespace("dspa-test"))
        self.assertTrue(matches_leaked_component_test_namespace("dspa-test-tscpmwt9"))
        self.assertTrue(matches_leaked_component_test_namespace("llmd-local-model-cache"))
        self.assertTrue(matches_leaked_component_test_namespace("test-kserve-raw-token-authentication"))
        self.assertTrue(matches_leaked_component_test_namespace("trainer-v2-test-502409"))
        self.assertFalse(matches_leaked_component_test_namespace("opendatahub-ogx-system"))
        self.assertFalse(matches_leaked_component_test_namespace("oidc"))
        self.assertFalse(matches_leaked_component_test_namespace("redhat-ods-applications"))
        self.assertFalse(matches_leaked_component_test_namespace("mohd"))

    @patch("install.e2e_namespace_bulk_delete.unblock_terminating_namespace")
    @patch("install.e2e_namespace_bulk_delete.oc_run")
    @patch("install.e2e_namespace_bulk_delete.list_cluster_namespace_names")
    def test_bulk_deletes_matched_namespaces(self, list_names, oc_run, unblock) -> None:
        list_names.return_value = [
            "default",
            "openldap",
            "ovms-smoke",
            "opendatahub-ogx-system",
        ]

        def _oc_run(args, **kwargs):
            if args[0] == "delete":
                self.assertEqual(args[1], "namespace")
                self.assertEqual(set(args[2:-2]), {"openldap", "ovms-smoke"})
                self.assertEqual(args[-2:], ["--ignore-not-found", "--wait=false"])
                return type("R", (), {"returncode": 0, "stdout": "", "stderr": ""})()
            if "jsonpath" in " ".join(args):
                return type("R", (), {"returncode": 0, "stdout": "Terminating", "stderr": ""})()
            raise AssertionError(f"unexpected oc_run args: {args}")

        oc_run.side_effect = _oc_run

        cleanup_leaked_component_test_namespaces()

        unblock.assert_any_call("openldap")

    @patch("install.e2e_namespace_bulk_delete.list_cluster_namespace_names")
    def test_skips_when_disabled(self, list_names) -> None:
        with patch.dict("os.environ", {"CLEANUP_LEAKED_COMPONENT_NS": "0"}, clear=False):
            cleanup_leaked_component_test_namespaces()
        list_names.assert_not_called()


if __name__ == "__main__":
    unittest.main()
