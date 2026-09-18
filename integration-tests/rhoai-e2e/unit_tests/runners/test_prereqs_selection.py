"""Component catalog selection for cluster prerequisites (no cluster)."""

from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from runners.selection import _selected_component_ids  # noqa: E402

class SelectedComponentIdsTest(unittest.TestCase):
    def test_from_components_csv(self) -> None:
        with patch.dict(os.environ, {"COMPONENTS_CSV": "workbenches,maas_billing", "COMPONENT_TEST_PLAN_JSON": ""}):
            self.assertEqual(_selected_component_ids(), {"workbenches", "maas_billing"})

    def test_from_plan_json(self) -> None:
        with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as tf:
            json.dump({"components": [{"id": "model_server"}, {"id": "kuberay"}]}, tf)
            plan_path = tf.name
        try:
            with patch.dict(
                os.environ,
                {"COMPONENT_TEST_PLAN_JSON": plan_path, "COMPONENTS_CSV": "ignored"},
            ):
                self.assertEqual(_selected_component_ids(), {"model_server", "kuberay"})
        finally:
            Path(plan_path).unlink(missing_ok=True)

    def test_from_plan_json_skips_version_gated_ids(self) -> None:
        with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as tf:
            json.dump(
                {
                    "operator_version": "3.5.0-ea.2",
                    "components": [
                        {"id": "ogx"},
                        {
                            "id": "llama_stack",
                            "version_skip_reason": "maxRhoai=3.4",
                        },
                    ],
                },
                tf,
            )
            plan_path = tf.name
        try:
            with patch.dict(
                os.environ,
                {"COMPONENT_TEST_PLAN_JSON": plan_path, "COMPONENTS_CSV": "ignored,llama_stack"},
            ):
                self.assertEqual(_selected_component_ids(), {"ogx"})
        finally:
            Path(plan_path).unlink(missing_ok=True)

    def test_empty_csv(self) -> None:
        with patch.dict(os.environ, {"COMPONENTS_CSV": "", "COMPONENT_TEST_PLAN_JSON": ""}, clear=False):
            self.assertEqual(_selected_component_ids(), set())
