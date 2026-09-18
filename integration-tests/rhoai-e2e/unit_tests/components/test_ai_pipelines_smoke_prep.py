#!/usr/bin/env python3
"""Unit tests for AI Pipelines pooled-cluster cleanup."""

from __future__ import annotations

import json
import unittest
from unittest.mock import patch

from components.ai_pipelines.smoke_prep import cleanup_ai_pipelines_smoke_leaks  # noqa: E402

class AiPipelinesSmokePrepTest(unittest.TestCase):
    def test_deletes_stale_dspa_test_namespaces(self) -> None:
        namespaces = {
            "items": [
                {"metadata": {"name": "dspa-test-abc123"}},
                {"metadata": {"name": "kube-system"}},
            ]
        }

        def _oc_run(args, **kwargs):
            cmd = list(args)
            if cmd[:3] == ["get", "namespace", "-o"]:
                return type("R", (), {"returncode": 0, "stdout": json.dumps(namespaces), "stderr": ""})()
            if cmd[:2] == ["get", "namespace"] and len(cmd) == 3:
                return type(
                    "R",
                    (),
                    {"returncode": 1, "stdout": "", "stderr": 'namespaces "dspa-test-abc123" NotFound'},
                )()

            return type("R", (), {"returncode": 0, "stdout": "", "stderr": ""})()

        with (
            patch("components.ai_pipelines.smoke_prep.oc_run", side_effect=_oc_run) as oc_run,
            patch("components.ai_pipelines.smoke_prep.time.sleep"),
        ):
            cleanup_ai_pipelines_smoke_leaks()
            delete_cmds = [list(c.args[0]) for c in oc_run.call_args_list if c.args[0][0] == "delete"]
            self.assertIn(
                ["delete", "namespace", "dspa-test-abc123", "--ignore-not-found", "--wait=false"],
                delete_cmds,
            )
