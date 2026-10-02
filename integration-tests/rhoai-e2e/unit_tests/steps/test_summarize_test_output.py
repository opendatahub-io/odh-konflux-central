#!/usr/bin/env python3
"""Unit tests for per-component TEST_OUTPUT via summarize_test_output."""

from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from unit_tests._paths import RHOAI_E2E_ROOT
from unittest.mock import patch

from steps.check_test_output_gate import check_test_output_json
from steps.summarize_test_output import build_test_output_payload

class EmitComponentTestOutputTest(unittest.TestCase):
    def test_build_payload_from_component_junit(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            plan = root / "plan.json"
            plan.write_text(
                json.dumps(
                    {
                        "components": [
                            {
                                "id": "workbenches",
                                "artifact_prefix": "workbenches-smoke",
                            }
                        ]
                    }
                ),
                encoding="utf-8",
            )
            junit = root / "workbenches-smoke.xml"
            junit.write_text(
                """<?xml version="1.0" encoding="UTF-8"?>
<testsuite name="workbenches" tests="2" failures="0" errors="0" skipped="0">
  <testcase classname="a" name="t1"/>
  <testcase classname="a" name="t2"/>
</testsuite>
""",
                encoding="utf-8",
            )
            payload, note = build_test_output_payload(
                root,
                component_id="workbenches",
                plan_path=plan,
            )
            self.assertEqual(payload["result"], "SUCCESS")
            self.assertEqual(payload["successes"], 0)
            self.assertEqual(payload["failures"], 0)
            self.assertEqual(payload["skipped"], 0)
            self.assertIn("Workbenches", note)
            self.assertIn("2 passed", note)

    def test_no_junit_emits_infrastructure_error(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            plan = root / "plan.json"
            plan.write_text(
                json.dumps({"components": [{"id": "model_runtime", "artifact_prefix": "model_runtime-smoke"}]}),
                encoding="utf-8",
            )
            payload, note = build_test_output_payload(
                root,
                component_id="model_runtime",
                plan_path=plan,
            )
            self.assertEqual(payload["result"], "FAILURE")
            self.assertEqual(payload["successes"], 0)
            self.assertEqual(payload["failures"], 1)
            self.assertEqual(payload["skipped"], 0)
            self.assertIn("infrastructure error", note)

    def test_dashboard_cypress_partial_smokeset_junit_is_summarized(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            plan = root / "plan.json"
            plan.write_text(
                json.dumps(
                    {
                        "components": [
                            {
                                "id": "dashboard_cypress",
                                "artifact_prefix": "dashboard-cypress-smoke",
                            }
                        ]
                    }
                ),
                encoding="utf-8",
            )
            report = root / "SmokeSet1" / "e2e"
            report.mkdir(parents=True)
            (report / "junit-report.xml").write_text(
                """<?xml version="1.0" encoding="UTF-8"?>
<testsuite name="SmokeSet1" tests="2" failures="0" errors="0" skipped="0">
  <testcase classname="a" name="t1"/>
  <testcase classname="a" name="t2"/>
</testsuite>
""",
                encoding="utf-8",
            )
            payload, note = build_test_output_payload(
                root,
                component_id="dashboard_cypress",
                plan_path=plan,
            )
            self.assertEqual(payload["result"], "SUCCESS")
            self.assertIn("2 passed", note)
            self.assertTrue((root / "dashboard-cypress-smoke.xml").is_file())

    def test_version_skip_junit_emits_failure_with_skipped(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            plan = root / "plan.json"
            plan.write_text(
                json.dumps({"components": [{"id": "mlflow", "artifact_prefix": "mlflow-smoke"}]}),
                encoding="utf-8",
            )
            (root / "mlflow-smoke.xml").write_text(
                """<?xml version="1.0" encoding="UTF-8"?>
<testsuite name="mlflow" tests="1" failures="0" errors="0" skipped="1">
  <testcase classname="mlflow" name="version_not_supported">
    <skipped message="minRhoai 3.4"/>
  </testcase>
</testsuite>
""",
                encoding="utf-8",
            )
            payload, note = build_test_output_payload(
                root,
                component_id="mlflow",
                plan_path=plan,
            )
            self.assertEqual(payload["result"], "FAILURE")
            self.assertEqual(payload["successes"], 0)
            self.assertEqual(payload["failures"], 1)
            self.assertEqual(payload["skipped"], 0)
            self.assertIn("0 passed, 0 failed, 1 skipped", note)

    def test_main_surfaces_failure_without_failing_step(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            results = root / "tekton-results"
            results.mkdir()
            plan = root / "plan.json"
            plan.write_text(
                json.dumps({"components": [{"id": "platform", "artifact_prefix": "platform-smoke"}]}),
                encoding="utf-8",
            )
            (root / "platform-smoke.xml").write_text(
                """<?xml version="1.0" encoding="UTF-8"?>
<testsuite name="platform" tests="1" failures="0" errors="0" skipped="1">
  <testcase classname="platform" name="version_not_supported">
    <skipped message="minRhoai 3.3"/>
  </testcase>
</testsuite>
""",
                encoding="utf-8",
            )
            out_path = results / "test_output.json"
            env = {
                "TEST_OUTPUT_PATH": str(out_path),
                "TEKTON_RESULTS_DIR": str(results),
                "ARTIFACT_BROWSER_BASE": "https://example.test",
                "PR_NAME": "pr-1",
                "ARTIFACTS_DIR": str(root),
                "COMPONENT_ID": "platform",
                "COMPONENT_TEST_PLAN_JSON": str(plan),
                "WRITE_ARTIFACTS_URL": "false",
                "SCRIPTS_REPO_ROOT": str(RHOAI_E2E_ROOT),
            }
            with patch.dict(os.environ, env, clear=False):
                from steps import summarize_test_output

                self.assertEqual(summarize_test_output.main(), 0)
            payload = json.loads(out_path.read_text(encoding="utf-8"))
            self.assertEqual(payload["result"], "FAILURE")

    def test_main_shows_stats_when_junit_zero_passed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            results = root / "tekton-results"
            results.mkdir()
            plan = root / "plan.json"
            plan.write_text(
                json.dumps({"components": [{"id": "platform", "artifact_prefix": "platform-smoke"}]}),
                encoding="utf-8",
            )
            (root / "platform-smoke.xml").write_text(
                """<?xml version="1.0" encoding="UTF-8"?>
<testsuite name="platform" tests="2" failures="2" errors="0" skipped="0">
  <testcase classname="a" name="t1"><failure message="x"/></testcase>
  <testcase classname="a" name="t2"><failure message="y"/></testcase>
</testsuite>
""",
                encoding="utf-8",
            )
            out_path = results / "test_output.json"
            env = {
                "TEST_OUTPUT_PATH": str(out_path),
                "TEKTON_RESULTS_DIR": str(results),
                "ARTIFACT_BROWSER_BASE": "https://example.test",
                "PR_NAME": "pr-1",
                "ARTIFACTS_DIR": str(root),
                "COMPONENT_ID": "platform",
                "COMPONENT_TEST_PLAN_JSON": str(plan),
                "WRITE_ARTIFACTS_URL": "false",
                "SCRIPTS_REPO_ROOT": str(RHOAI_E2E_ROOT),
            }
            with patch.dict(os.environ, env, clear=False):
                from steps import summarize_test_output

                self.assertEqual(summarize_test_output.main(), 0)
            payload = json.loads(out_path.read_text(encoding="utf-8"))
            self.assertEqual(payload["result"], "FAILURE")
            self.assertEqual(payload["successes"], 0)
            self.assertEqual(payload["failures"], 2)
            self.assertIn("0 passed, 2 failed", str(payload.get("note", "")))

    def test_unreadable_junit_emits_error(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            plan = root / "plan.json"
            plan.write_text(
                json.dumps({"components": [{"id": "model_runtime", "artifact_prefix": "model_runtime-smoke"}]}),
                encoding="utf-8",
            )
            (root / "model_runtime-smoke.xml").write_text("not xml", encoding="utf-8")
            payload, _note = build_test_output_payload(
                root,
                component_id="model_runtime",
                plan_path=plan,
            )
            self.assertEqual(payload["result"], "FAILURE")
            self.assertEqual(payload["successes"], 0)
            self.assertEqual(payload["failures"], 1)

    def test_mixed_failures_write_warning(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            plan = root / "plan.json"
            plan.write_text(
                json.dumps({"components": [{"id": "workbenches", "artifact_prefix": "workbenches-smoke"}]}),
                encoding="utf-8",
            )
            (root / "workbenches-smoke.xml").write_text(
                """<?xml version="1.0" encoding="UTF-8"?>
<testsuite name="workbenches" tests="3" failures="1" errors="0" skipped="0">
  <testcase classname="a" name="t1"/>
  <testcase classname="a" name="t2"/>
  <testcase classname="a" name="t3"><failure message="x"/></testcase>
</testsuite>
""",
                encoding="utf-8",
            )
            payload, _note = build_test_output_payload(
                root,
                component_id="workbenches",
                plan_path=plan,
            )
            self.assertEqual(payload["result"], "WARNING")
            self.assertEqual(payload["successes"], 0)
            self.assertEqual(payload["failures"], 1)
            self.assertIn("2 passed, 1 failed", str(payload.get("note", "")))

    def test_all_failed_write_failure(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            plan = root / "plan.json"
            plan.write_text(
                json.dumps({"components": [{"id": "model_registry", "artifact_prefix": "model_registry-smoke"}]}),
                encoding="utf-8",
            )
            (root / "model_registry-smoke.xml").write_text(
                """<?xml version="1.0" encoding="UTF-8"?>
<testsuite name="model_registry" tests="2" failures="2" errors="0" skipped="0">
  <testcase classname="a" name="t1"><failure message="x"/></testcase>
  <testcase classname="a" name="t2"><failure message="y"/></testcase>
</testsuite>
""",
                encoding="utf-8",
            )
            payload, _note = build_test_output_payload(
                root,
                component_id="model_registry",
                plan_path=plan,
            )
            self.assertEqual(payload["result"], "FAILURE")
            self.assertEqual(payload["successes"], 0)
            self.assertEqual(payload["failures"], 2)
            self.assertIn("0 passed, 2 failed", str(payload.get("note", "")))

    def test_component_dag_badge_counts_failures_only_not_skipped(self) -> None:
        """Skipped tests stay in note/suites; badge uses failures only (warnings zeroed)."""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            plan = root / "plan.json"
            plan.write_text(
                json.dumps(
                    {"components": [{"id": "platform", "artifact_prefix": "platform-smoke"}]}
                ),
                encoding="utf-8",
            )
            (root / "platform-smoke.xml").write_text(
                """<?xml version="1.0" encoding="UTF-8"?>
<testsuite name="platform" tests="60" failures="8" errors="0" skipped="50">
  <testcase classname="a" name="t1"/>
</testsuite>
""",
                encoding="utf-8",
            )
            payload, note = build_test_output_payload(
                root,
                component_id="platform",
                plan_path=plan,
            )
            self.assertEqual(payload["result"], "WARNING")
            self.assertEqual(payload["failures"], 8)
            self.assertEqual(payload["warnings"], 0)
            self.assertEqual(payload["successes"], 0)
            self.assertEqual(payload["skipped"], 0)
            self.assertIn("50 skipped", note)
            suites = payload.get("suites", [])
            self.assertEqual(len(suites), 1)
            self.assertEqual(suites[0].get("skipped"), 50)

    def test_passed_with_skips_emits_success_not_warning(self) -> None:
        """Per-component SUCCESS when executed tests pass; skips stay in note."""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            plan = root / "plan.json"
            plan.write_text(
                json.dumps({"components": [{"id": "spark_operator", "artifact_prefix": "spark_operator-smoke"}]}),
                encoding="utf-8",
            )
            (root / "spark_operator-smoke.xml").write_text(
                """<?xml version="1.0" encoding="UTF-8"?>
<testsuite name="spark_operator" tests="122" failures="0" errors="0" skipped="37">
  <testcase classname="a" name="t1"/>
</testsuite>
""",
                encoding="utf-8",
            )
            payload, note = build_test_output_payload(
                root,
                component_id="spark_operator",
                plan_path=plan,
            )
            self.assertEqual(payload["result"], "SUCCESS")
            self.assertEqual(payload["failures"], 0)
            self.assertIn("37 skipped", note)

    def test_component_preserves_failures_zeros_successes_for_dag_badge(self) -> None:
        """Konflux DAG badge uses TEST_OUTPUT.failures only; successes stay 0."""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            plan = root / "plan.json"
            plan.write_text(
                json.dumps(
                    {"components": [{"id": "trainer", "artifact_prefix": "trainer-smoke"}]}
                ),
                encoding="utf-8",
            )
            (root / "trainer-smoke.xml").write_text(
                """<?xml version="1.0" encoding="UTF-8"?>
<testsuite name="trainer" tests="5" failures="0" errors="0" skipped="0">
  <testcase classname="a" name="t1"/>
  <testcase classname="a" name="t2"/>
  <testcase classname="a" name="t3"/>
  <testcase classname="a" name="t4"/>
  <testcase classname="a" name="t5"/>
</testsuite>
""",
                encoding="utf-8",
            )
            payload, _note = build_test_output_payload(
                root,
                component_id="trainer",
                plan_path=plan,
            )
            self.assertEqual(payload["result"], "SUCCESS")
            self.assertEqual(payload["successes"], 0)
            self.assertEqual(payload["failures"], 0)
            self.assertEqual(payload["skipped"], 0)

    def test_bvt_all_skipped_emits_success(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "cluster-health.xml").write_text(
                """<?xml version="1.0" encoding="UTF-8"?>
<testsuite name="cluster" tests="1" failures="0" errors="0" skipped="1">
  <testcase classname="a" name="t1"><skipped message="n/a"/></testcase>
</testsuite>
""",
                encoding="utf-8",
            )
            (root / "operator-health.xml").write_text(
                """<?xml version="1.0" encoding="UTF-8"?>
<testsuite name="operator" tests="1" failures="0" errors="0" skipped="1">
  <testcase classname="a" name="t1"><skipped message="n/a"/></testcase>
</testsuite>
""",
                encoding="utf-8",
            )
            payload, _note = build_test_output_payload(root, note_prefix="BVT")
            self.assertEqual(payload["result"], "SUCCESS")
            self.assertEqual(payload["successes"], 0)
            self.assertEqual(payload["skipped"], 2)

    def test_bvt_partial_skip_emits_success_when_executed_passed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "cluster-health.xml").write_text(
                """<?xml version="1.0" encoding="UTF-8"?>
<testsuite name="cluster" tests="1" failures="0" errors="0" skipped="0">
  <testcase classname="a" name="t1"/>
</testsuite>
""",
                encoding="utf-8",
            )
            (root / "operator-health.xml").write_text(
                """<?xml version="1.0" encoding="UTF-8"?>
<testsuite name="operator" tests="1" failures="0" errors="0" skipped="1">
  <testcase classname="a" name="t1"><skipped message="n/a"/></testcase>
</testsuite>
""",
                encoding="utf-8",
            )
            payload, note = build_test_output_payload(root, note_prefix="BVT")
            self.assertEqual(payload["result"], "SUCCESS")
            self.assertEqual(payload["successes"], 1)
            self.assertEqual(payload["skipped"], 1)
            self.assertIn("50%", note)

    def test_bvt_cluster_precheck_failure_fails_gate(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "cluster-health.xml").write_text(
                """<?xml version="1.0" encoding="UTF-8"?>
<testsuite name="cluster" tests="1" failures="1" errors="0" skipped="0">
  <testcase classname="a" name="cluster_nodes_precheck_failed"><failure message="node-a"/></testcase>
</testsuite>
""",
                encoding="utf-8",
            )
            payload, _note = build_test_output_payload(root, note_prefix="BVT")
            self.assertEqual(payload["result"], "FAILURE")
            self.assertEqual(payload["successes"], 0)
            self.assertEqual(payload["failures"], 1)
            ec, _msg = check_test_output_json(payload, gate_label="BVT", strict=True)
            self.assertEqual(ec, 1)

    def test_bvt_operator_precheck_failure_fails_gate(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "cluster-health.xml").write_text(
                """<?xml version="1.0" encoding="UTF-8"?>
<testsuite name="cluster" tests="1" failures="0" errors="0" skipped="0">
  <testcase classname="a" name="t1"/>
</testsuite>
""",
                encoding="utf-8",
            )
            (root / "operator-health.xml").write_text(
                """<?xml version="1.0" encoding="UTF-8"?>
<testsuite name="operator" tests="1" failures="1" errors="0" skipped="0">
  <testcase classname="a" name="dsc_ready_precheck_failed"><failure message="DSC not Ready"/></testcase>
</testsuite>
""",
                encoding="utf-8",
            )
            payload, _note = build_test_output_payload(root, note_prefix="BVT")
            self.assertEqual(payload["result"], "WARNING")
            self.assertEqual(payload["successes"], 1)
            self.assertEqual(payload["failures"], 1)
            ec, _msg = check_test_output_json(payload, gate_label="BVT", strict=True)
            self.assertEqual(ec, 1)

    def test_component_aggregate_excludes_bvt_and_allows_tier_skips(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "results"
            root.mkdir()
            (root / "cluster-health.xml").write_text(
                """<?xml version="1.0" encoding="UTF-8"?>
<testsuite name="cluster" tests="1" failures="0" errors="0" skipped="0">
  <testcase classname="a" name="t1"/>
</testsuite>
""",
                encoding="utf-8",
            )
            (root / "distributed-workloads-smoke.xml").write_text(
                """<?xml version="1.0" encoding="UTF-8"?>
<testsuite name="kfto" tests="51" failures="0" errors="0" skipped="50">
  <testcase classname="a" name="TestKftoSmoke"/>
  <testcase classname="a" name="skipped"><skipped/></testcase>
</testsuite>
""",
                encoding="utf-8",
            )
            payload, note = build_test_output_payload(
                root.parent,
                note_prefix="Component",
                recursive=True,
            )
            self.assertEqual(payload["result"], "SUCCESS")
            self.assertEqual(payload["successes"], 1)
            self.assertEqual(payload["skipped"], 50)
            suite_ids = [str(s.get("id")) for s in payload.get("suites", [])]
            self.assertNotIn("cluster-health", suite_ids)
            self.assertIn("distributed-workloads-smoke", suite_ids)
            self.assertIn("1 passed", note)

    def test_shrink_drops_suites_when_payload_too_large(self) -> None:
        from steps.summarize_test_output import _shrink_test_output_payload, _TEKTON_TEST_OUTPUT_MAX_BYTES

        suites = [
            {
                "id": f"component-{i}",
                "name": f"Component {i}",
                "total": 10,
                "passed": 8,
                "failed": 2,
                "skipped": 0,
            }
            for i in range(80)
        ]
        payload: dict[str, object] = {
            "result": "FAILURE",
            "timestamp": "2026-06-21T00:00:00Z",
            "failures": 160,
            "warnings": 0,
            "successes": 640,
            "skipped": 0,
            "note": "component: 80% pass rate (640 passed, 160 failed, 0 skipped)",
            "suites": suites,
        }
        self.assertGreater(len(json.dumps(payload, separators=(",", ":")).encode()), _TEKTON_TEST_OUTPUT_MAX_BYTES)
        shrunk = _shrink_test_output_payload(payload)
        self.assertNotIn("suites", shrunk)
        self.assertLessEqual(len(json.dumps(shrunk, separators=(",", ":")).encode()), _TEKTON_TEST_OUTPUT_MAX_BYTES)
        self.assertIn("per-suite details omitted", str(shrunk.get("note", "")))

    def test_component_aggregate_applies_finalize_pass_rate_when_env_set(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            passed_cases = "\n".join(
                f'  <testcase classname="a" name="t{i}"/>' for i in range(995)
            )
            failed_cases = "\n".join(
                f'  <testcase classname="a" name="f{i}"><failure message="x"/></testcase>'
                for i in range(5)
            )
            (root / "aggregate-smoke.xml").write_text(
                f"""<?xml version="1.0" encoding="UTF-8"?>
<testsuite name="aggregate" tests="1000" failures="5" errors="0" skipped="0">
{passed_cases}
{failed_cases}
</testsuite>
""",
                encoding="utf-8",
            )
            payload_default, _ = build_test_output_payload(
                root,
                note_prefix="Component",
                recursive=True,
            )
            self.assertEqual(payload_default["result"], "WARNING")
            with patch.dict(os.environ, {"APPLY_TEST_FINALIZE_PASS_RATE": "true"}, clear=False):
                payload_finalize, _ = build_test_output_payload(
                    root,
                    note_prefix="Component",
                    recursive=True,
                )
            self.assertEqual(payload_finalize["result"], "SUCCESS")

if __name__ == "__main__":
    raise SystemExit(unittest.main())
