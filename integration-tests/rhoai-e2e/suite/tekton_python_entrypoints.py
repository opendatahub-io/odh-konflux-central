"""Discover ``python -m …`` modules referenced from committed Tekton YAML."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Iterator

import yaml

_RHOAI_E2E_ROOT = Path(__file__).resolve().parent.parent
_TEKTON_ROOT = _RHOAI_E2E_ROOT / "tekton"
_ENTRYPOINT_RE = re.compile(
    r"""(?:\bpython3?\s+-m\s+|["']-m["'],\s*["'])"""
    r"""((?:runners|steps|install)\.[a-z_][a-z0-9_.]*)"""
)
_LEAN_IMAGE_MARKER = "OPENDATAHUB_TESTS_IMAGE"


def _iter_step_dicts(node: object) -> Iterator[dict]:
    if isinstance(node, dict):
        steps = node.get("steps")
        if isinstance(steps, list):
            for step in steps:
                if isinstance(step, dict):
                    yield step
        for value in node.values():
            yield from _iter_step_dicts(value)
    elif isinstance(node, list):
        for item in node:
            yield from _iter_step_dicts(item)


def discover_tekton_python_entrypoints(
    tekton_root: Path | None = None,
) -> dict[str, bool]:
    """Return Tekton ``python -m`` modules and whether any step uses the lean test image.

    Value ``True`` means the module is invoked from a step whose image is
    ``$(params.OPENDATAHUB_TESTS_IMAGE)`` (pytest/BVT container without PyYAML at import).
    """
    root = tekton_root or _TEKTON_ROOT
    modules: dict[str, bool] = {}
    for path in sorted(root.rglob("*.yaml")):
        try:
            doc = yaml.safe_load(path.read_text(encoding="utf-8"))
        except (yaml.YAMLError, OSError, UnicodeDecodeError):
            continue
        if doc is None:
            continue
        for step in _iter_step_dicts(doc):
            script = step.get("script")
            if not isinstance(script, str):
                continue
            lean_image = _LEAN_IMAGE_MARKER in str(step.get("image", ""))
            for match in _ENTRYPOINT_RE.finditer(script):
                module = match.group(1)
                modules[module] = modules.get(module, False) or lean_image
    return modules
