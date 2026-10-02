"""Drift test: every component in the smoke catalog must have a matching
test-<id> pipeline task and RUN_SMOKE_<id> result in the committed pipeline YAML.

This replaces the old gen_test_pipeline_tasks.py code generator: the pipeline
is now statically maintained, and this test catches catalog-vs-pipeline drift.
"""

from __future__ import annotations

import re
import unittest
from pathlib import Path

import yaml

from unit_tests._paths import RHOAI_E2E_ROOT

_RHOAI_E2E = RHOAI_E2E_ROOT
_CATALOG = _RHOAI_E2E / "config" / "rhoai-e2e-components-smoke.yaml"
_PIPELINE = _RHOAI_E2E / "tekton" / "pipelines" / "rhoai-e2e-pipeline.yaml"
_TEST_TASK_PREFIX = "test-"

def _component_id_from_pipeline_task(task_name: str) -> str | None:
    if not task_name.startswith(_TEST_TASK_PREFIX):
        return None
    slug = task_name.removeprefix(_TEST_TASK_PREFIX)
    if slug == "finalize":
        return None
    return slug.replace("-", "_")

def _resolve_run_smoke_order_from_pipeline(
    pipeline_doc: dict,
    *,
    catalog_component_ids: tuple[str, ...],
) -> tuple[str, ...]:
    """Serial test-* chain order for resolve-component-run-flags result display."""
    from suite.component_smoke_flag_refresh import catalog_ids_with_run_smoke_result

    tasks = pipeline_doc.get("spec", {}).get("tasks") or []
    by_name = {str(t["name"]): t for t in tasks if isinstance(t, dict) and t.get("name")}
    pred: dict[str, str | None] = {}
    for name, task in by_name.items():
        if _component_id_from_pipeline_task(name) is None:
            continue
        test_deps = [
            dep
            for dep in (task.get("runAfter") or [])
            if str(dep).startswith(_TEST_TASK_PREFIX)
            and _component_id_from_pipeline_task(str(dep)) is not None
        ]
        pred[name] = test_deps[0] if len(test_deps) == 1 else None
    heads = [name for name, dep in pred.items() if dep is None]
    if len(heads) != 1:
        raise ValueError(f"expected one test-* chain head, found {heads}")
    chain: list[str] = []
    current: str | None = heads[0]
    while current:
        chain.append(current)
        nxt = [name for name, dep in pred.items() if dep == current]
        if len(nxt) > 1:
            raise ValueError(f"branch after {current}: {nxt}")
        current = nxt[0] if nxt else None
    ordered = [
        cid for name in chain if (cid := _component_id_from_pipeline_task(name)) is not None
    ]
    expected = catalog_ids_with_run_smoke_result(catalog_component_ids)
    return tuple(cid for cid in ordered if cid in expected)

def _catalog_ids() -> list[str]:
    with open(_CATALOG, encoding="utf-8") as fh:
        cat = yaml.safe_load(fh)
    return [c["konflux"]["id"] for c in cat["components"]]

def _catalog_ids_requiring_run_smoke_result() -> list[str]:
    """Catalog ids with version-aware RUN_SMOKE_<id> on resolve-component-run-flags."""
    from suite.component_smoke_flag_refresh import catalog_ids_with_run_smoke_result

    return list(catalog_ids_with_run_smoke_result(tuple(_catalog_ids())))

def _catalog_ids_requiring_parse_run_smoke_result() -> list[str]:
    """Catalog ids with RUN_SMOKE_<id> on parse-pipeline-tests (pre-version-gate selection)."""
    from suite.component_smoke_flag_refresh import parse_pipeline_run_smoke_result_ids

    return list(parse_pipeline_run_smoke_result_ids(tuple(_catalog_ids())))

def _resolve_task_results() -> set[str]:
    pipeline = _pipeline_doc()
    resolve = next(
        t for t in pipeline["spec"]["tasks"] if t["name"] == "resolve-component-run-flags"
    )
    return {r["name"] for r in resolve["taskSpec"]["results"]}

def _pipeline_doc() -> dict:
    with open(_PIPELINE, encoding="utf-8") as fh:
        return yaml.safe_load(fh)

