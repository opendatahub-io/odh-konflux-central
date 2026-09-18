"""Unit tests for bulk leaked tenant namespace cleanup."""

from __future__ import annotations

import json
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

    @patch("install.leaked_tenant_namespace_cleanup.unblock_terminating_namespace")
    @patch("install.leaked_tenant_namespace_cleanup.oc_run")
    def test_bulk_deletes_matched_namespaces(self, oc_run, unblock) -> None:
        ns_json = {
            "items": [
                {"metadata": {"name": "ai-tenant-e2e-aigw-abc12345"}},
                {"metadata": {"name": "test-kueue-managed-xyz"}},
                {"metadata": {"name": "default"}},
            ]
        }

        def _oc_run(args, **kwargs):
            cmd = " ".join(args)
            if args[:3] == ["get", "namespace", "-o"]:
                return type("R", (), {"returncode": 0, "stdout": json.dumps(ns_json), "stderr": ""})()
            if args[0] == "delete":
                self.assertEqual(args[1], "namespace")
                self.assertEqual(
                    set(args[2:-2]),
                    {"ai-tenant-e2e-aigw-abc12345", "test-kueue-managed-xyz"},
                )
                self.assertEqual(args[-2:], ["--ignore-not-found", "--wait=false"])
                return type("R", (), {"returncode": 0, "stdout": "", "stderr": ""})()
            if "jsonpath" in cmd:
                return type("R", (), {"returncode": 0, "stdout": "Terminating", "stderr": ""})()
            raise AssertionError(f"unexpected oc_run args: {args}")

        oc_run.side_effect = _oc_run

        cleanup_leaked_tenant_namespaces()

        unblock.assert_any_call("ai-tenant-e2e-aigw-abc12345")
        unblock.assert_any_call("test-kueue-managed-xyz")

    @patch("install.leaked_tenant_namespace_cleanup.oc_run")
    def test_skips_when_disabled(self, oc_run) -> None:
        with patch.dict("os.environ", {"CLEANUP_LEAKED_TENANT_NS": "0"}, clear=False):
            cleanup_leaked_tenant_namespaces()
        oc_run.assert_not_called()

    @patch("install.leaked_tenant_namespace_cleanup.oc_run")
    def test_no_matches_is_noop(self, oc_run) -> None:
        oc_run.return_value = type(
            "R",
            (),
            {"returncode": 0, "stdout": json.dumps({"items": []}), "stderr": ""},
        )()
        cleanup_leaked_tenant_namespaces()
        self.assertEqual(oc_run.call_count, 1)


if __name__ == "__main__":
    unittest.main()
