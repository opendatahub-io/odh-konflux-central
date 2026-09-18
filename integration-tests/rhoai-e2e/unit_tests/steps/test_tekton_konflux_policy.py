"""Konflux/Tekton policy checks for rhoai-e2e YAML (no cluster required).

Encodes resolver footguns seen in production (CouldntGetTask): nested taskSpec.finally,
forbidden step fields, missing git-resolved task paths, and orphan result references.
"""

from __future__ import annotations

import re
import unittest
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import yaml

from unit_tests._paths import RHOAI_E2E_ROOT, REPO_ROOT

_RHOAI_E2E = RHOAI_E2E_ROOT
_REPO_ROOT = REPO_ROOT
_TEKTON_DIR = _RHOAI_E2E / "tekton"
_PIPELINE = _TEKTON_DIR / "pipelines" / "rhoai-e2e-pipeline.yaml"
_TASKS_DIR = _TEKTON_DIR / "tasks"

_RESULT_PATH = re.compile(r"\$\(results\.([A-Za-z0-9_-]+)\.path\)")
# Tekton step keys Konflux accepts; step-level ``description`` caused CouldntGetTask (fkmx8).
_STEP_ALLOWED_KEYS = frozenset(
    {
        "args",
        "command",
        "computeResources",
        "displayName",
        "env",
        "envFrom",
        "image",
        "imagePullPolicy",
        "name",
        "onError",
        "ref",
        "results",
        "script",
        "securityContext",
        "stderrConfig",
        "stdoutConfig",
        "timeout",
        "volumeDevices",
        "volumeMounts",
        "when",
        "workingDir",
        "params",
        "workspaces",
    }
)

def _tekton_yaml_paths() -> list[Path]:
    paths = [
        _PIPELINE,
        _TEKTON_DIR / "pipelines" / "rhoai-e2e-pipelinerun.yaml",
        *_TASKS_DIR.glob("*.yaml"),
    ]
    return sorted(p for p in paths if p.is_file())

def _load_yaml(path: Path) -> Any:
    with path.open(encoding="utf-8") as fh:
        return yaml.safe_load(fh)

def _iter_pipeline_inline_task_specs(doc: dict[str, Any]) -> Iterator[tuple[str, dict[str, Any]]]:
    spec = doc.get("spec") or {}
    for section in ("tasks", "finally"):
        for task in spec.get(section) or []:
            if not isinstance(task, dict):
                continue
            task_spec = task.get("taskSpec")
            if isinstance(task_spec, dict):
                label = f"{section}/{task.get('name', '?')}"
                yield label, task_spec

def _iter_task_specs(doc: dict[str, Any], *, source: str) -> Iterator[tuple[str, dict[str, Any]]]:
    if doc.get("kind") == "Pipeline":
        yield from _iter_pipeline_inline_task_specs(doc)
        return
    if doc.get("kind") == "Task":
        spec = doc.get("spec")
        if isinstance(spec, dict):
            yield source, spec

def _walk_strings(obj: Any) -> Iterator[str]:
    if isinstance(obj, str):
        yield obj
    elif isinstance(obj, dict):
        for val in obj.values():
            yield from _walk_strings(val)
    elif isinstance(obj, list):
        for item in obj:
            yield from _walk_strings(item)

def _result_names(task_spec: dict[str, Any]) -> set[str]:
    names: set[str] = set()
    for entry in task_spec.get("results") or []:
        if isinstance(entry, dict) and entry.get("name"):
            names.add(str(entry["name"]))
    return names

def _git_path_in_repo(task_ref: dict[str, Any]) -> str | None:
    if not isinstance(task_ref, dict):
        return None
    resolver = task_ref.get("resolver")
    params = task_ref.get("params")
    if resolver != "git" or not isinstance(params, list):
        return None
    for param in params:
        if isinstance(param, dict) and param.get("name") == "pathInRepo":
            return str(param.get("value") or "").strip() or None
    return None

def _iter_git_task_refs(obj: Any, *, label: str = "") -> Iterator[tuple[str, str]]:
    if isinstance(obj, dict):
        if "taskRef" in obj and isinstance(obj["taskRef"], dict):
            path = _git_path_in_repo(obj["taskRef"])
            if path:
                task_label = str(obj.get("name") or label or "?")
                yield task_label, path
        for key, val in obj.items():
            child_label = label or str(obj.get("name") or "")
            yield from _iter_git_task_refs(val, label=child_label if key != "taskRef" else label)
    elif isinstance(obj, list):
        for item in obj:
            yield from _iter_git_task_refs(item, label=label)