_TRIGGER_CONTEXT_SAMPLE = {
    "TRIGGER": "CLI direct (manual trigger)",
    "KONFLUX_EVENT": "Incoming — CLI direct PipelineRun",
    "SNAPSHOT": "n/a",
    "FBC": "rhoai-fbc-fragment-ocp-420 @ sha256:d9f54f26",
    "CLUSTER": "rh-nightly-pm",
    "RUN": "product=rhoai, tests=bvt,smoke",
    "TRIGGER_CMD": "python3 integration-tests/rhoai-e2e/rhoai_e2e.py --run-its rhoai-e2e-rh-nightly-pm-ocp420",
}


class PipelineCatalogConsistencyTest(unittest.TestCase):
    """Fail when a catalog component has no matching pipeline wiring."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.catalog_ids = _catalog_ids()
        pipeline = _pipeline_doc()
        spec = pipeline["spec"]

        cls.task_names: set[str] = {
            t["name"] for t in spec.get("tasks", [])
        }

        parse_task = next(
            t for t in spec["tasks"]
            if t["name"] == "parse-pipeline-tests"
        )
        cls.result_names: set[str] = {
            r["name"]
            for r in parse_task["taskSpec"]["results"]
        }

    def test_catalog_is_not_empty(self) -> None:
        self.assertGreater(len(self.catalog_ids), 0, "catalog has no components")

    def test_every_component_has_pipeline_task(self) -> None:
        missing = [
            cid for cid in self.catalog_ids
            if f"test-{cid.replace('_', '-')}" not in self.task_names
        ]
        self.assertEqual(
            missing, [],
            f"Components missing test-<id> pipeline task: {missing}. "
            "Add a task block to rhoai-e2e-pipeline.yaml.",
        )

    def test_parse_task_has_no_per_component_run_smoke_results(self) -> None:
        run_smoke_on_parse = [
            name for name in self.result_names if name.startswith("RUN_SMOKE_")
        ]
        self.assertEqual(
            run_smoke_on_parse,
            [],
            "RUN_SMOKE_<id> must not be on parse-pipeline-tests (use resolve-component-run-flags)",
        )

    def test_every_component_has_run_smoke_result_on_resolve_task(self) -> None:
        resolve_results = _resolve_task_results()
        missing = [
            cid for cid in _catalog_ids_requiring_run_smoke_result()
            if f"RUN_SMOKE_{cid}" not in resolve_results
        ]
        self.assertEqual(
            missing, [],
            f"Components missing RUN_SMOKE_<id> in resolve-component-run-flags results: {missing}.",
        )

    def test_resolve_run_flags_is_standalone_pipeline_task(self) -> None:
        self.assertIn(
            "resolve-component-run-flags",
            self.task_names,
            "RUN_SMOKE resolve must be a dedicated pipeline task (Tekton result budget)",
        )

    def test_bvt_runs_after_resolve_component_run_flags(self) -> None:
        pipeline = _pipeline_doc()
        bvt = next(t for t in pipeline["spec"]["tasks"] if t["name"] == "bvt-health-checks")
        run_after = bvt.get("runAfter") or []
        self.assertIn(
            "resolve-component-run-flags",
            run_after,
            "bvt-health-checks must run after resolve-component-run-flags (serial DAG)",
        )

    def test_prepare_task_results_fits_tekton_budget_without_run_smoke(self) -> None:
        from steps.tekton_util import (
            _TEKTON_TASK_RESULTS_BUDGET_BYTES,
            tekton_step_termination_payload_size,
        )

        pipeline = _pipeline_doc()
        prepare = next(
            t for t in pipeline["spec"]["tasks"] if t["name"] == "opendatahub-tests-prepare"
        )
        results = {r["name"]: "placeholder" for r in prepare["taskSpec"]["results"]}
        run_smoke_on_prepare = [
            name for name in results if name.startswith("RUN_SMOKE_")
        ]
        self.assertEqual(
            run_smoke_on_prepare,
            [],
            "RUN_SMOKE_<id> must not be on opendatahub-tests-prepare (use resolve task)",
        )
        size = tekton_step_termination_payload_size(results)
        self.assertLess(
            size,
            _TEKTON_TASK_RESULTS_BUDGET_BYTES,
            f"opendatahub-tests-prepare task results payload {size}B exceeds "
            f"{_TEKTON_TASK_RESULTS_BUDGET_BYTES}B Tekton task limit",
        )

    def test_resolve_task_results_fits_tekton_budget(self) -> None:
        from steps.tekton_util import (
            _TEKTON_TASK_RESULTS_BUDGET_BYTES,
            tekton_step_termination_payload_size,
        )

        pipeline = _pipeline_doc()
        resolve = next(
            t for t in pipeline["spec"]["tasks"] if t["name"] == "resolve-component-run-flags"
        )
        results = {r["name"]: "true" for r in resolve["taskSpec"]["results"]}
        size = tekton_step_termination_payload_size(results)
        self.assertLess(
            size,
            _TEKTON_TASK_RESULTS_BUDGET_BYTES,
            f"resolve-component-run-flags task results payload {size}B exceeds "
            f"{_TEKTON_TASK_RESULTS_BUDGET_BYTES}B Tekton task limit",
        )

    def test_resolve_run_smoke_results_match_pipeline_execution_order(self) -> None:
        from suite.component_catalog import load_components_smoke_catalog

        catalog = load_components_smoke_catalog(_CATALOG)
        pipeline = _pipeline_doc()
        expected_order = _resolve_run_smoke_order_from_pipeline(
            pipeline,
            catalog_component_ids=catalog.component_ids,
        )
        resolve = next(
            t for t in pipeline["spec"]["tasks"] if t["name"] == "resolve-component-run-flags"
        )
        actual_order = [
            r["name"].removeprefix("RUN_SMOKE_")
            for r in resolve["taskSpec"]["results"]
            if r["name"].startswith("RUN_SMOKE_")
        ]
        self.assertEqual(
            actual_order,
            list(expected_order),
            "resolve-component-run-flags RUN_SMOKE_* results must follow test-* DAG order",
        )

    def test_no_orphan_smoke_results(self) -> None:
        """Flag RUN_SMOKE_* results that no longer have a catalog entry."""
        catalog_set = set(self.catalog_ids)
        orphans = [
            name for name in sorted(self.result_names)
            if name.startswith("RUN_SMOKE_")
            and name.removeprefix("RUN_SMOKE_") not in catalog_set
        ]
        self.assertEqual(
            orphans, [],
            f"Orphan RUN_SMOKE_* results (no catalog entry): {orphans}. "
            "Remove from parse-pipeline-tests or add the component to the catalog.",
        )

    def test_parse_pipeline_tests_print_context_step_fits_tekton_budget(self) -> None:
        from steps.tekton_util import (
            _TEKTON_STEP_TERMINATION_BUDGET_BYTES,
            tekton_step_termination_payload_size,
        )

        size = tekton_step_termination_payload_size(_TRIGGER_CONTEXT_SAMPLE)
        self.assertLess(
            size,
            _TEKTON_STEP_TERMINATION_BUDGET_BYTES,
            f"parse-pipeline-tests print-run-context step payload {size}B exceeds "
            f"{_TEKTON_STEP_TERMINATION_BUDGET_BYTES}B Tekton step limit",
        )

    def test_parse_pipeline_tests_emit_step_fits_tekton_budget(self) -> None:
        from steps.tekton_util import (
            _TEKTON_STEP_TERMINATION_BUDGET_BYTES,
            tekton_step_termination_payload_size,
        )

        results = {
            "RUN_MINIMAL_DEPS": "true",
            "RUN_INSTALL_DEP_OPERATORS": "true",
            "RUN_SMOKE": "true",
            "RUN_BVT": "true",
            "RUN_TIER1": "false",
            "RUN_OPENDATAHUB_TESTS": "true",
            "RUN_COMPONENT_TESTS": "true",
            "RUN_COMPONENT_CLUSTER_PREP_IN_DEP_OPERATORS": "true",
            "RUN_BVT_PLACEHOLDER_ONLY": "false",
            "RUN_DISTRIBUTED_WORKLOADS_TESTS": "true",
        }
        size = tekton_step_termination_payload_size(results)
        self.assertLess(
            size,
            _TEKTON_STEP_TERMINATION_BUDGET_BYTES,
            f"parse-pipeline-tests eval step payload {size}B exceeds "
            f"{_TEKTON_STEP_TERMINATION_BUDGET_BYTES}B Tekton step limit",
        )

    def test_parse_pipeline_tests_config_emit_step_fits_tekton_budget(self) -> None:
        from suite.component_catalog import load_components_smoke_catalog
        from steps.tekton_util import (
            _TEKTON_STEP_TERMINATION_BUDGET_BYTES,
            tekton_step_termination_payload_size,
        )

        catalog = load_components_smoke_catalog(_CATALOG)
        components_csv = ",".join(catalog.component_ids)
        results = {
            "COMPONENTS_CSV": components_csv,
            "SETUP_DEPENDENCIES_ARGS": "-M",
            "SMOKE_AWS_SECRET": "shiftleft-envfile-model-serving",
        }
        size = tekton_step_termination_payload_size(results)
        self.assertLess(
            size,
            _TEKTON_STEP_TERMINATION_BUDGET_BYTES,
            f"parse-pipeline-tests emit-parse-artifacts step payload {size}B exceeds "
            f"{_TEKTON_STEP_TERMINATION_BUDGET_BYTES}B Tekton step limit",
        )

    def test_parse_pipeline_tests_task_results_fits_tekton_budget(self) -> None:
        from suite.component_catalog import load_components_smoke_catalog
        from steps.tekton_util import (
            _TEKTON_STEP_TERMINATION_BUDGET_BYTES,
            _TEKTON_TASK_RESULTS_BUDGET_BYTES,
            tekton_step_termination_payload_size,
        )

        catalog = load_components_smoke_catalog(_CATALOG)
        results = {
            "RUN_MINIMAL_DEPS": "true",
            "RUN_INSTALL_DEP_OPERATORS": "true",
            "RUN_SMOKE": "true",
            "RUN_BVT": "true",
            "RUN_TIER1": "false",
            "RUN_OPENDATAHUB_TESTS": "true",
            "RUN_COMPONENT_TESTS": "true",
            "RUN_COMPONENT_CLUSTER_PREP_IN_DEP_OPERATORS": "true",
            "RUN_BVT_PLACEHOLDER_ONLY": "false",
            "RUN_DISTRIBUTED_WORKLOADS_TESTS": "true",
            "COMPONENTS_CSV": ",".join(catalog.component_ids),
            "SETUP_DEPENDENCIES_ARGS": "-M",
            "SMOKE_AWS_SECRET": "shiftleft-envfile-model-serving",
            **_TRIGGER_CONTEXT_SAMPLE,
        }
        size = tekton_step_termination_payload_size(results)
        self.assertLess(
            size,
            _TEKTON_STEP_TERMINATION_BUDGET_BYTES,
            f"parse-pipeline-tests task results payload {size}B exceeds "
            f"{_TEKTON_STEP_TERMINATION_BUDGET_BYTES}B Tekton step limit",
        )
        self.assertLess(
            size,
            _TEKTON_TASK_RESULTS_BUDGET_BYTES,
            f"parse-pipeline-tests task results payload {size}B exceeds "
            f"{_TEKTON_TASK_RESULTS_BUDGET_BYTES}B Tekton task limit",
        )

    def test_ephemeral_path_uses_openshift_ci_provision(self) -> None:
        text = _PIPELINE.read_text(encoding="utf-8")
        self.assertIn("tasks/provision-ephemeral-cluster/0.1/", text)
        self.assertIn("hypershift-hostedcluster-workflow", text)
        self.assertNotIn("ephc-create-ephemeral-cluster-hypershift-aws", text)
        self.assertNotIn("task-ephc-provision-space.yaml", text)
        self.assertIn("provision-ephemeral-cluster", self.task_names)
        self.assertIn("resolve-oci-releases", self.task_names)
        self.assertIn("stage-ephemeral-kubeconfig", self.task_names)
        self.assertNotIn("install-ocp-cluster", self.task_names)
        self.assertIn("OCP_RELEASE_CHANNEL", text)
        self.assertIn("aws-konflux-prod", text)
        self.assertIn("secretName: vault-approle", text)
        self.assertIn("name: tenant-test-secrets", text)
        self.assertIn("hosted-mgmt2", text)
        self.assertIn("HYPERSHIFT_NODE_COUNT", text)
        self.assertIn("OCI_TASKS_REVISION", text)
        self.assertIn("9751a2028f2b3b88e4204d5c42de8eda4ebb466f", text)
        self.assertNotIn("provision-ephc-space", self.task_names)

if __name__ == "__main__":
    raise SystemExit(unittest.main())
