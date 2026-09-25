"""Unit tests for bulk leaked tenant namespace cleanup."""

from __future__ import annotations

import unittest
from unittest.mock import patch

from install.leaked_tenant_namespace_cleanup import (
    _matches_leaked_tenant_namespace,
    cleanup_leaked_tenant_namespaces,
)


class LeakedTenantNamespaceCleanupTest(unittest.TestCase):
    def test_matches_known_leak_patterns(self) -> None:
        self.assertTrue(_matches_leaked_tenant_namespace("ai-tenant-e2e-aigw-0dc80348"))
        self.assertTrue(_matches_leaked_tenant_namespace("test-kueue-managed-da3p097fg5dc7bcjgur0"))
        self.assertFalse(_matches_leaked_tenant_namespace("ai-tenants"))
        self.assertFalse(_matches_leaked_tenant_namespace("redhat-ods-applications"))

    @patch("install.e2e_namespace_bulk_delete.unblock_terminating_namespace")
    @patch("install.e2e_namespace_bulk_delete.oc_run")
    @patch("install.e2e_namespace_bulk_delete.list_cluster_namespace_names")
    def test_bulk_deletes_matched_namespaces(self, list_names, oc_run, unblock) -> None:
        list_names.return_value = [
            "ai-tenant-e2e-aigw-abc12345",
            "test-kueue-managed-xyz",
            "default",
        ]

        def _oc_run(args, **kwargs):
            if args[0] == "delete":
                self.assertEqual(args[1], "namespace")
                self.assertEqual(
                    set(args[2:-2]),
                    {"ai-tenant-e2e-aigw-abc12345", "test-kueue-managed-xyz"},
                )
                self.assertEqual(args[-2:], ["--ignore-not-found", "--wait=false"])
                return type("R", (), {"returncode": 0, "stdout": "", "stderr": ""})()
            if "jsonpath" in " ".join(args):
                return type("R", (), {"returncode": 0, "stdout": "Terminating", "stderr": ""})()
            raise AssertionError(f"unexpected oc_run args: {args}")

        oc_run.side_effect = _oc_run

        cleanup_leaked_tenant_namespaces()

        unblock.assert_any_call("ai-tenant-e2e-aigw-abc12345")
        unblock.assert_any_call("test-kueue-managed-xyz")

    @patch("install.e2e_namespace_bulk_delete.list_cluster_namespace_names")
    def test_skips_when_disabled(self, list_names) -> None:
        with patch.dict("os.environ", {"CLEANUP_LEAKED_TENANT_NS": "0"}, clear=False):
            cleanup_leaked_tenant_namespaces()
        list_names.assert_not_called()

    @patch("install.e2e_namespace_bulk_delete.list_cluster_namespace_names")
    def test_no_matches_is_noop(self, list_names) -> None:
        list_names.return_value = ["default"]
        cleanup_leaked_tenant_namespaces()


if __name__ == "__main__":
    unittest.main()
