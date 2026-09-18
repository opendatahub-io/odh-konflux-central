"""Tests for catalog-driven Konflux component task descriptions."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

import yaml

from unit_tests._paths import RHOAI_E2E_ROOT

from suite.component_catalog import default_components_smoke_config_path, load_components_smoke_catalog
from suite.component_task_description import (
    build_component_task_description,
    component_tekton_task_base,
    extract_pipeline_task_description_from_block,
    format_pipeline_task_description,
    generated_task_path_in_repo,
    pipeline_task_name,
)
from suite.generate_component_tekton_tasks import (
    _replace_task_description,
    _replace_task_path_in_repo,
    generate,
)

class ComponentTaskDescriptionTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.catalog = load_components_smoke_catalog(default_components_smoke_config_path())
        cls.pipeline_text = (
            RHOAI_E2E_ROOT / "tekton" / "pipelines" / "rhoai-e2e-pipeline.yaml"
        ).read_text(encoding="utf-8")

    def test_workbenches_description_is_operator_facing(self) -> None:
        comp = self.catalog.components["workbenches"]
        desc = build_component_task_description(comp)
        self.assertIn("opendatahub-tests", desc)
        self.assertIn("Test framework:", desc)
        self.assertIn("Command: pytest tests/workbenches", desc)
        self.assertNotIn("Jenkins", desc)
        self.assertNotIn("TestOps", desc)
        self.assertNotIn("main.yaml", desc)
        self.assertNotIn("shiftLeftEnvSecret", desc)
        self.assertNotIn("envfile-", desc)
        self.assertNotIn("Artifact prefix:", desc)
        self.assertNotIn("OCI upload happens in publish-results", desc)
        self.assertNotIn("Min pass rate for Tekton success: 90%", desc)

    def test_dashboard_cypress_uses_default_pass_rate(self) -> None:
        comp = self.catalog.components["dashboard_cypress"]
        desc = build_component_task_description(comp)
        self.assertNotIn("Min pass rate for Tekton success:", desc)

    def test_golang_runner_description_mentions_framework(self) -> None:
        comp = self.catalog.components["ai_pipelines"]
        desc = build_component_task_description(comp)
        self.assertIn("golang ginkgo", desc)
        self.assertIn("Smoke command:", desc)
        self.assertIn("test-run.sh", desc)
        self.assertEqual(component_tekton_task_base(comp), "task-component-golang.yaml")

    def test_model_server_omits_secret_names(self) -> None:
        comp = self.catalog.components["model_server"]
        desc = build_component_task_description(comp)
        self.assertIn("tenant credentials", desc)
        self.assertNotIn("shiftLeftEnvSecret", desc)
        self.assertNotIn("RHOAIENG", desc)
        self.assertNotIn("ods-ci", desc)

    def test_generated_task_files_match_catalog(self) -> None:
        generated = RHOAI_E2E_ROOT / "tekton" / "tasks" / "generated"
        for comp_id, comp in self.catalog.components.items():
            path = generated / f"component-{comp_id}.yaml"
            self.assertTrue(path.is_file(), f"missing generated task for {comp_id}")
            doc = yaml.safe_load(path.read_text(encoding="utf-8"))
            expected = build_component_task_description(comp)
            self.assertEqual(doc["spec"]["description"].strip(), expected.strip())

    def test_pipeline_path_in_repo_points_at_generated_task(self) -> None:
        for comp_id in self.catalog.component_ids:
            task_name = pipeline_task_name(comp_id)
            expected = generated_task_path_in_repo(comp_id)
            block_start = self.pipeline_text.find(f"    - name: {task_name}\n")
            self.assertGreaterEqual(block_start, 0, task_name)
            next_task = self.pipeline_text.find("\n    - name:", block_start + 1)
            block = self.pipeline_text[block_start:next_task]
            self.assertIn(expected, block, task_name)

    def test_pipeline_descriptions_match_generated_tasks(self) -> None:
        for comp_id, comp in self.catalog.components.items():
            task_name = pipeline_task_name(comp_id)
            expected = build_component_task_description(comp)
            block_start = self.pipeline_text.find(f"    - name: {task_name}\n")
            self.assertGreaterEqual(block_start, 0, task_name)
            next_task = self.pipeline_text.find("\n    - name:", block_start + 1)
            block = self.pipeline_text[block_start:next_task]
            pipeline_desc = extract_pipeline_task_description_from_block(block)
            self.assertEqual(pipeline_desc.strip(), expected.strip(), task_name)

    def test_replace_task_description_round_trip(self) -> None:
        comp = self.catalog.components["workbenches"]
        desc = build_component_task_description(comp)
        task_name = pipeline_task_name("workbenches")
        updated = _replace_task_description(self.pipeline_text, task_name, desc)
        again = _replace_task_description(updated, task_name, desc)
        self.assertEqual(again, updated)
        block_start = updated.find(f"    - name: {task_name}\n")
        next_task = updated.find("\n    - name:", block_start + 1)
        block = updated[block_start:next_task]
        self.assertIn(format_pipeline_task_description(desc).strip(), block)

    def test_regenerated_tasks_match_committed_files(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "rhoai-e2e"
            (root / "config").mkdir(parents=True)
            (root / "tekton" / "tasks").mkdir(parents=True)
            catalog_path = default_components_smoke_config_path()
            (root / "config" / catalog_path.name).write_text(
                catalog_path.read_text(encoding="utf-8"),
                encoding="utf-8",
            )
            for base in (
                "task-component-pytest.yaml",
                "task-component-golang.yaml",
                "task-component-playwright.yaml",
                "task-component-dashboard-cypress.yaml",
                "task-component-pending.yaml",
            ):
                src = RHOAI_E2E_ROOT / "tekton" / "tasks" / base
                (root / "tekton" / "tasks" / base).write_text(
                    src.read_text(encoding="utf-8"),
                    encoding="utf-8",
                )
            generate(rhoai_e2e_root=root, write_pipeline=False)
            committed = RHOAI_E2E_ROOT / "tekton" / "tasks" / "generated"
            for comp_id in self.catalog.component_ids:
                rel = f"component-{comp_id}.yaml"
                expected = committed / rel
                actual = root / "tekton" / "tasks" / "generated" / rel
                self.assertTrue(expected.is_file(), rel)
                self.assertTrue(actual.is_file(), rel)
                self.assertEqual(
                    actual.read_text(encoding="utf-8"),
                    expected.read_text(encoding="utf-8"),
                    rel,
                )

if __name__ == "__main__":
    raise SystemExit(unittest.main())
