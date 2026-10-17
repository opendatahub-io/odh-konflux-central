"""Unit tests for steps.finalize_component_task_exit."""

from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from steps import finalize_component_task_exit

class FinalizeComponentTaskExitTest(unittest.TestCase):
    def _env(self, artifacts: Path, component_id: str = "workbenches") -> None:
        plan = {
            "components": [
                {
                    "id": component_id,
                    "runner": "external-pytest",
                    "gates": ["smoke"],
                }
            ]
        }
        plan_path = artifacts / "component-test-plan.json"
        plan_path.write_text(json.dumps(plan), encoding="utf-8")
        os.environ["ARTIFACTS_DIR"] = str(artifacts)
        os.environ["COMPONENT_ID"] = component_id
        os.environ["COMPONENT_TEST_PLAN_JSON"] = str(plan_path)
        self.addCleanup(lambda: os.environ.pop("ARTIFACTS_DIR", None))
        self.addCleanup(lambda: os.environ.pop("COMPONENT_ID", None))
        self.addCleanup(lambda: os.environ.pop("COMPONENT_TEST_PLAN_JSON", None))

    def test_attributed_drift_fails_tekton(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            artifacts = Path(tmp)
            (artifacts / "dashboard_cypress.component-test.exit").write_text("0", encoding="ascii")
            self._env(artifacts, "dashboard_cypress")
            drifts = ["dashboard: Managed\u2192Removed"]
            with mock.patch(
                "steps.finalize_component_task_exit.finalize_component_dsc_hygiene",
                return_value=drifts,
            ):
                ec = finalize_component_task_exit.main()
            self.assertEqual(ec, 1)
            self.assertEqual(
                (artifacts / "dashboard_cypress.component-test.exit").read_text(encoding="ascii"), "1"
            )

    def test_published_test_output_zero_success_fails_tekton_for_red_dag(self) -> None:
        """0 successes must fail finalize so Konflux DAG is red (not yellow Succeeded)."""
        with tempfile.TemporaryDirectory() as tmp:
            artifacts = Path(tmp)
            (artifacts / "ogx.component-test.exit").write_text("1", encoding="ascii")
            (artifacts / "ogx-smoke.xml").write_text(
                '<?xml version="1.0"?><testsuites tests="4" failures="4" errors="0" skipped="0"/>',
                encoding="utf-8",
            )
            self._env(artifacts, "ogx")
            plan = json.loads(
                (artifacts / "component-test-plan.json").read_text(encoding="utf-8")
            )
            plan["components"][0]["artifact_prefix"] = "ogx-smoke"
            (artifacts / "component-test-plan.json").write_text(
                json.dumps(plan), encoding="utf-8"
            )
            test_output = artifacts / "TEST_OUTPUT.json"
            test_output.write_text(
                json.dumps(
                    {
                        "result": "FAILURE",
                        "timestamp": "2026-07-07T19:00:00Z",
                        "failures": 4,
                        "warnings": 0,
                        "successes": 0,
                        "note": "OGX: 0 passed, 4 failed, 0 skipped",
                    }
                ),
                encoding="utf-8",
            )
            os.environ["TEST_OUTPUT_PATH"] = str(test_output)
            self.addCleanup(lambda: os.environ.pop("TEST_OUTPUT_PATH", None))
            with mock.patch(
                "steps.finalize_component_task_exit.finalize_component_dsc_hygiene",
                return_value=[],
            ):
                ec = finalize_component_task_exit.main()
            self.assertEqual(ec, 1)
            self.assertEqual(
                (artifacts / "ogx.component-test.exit").read_text(encoding="ascii"), "1"
            )

    def test_published_test_output_partial_pass_keeps_tekton_exit_zero(self) -> None:
        """Some passed + some failed → yellow WARNING; TaskRun stays Succeeded."""
        with tempfile.TemporaryDirectory() as tmp:
            artifacts = Path(tmp)
            (artifacts / "ogx.component-test.exit").write_text("1", encoding="ascii")
            (artifacts / "ogx-smoke.xml").write_text(
                '<?xml version="1.0"?><testsuites tests="4" failures="1" errors="0" skipped="0">'
                "<testcase classname='a' name='t1'/><testcase classname='a' name='t2'/>"
                "<testcase classname='a' name='t3'/><testcase classname='a' name='t4'>"
                "<failure message='x'/></testcase></testsuites>",
                encoding="utf-8",
            )
            self._env(artifacts, "ogx")
            plan = json.loads(
                (artifacts / "component-test-plan.json").read_text(encoding="utf-8")
            )
            plan["components"][0]["artifact_prefix"] = "ogx-smoke"
            (artifacts / "component-test-plan.json").write_text(
                json.dumps(plan), encoding="utf-8"
            )
            test_output = artifacts / "TEST_OUTPUT.json"
            test_output.write_text(
                json.dumps(
                    {
                        "result": "WARNING",
                        "timestamp": "2026-07-07T19:00:00Z",
                        "failures": 1,
                        "warnings": 0,
                        "successes": 3,
                        "note": "OGX: 3 passed, 1 failed, 0 skipped",
                    }
                ),
                encoding="utf-8",
            )
            os.environ["TEST_OUTPUT_PATH"] = str(test_output)
            self.addCleanup(lambda: os.environ.pop("TEST_OUTPUT_PATH", None))
            with mock.patch(
                "steps.finalize_component_task_exit.finalize_component_dsc_hygiene",
                return_value=[],
            ):
                ec = finalize_component_task_exit.main()
            self.assertEqual(ec, 0)

    def test_no_drift_preserves_pass_exit(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            artifacts = Path(tmp)
            (artifacts / "workbenches.component-test.exit").write_text("0", encoding="ascii")
            self._env(artifacts)
            with mock.patch(
                "steps.finalize_component_task_exit.finalize_component_dsc_hygiene",
                return_value=[],
            ):
                ec = finalize_component_task_exit.main()
            self.assertEqual(ec, 0)

    def test_stale_shared_exit_file_ignored_when_test_output_failure(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            artifacts = Path(tmp)
            (artifacts / "component-test.exit").write_text("0", encoding="ascii")
            self._env(artifacts, "model_server")
            test_output = artifacts / "TEST_OUTPUT.json"
            test_output.write_text(
                json.dumps(
                    {
                        "result": "FAILURE",
                        "failures": 1,
                        "successes": 0,
                        "note": "model_server: infrastructure error",
                    }
                ),
                encoding="utf-8",
            )
            os.environ["TEST_OUTPUT_PATH"] = str(test_output)
            self.addCleanup(lambda: os.environ.pop("TEST_OUTPUT_PATH", None))
            with mock.patch(
                "steps.finalize_component_task_exit.finalize_component_dsc_hygiene",
                return_value=[],
            ):
                ec = finalize_component_task_exit.main()
            self.assertEqual(ec, 1)

