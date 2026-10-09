#!/usr/bin/env python3
"""Unit tests for Model Registry pooled-cluster cleanup."""

from __future__ import annotations

import json
import unittest
from unittest.mock import patch

from components.model_registry.cluster_cleanup import cleanup_model_registry_smoke_leaks  # noqa: E402

class ModelRegistryClusterCleanupTest(unittest.TestCase):
    def test_deletes_stale_db_secrets(self) -> None:
        secrets = {
            "items": [
                {"metadata": {"name": "db-model-registry0"}},
                {"metadata": {"name": "model-catalog-db"}},
            ]
        }
        pvcs = {
            "items": [
                {"metadata": {"name": "db-model-registry0"}},
                {"metadata": {"name": "model-catalog-pvc"}},
            ]
        }
        services = {
            "items": [
                {"metadata": {"name": "db-model-registry0"}},
                {"metadata": {"name": "model-catalog"}},
            ]
        }

        def _oc_run(args, **kwargs):
            cmd = list(args)
            if cmd[:3] == ["get", "secret", "-n"]:
                return type("R", (), {"returncode": 0, "stdout": json.dumps(secrets), "stderr": ""})()
            if cmd[:3] == ["get", "pvc", "-n"]:
                return type("R", (), {"returncode": 0, "stdout": json.dumps(pvcs), "stderr": ""})()
            if cmd[:3] == ["get", "service", "-n"]:
                return type("R", (), {"returncode": 0, "stdout": json.dumps(services), "stderr": ""})()
            if cmd[:3] == ["get", "modelregistry", "-n"]:
                return type("R", (), {"returncode": 1, "stdout": "", "stderr": ""})()
            if cmd[:3] == ["get", "modelregistryservice", "-n"]:
                return type("R", (), {"returncode": 1, "stdout": "", "stderr": ""})()
            if cmd[:3] == ["get", "deploy", "-n"]:
                return type("R", (), {"returncode": 0, "stdout": "model-catalog\ndb-model-registry0\n", "stderr": ""})()
            return type("R", (), {"returncode": 0, "stdout": "", "stderr": ""})()

        with patch("components.model_registry.cluster_cleanup.oc_run", side_effect=_oc_run) as oc_run:
            cleanup_model_registry_smoke_leaks()
            delete_cmds = [list(c.args[0]) for c in oc_run.call_args_list if c.args[0][0] == "delete"]
            self.assertIn(
                ["delete", "secret", "db-model-registry0", "-n", "rhoai-model-registries", "--ignore-not-found"],
                delete_cmds,
            )
            self.assertIn(
                ["delete", "pvc", "db-model-registry0", "-n", "rhoai-model-registries", "--ignore-not-found"],
                delete_cmds,
            )
            self.assertIn(
                ["delete", "service", "db-model-registry0", "-n", "rhoai-model-registries", "--ignore-not-found"],
                delete_cmds,
            )
            self.assertIn(
                ["delete", "deploy", "db-model-registry0", "-n", "rhoai-model-registries", "--ignore-not-found"],
                delete_cmds,
            )

if __name__ == "__main__":
    raise SystemExit(unittest.main())
