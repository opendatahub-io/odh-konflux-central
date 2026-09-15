"""Regression: cluster/install/test tasks must skip when CONFORMA_GATE=skip."""

from __future__ import annotations

import unittest
from typing import Any

import yaml

from unit_tests._paths import OLMINSTALL_ROOT

_PIPELINE = OLMINSTALL_ROOT / "tekton" / "pipelines" / "olminstall-pipeline.yaml"

_CONFORMA_INPUT = "$(tasks.wait-for-conforma.results.CONFORMA_GATE)"

# Always run: gate itself, parse, and publish-results (reports conforma skip failure).
_EXEMPT = frozenset(
    {
        "parse-pipeline-tests",
        "wait-for-conforma",
        "publish-results",
    }
)


def _task_blocks(spec: dict[str, Any]) -> list[tuple[str, dict[str, Any]]]:
    blocks: list[tuple[str, dict[str, Any]]] = []
    for section in ("tasks", "finally"):
        for task in spec.get(section) or []:
            if not isinstance(task, dict):
                continue
            name = str(task.get("name") or "").strip()
            if not name:
                continue
            blocks.append((name, task))
    return blocks


def _has_conforma_pass_when(task: dict[str, Any]) -> bool:
    for expr in task.get("when") or []:
        if not isinstance(expr, dict):
            continue
        if str(expr.get("input") or "").strip() != _CONFORMA_INPUT:
            continue
        if str(expr.get("operator") or "").strip() != "in":
            continue
        values = expr.get("values") or []
        if any(str(v).strip() == "pass" for v in values):
            return True
    return False


class ConformaGateWhenTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        with open(_PIPELINE, encoding="utf-8") as fh:
            doc = yaml.safe_load(fh)
        cls.blocks = _task_blocks(doc.get("spec") or {})

    def test_workload_tasks_require_conforma_pass_when(self) -> None:
        missing = [
            name
            for name, task in self.blocks
            if name not in _EXEMPT and not _has_conforma_pass_when(task)
        ]
        self.assertEqual(
            missing,
            [],
            "Add CONFORMA_GATE=pass when to: "
            + ", ".join(missing)
            + ". publish-results stays exempt so conforma skip failures surface.",
        )

    def test_publish_results_not_gated_on_conforma(self) -> None:
        publish = next(task for name, task in self.blocks if name == "publish-results")
        self.assertFalse(_has_conforma_pass_when(publish))

    def test_collect_diagnostics_gated_on_conforma(self) -> None:
        collect = next(task for name, task in self.blocks if name == "collect-diagnostics")
        self.assertTrue(_has_conforma_pass_when(collect))


if __name__ == "__main__":
    unittest.main()
