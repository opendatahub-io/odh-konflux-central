#!/usr/bin/env python3
"""Unit tests for model_runtime pooled-cluster cleanup."""

from __future__ import annotations

import json
import unittest
from unittest.mock import patch

from components.model_runtime.smoke_prep import cleanup_model_runtime_smoke_leaks


class ModelRuntimeSmokePrepTest(unittest.TestCase):
    def test_deletes_stale_vllm_namespaces(self) -> None:
        namespaces = {
            "items": [
                {"metadata": {"name": "opt-125m-standard-cpu"}},
                {"metadata": {"name": "opt-125m-probes"}},
                {"metadata": {"name": "onnx-standard-rest"}},
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
                    {
                        "returncode": 1,
                        "stdout": "",
                        "stderr": f'namespaces "{cmd[2]}" NotFound',
                    },
                )()
            return type("R", (), {"returncode": 0, "stdout": "", "stderr": ""})()

        with patch("components.model_runtime.smoke_prep.oc_run", side_effect=_oc_run), patch(
            "components.model_runtime.smoke_prep.time.sleep",
        ):
            cleanup_model_runtime_smoke_leaks()
            delete_cmds = [c for c in calls if c and c[0] == "delete"]
            deleted = {c[2] for c in delete_cmds}
            self.assertEqual(
                deleted,
                {"opt-125m-standard-cpu", "opt-125m-probes", "onnx-standard-rest"},
            )
            self.assertNotIn("default", deleted)
