#!/usr/bin/env python3
"""Unit tests for KubeRay pooled-cluster cleanup."""

from __future__ import annotations

import json
import unittest
from unittest.mock import patch

from components.kuberay.smoke_prep import cleanup_kuberay_smoke_leaks  # noqa: E402

class KuberaySmokePrepTest(unittest.TestCase):
    def test_deletes_stale_test_ns_namespaces(self) -> None:
        namespaces = {
            "items": [
                {"metadata": {"name": "test-ns-abc123"}},
                {"metadata": {"name": "default"}},
            ]
        }
        calls: list[list[str]] = []

        def _oc_run(args, **kwargs):
            cmd = list(args)
            calls.append(cmd)
            if cmd[:3] == ["get", "namespace", "-o"]:
                return type("R", (), {"returncode": 0, "stdout": json.dumps(namespaces), "stderr": ""})()
            if cmd[:2] == ["get", "namespace"] and len(cmd) == 3:
                return type(
                    "R",
                    (),
                    {"returncode": 1, "stdout": "", "stderr": 'namespaces "test-ns-abc123" NotFound'},
                )()
            return type("R", (), {"returncode": 0, "stdout": "", "stderr": ""})()

        with patch("components.kuberay.smoke_prep.oc_run", side_effect=_oc_run), patch(
            "components.kuberay.smoke_prep.time.sleep",
        ):
            cleanup_kuberay_smoke_leaks()
            delete_cmds = [c for c in calls if c and c[0] == "delete"]
            self.assertEqual(
                delete_cmds[0],
                ["delete", "namespace", "test-ns-abc123", "--ignore-not-found", "--wait=false"],
            )
