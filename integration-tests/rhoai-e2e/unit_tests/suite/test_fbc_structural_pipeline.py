"""validate-fbc-structural sits between extract-fbcf-image and cluster tasks."""

from __future__ import annotations

import unittest
from typing import Any

import yaml

from unit_tests._paths import RHOAI_E2E_ROOT

_PIPELINE = RHOAI_E2E_ROOT / "tekton" / "pipelines" / "rhoai-e2e-pipeline.yaml"


def _task(spec: dict[str, Any], name: str) -> dict[str, Any]:
    for section in ("tasks", "finally"):
        for task in spec.get(section) or []:
            if isinstance(task, dict) and task.get("name") == name:
                return task
    raise AssertionError(f"missing pipeline task {name}")


def _assert_skips_structural_only(task: dict[str, Any]) -> None:
    when = [w for w in (task.get("when") or []) if isinstance(w, dict)]
    structural = [
        w
        for w in when
        if w.get("input") == "$(params.STRUCTURAL_TESTS_ONLY)"
        and w.get("operator") == "notin"
        and w.get("values") == ["true"]
    ]
    assert structural, f"{task.get('name')} missing STRUCTURAL_TESTS_ONLY skip when"


class FbcStructuralPipelineTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        with open(_PIPELINE, encoding="utf-8") as fh:
            cls.spec = yaml.safe_load(fh).get("spec") or {}

    def test_validate_runs_after_extract(self) -> None:
        task = _task(self.spec, "validate-fbc-structural")
        self.assertIn("extract-fbcf-image", task.get("runAfter") or [])

    def test_cluster_tasks_wait_on_structural(self) -> None:
        oci = _task(self.spec, "resolve-oci-releases")
        ext = _task(self.spec, "external-cluster-ready")
        self.assertIn("validate-fbc-structural", oci.get("runAfter") or [])
        self.assertIn("validate-fbc-structural", ext.get("runAfter") or [])

    def test_opt_in_when_fbc_structural_true(self) -> None:
        task = _task(self.spec, "validate-fbc-structural")
        inputs = {(w.get("input") or ""): w for w in task.get("when") or [] if isinstance(w, dict)}
        flag = inputs.get("$(params.FBC_STRUCTURAL)")
        self.assertIsNotNone(flag)
        self.assertEqual(flag.get("operator"), "in")
        self.assertEqual(flag.get("values"), ["true"])

    def test_failure_does_not_block_cluster_tasks(self) -> None:
        task = _task(self.spec, "validate-fbc-structural")
        self.assertEqual(task.get("onError"), "continue")

    def test_passes_git_sslverify_false_for_rhoai_releases(self) -> None:
        task = _task(self.spec, "validate-fbc-structural")
        params = {p.get("name"): p.get("value") for p in task.get("params") or []}
        self.assertEqual(params.get("OLMINSTALL_ALLOW_GIT_SSLVERIFY_FALSE"), "true")

    def test_structural_only_skips_cluster_verify_and_diagnostics(self) -> None:
        _assert_skips_structural_only(_task(self.spec, "verify-operator-ready"))
        _assert_skips_structural_only(_task(self.spec, "collect-diagnostics"))
        _assert_skips_structural_only(_task(self.spec, "external-cluster-ready"))

    def test_test_finalize_runs_after_structural_for_finalize_flag(self) -> None:
        task = _task(self.spec, "test-finalize")
        self.assertIn("validate-fbc-structural", task.get("runAfter") or [])
        inputs = {(w.get("input") or ""): w for w in task.get("when") or [] if isinstance(w, dict)}
        flag = inputs.get("$(tasks.parse-pipeline-tests.results.RUN_TEST_FINALIZE)")
        self.assertIsNotNone(flag)
        self.assertEqual(flag.get("values"), ["true"])