class TektonKonfluxPolicyTest(unittest.TestCase):
    """Static policy guardrails for Konflux git-resolved Tekton."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.paths = _tekton_yaml_paths()
        cls.pipeline_doc = _load_yaml(_PIPELINE)

    def test_tekton_yaml_files_parse(self) -> None:
        for path in self.paths:
            with self.subTest(path=path.name):
                doc = _load_yaml(path)
                self.assertIsInstance(doc, dict, "top-level mapping expected")
                self.assertIn("kind", doc)

    def test_standalone_tasks_have_no_spec_finally(self) -> None:
        """Konflux git resolver rejects Task.spec.finally (CouldntGetTask qx7st)."""
        violations: list[str] = []
        for path in self.paths:
            doc = _load_yaml(path)
            if doc.get("kind") != "Task":
                continue
            spec = doc.get("spec") or {}
            if spec.get("finally"):
                violations.append(path.name)
        self.assertEqual(
            violations,
            [],
            "Move write-konflux-task-summary from Task.spec.finally to spec.steps with onError: continue: "
            + ", ".join(violations),
        )

    def test_pipeline_inline_taskspec_has_no_finally(self) -> None:
        """Konflux git resolver rejects nested taskSpec.finally (CouldntGetTask p7drq)."""
        violations: list[str] = []
        for label, task_spec in _iter_pipeline_inline_task_specs(self.pipeline_doc):
            if task_spec.get("finally"):
                violations.append(label)
        self.assertEqual(
            violations,
            [],
            "Remove taskSpec.finally from inline pipeline tasks; use a last step or "
            "a git-resolved Task YAML instead: "
            + ", ".join(violations),
        )

    def test_steps_have_no_description_field(self) -> None:
        """Step-level description is rejected by Konflux resolver (CouldntGetTask fkmx8)."""
        violations: list[str] = []
        for path in self.paths:
            doc = _load_yaml(path)
            for label, task_spec in _iter_task_specs(doc, source=path.name):
                for step in task_spec.get("steps") or []:
                    if not isinstance(step, dict):
                        continue
                    if "description" in step:
                        violations.append(f"{path.name}:{label}:step/{step.get('name', '?')}")
                for step in task_spec.get("finally") or []:
                    if isinstance(step, dict) and "description" in step:
                        violations.append(
                            f"{path.name}:{label}:finally/{step.get('name', '?')}",
                        )
        self.assertEqual(
            violations,
            [],
            "Remove spec.steps[].description (task/step description belongs on Task/Pipeline): "
            + ", ".join(violations),
        )

    def test_steps_use_allowed_keys_only(self) -> None:
        violations: list[str] = []
        for path in self.paths:
            doc = _load_yaml(path)
            for label, task_spec in _iter_task_specs(doc, source=path.name):
                for section in ("steps", "finally"):
                    for step in task_spec.get(section) or []:
                        if not isinstance(step, dict):
                            violations.append(
                                f"{path.name}:{label}:{section} entry is not a mapping",
                            )
                            continue
                        if "name" not in step:
                            violations.append(
                                f"{path.name}:{label}:{section} missing name",
                            )
                        unknown = sorted(set(step) - _STEP_ALLOWED_KEYS)
                        if unknown:
                            violations.append(
                                f"{path.name}:{label}:{section}/{step.get('name', '?')}: {unknown}",
                            )
        self.assertEqual(violations, [], "Unexpected step keys: " + "; ".join(violations))

    def test_git_taskref_paths_exist_in_repo(self) -> None:
        missing: list[str] = []
        for path in self.paths:
            doc = _load_yaml(path)
            for task_label, repo_path in _iter_git_task_refs(doc, label=path.name):
                if not repo_path.startswith("integration-tests/rhoai-e2e/"):
                    continue
                full = _REPO_ROOT / repo_path
                if not full.is_file():
                    missing.append(f"{path.name}/{task_label} -> {repo_path}")
        self.assertEqual(
            missing,
            [],
            "git taskRef pathInRepo must exist in repo: " + "; ".join(missing),
        )

    def test_result_path_references_are_declared(self) -> None:
        violations: list[str] = []
        for path in self.paths:
            doc = _load_yaml(path)
            for label, task_spec in _iter_task_specs(doc, source=path.name):
                declared = _result_names(task_spec)
                if not declared:
                    continue
                for text in _walk_strings(task_spec):
                    for match in _RESULT_PATH.finditer(text):
                        name = match.group(1)
                        if name not in declared:
                            violations.append(
                                f"{path.name}:{label}: $(results.{name}.path) not in {sorted(declared)}",
                            )
        self.assertEqual(
            violations,
            [],
            "Undeclared Tekton result references: " + "; ".join(violations),
        )

    def test_write_task_message_uses_pipeline_task_label(self) -> None:
        """PIPELINE_TASK must come from pod label; context.pipelineTask is not substituted in taskSpec steps."""
        needle = "metadata.labels['tekton.dev/pipelineTask']"
        violations: list[str] = []
        for path in _tekton_yaml_paths():
            text = path.read_text(encoding="utf-8")
            if "write-konflux-task-summary" not in text:
                continue
            if needle not in text:
                violations.append(path.name)
            if "$(context.pipelineTask.name)" in text:
                violations.append(f"{path.name}:context.pipelineTask.name")
        self.assertEqual(violations, [], "write-konflux-task-summary PIPELINE_TASK wiring: " + ", ".join(violations))

