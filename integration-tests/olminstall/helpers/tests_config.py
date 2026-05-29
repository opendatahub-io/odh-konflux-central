"""Load olminstall-tests-config.yaml (PyYAML if available, else Mike Farah ``yq -o=json``)."""

from __future__ import annotations

import json
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .errors import AppError


@dataclass(frozen=True)
class TestsCatalog:
    """Phases allowed for the pipeline TESTS param and how they map to Tekton result flags."""

    schema_version: int
    phase_ids: tuple[str, ...]
    required_ids: frozenset[str]
    default_csv: str
    # phase_id -> result key -> bool (only True entries stored)
    sets_results: dict[str, frozenset[str]]
    known_result_keys: frozenset[str]


def _load_yaml_document(path: Path) -> dict[str, Any]:
    if not path.is_file():
        raise AppError(f"Tests config not found: {path}", 2)
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise AppError(f"Cannot read tests config {path}: {exc}", 2) from exc

    try:
        import yaml as pyyaml  # type: ignore[import-untyped, import-not-found]
    except ImportError:
        pyyaml = None
    if pyyaml is not None:
        try:
            loaded = pyyaml.safe_load(text)
        except pyyaml.YAMLError as exc:
            raise AppError(f"Invalid YAML in {path}: {exc}", 2) from exc
        if isinstance(loaded, dict):
            return loaded
        raise AppError(f"Tests config root must be a mapping: {path}", 2)

    yq_bin = shutil.which("yq")
    if yq_bin:
        try:
            proc = subprocess.run(
                [yq_bin, "e", "-o=json", ".", str(path)],
                capture_output=True,
                text=True,
                check=False,
                timeout=60,
            )
        except subprocess.TimeoutExpired as exc:
            raise AppError(f"yq timed out reading {path} (>{exc.timeout}s)", 2) from exc
        if proc.returncode == 0 and proc.stdout.strip():
            try:
                doc = json.loads(proc.stdout)
            except json.JSONDecodeError as exc:
                raise AppError(f"Invalid JSON from yq for {path}: {exc}", 2) from exc
            if isinstance(doc, dict):
                return doc
        err = (proc.stderr or proc.stdout or "").strip()
        raise AppError(f"yq failed for {path} (exit {proc.returncode}): {err or 'no output'}", 2)

    raise AppError(
        f"Cannot read {path}: install PyYAML (`pip install pyyaml`) or install "
        "Mike Farah's yq and ensure it is on PATH (`yq e -o=json . file.yaml`).",
        2,
    )


def load_tests_catalog(path: Path) -> TestsCatalog:
    doc = _load_yaml_document(path)
    ver = doc.get("schemaVersion")
    if ver != 1:
        raise AppError(f"Unsupported tests config schemaVersion {ver!r} in {path} (expected 1).", 2)
    raw_phases = doc.get("phases")
    if not isinstance(raw_phases, list) or not raw_phases:
        raise AppError(f"Tests config must define non-empty phases: {path}", 2)

    phase_ids: list[str] = []
    required: set[str] = set()
    default_parts: list[str] = []
    sets_results: dict[str, frozenset[str]] = {}
    known_keys: set[str] = set()

    for i, item in enumerate(raw_phases):
        if not isinstance(item, dict):
            raise AppError(f"phases[{i}] must be a mapping in {path}", 2)
        pid = item.get("id")
        if not isinstance(pid, str) or not pid.strip():
            raise AppError(f"phases[{i}].id must be a non-empty string in {path}", 2)
        pid = pid.strip()
        if pid in phase_ids:
            raise AppError(f"Duplicate phase id {pid!r} in {path}", 2)
        phase_ids.append(pid)

        if item.get("requiredInSelection") is True:
            required.add(pid)
        if item.get("default") is True:
            default_parts.append(pid)

        spr = item.get("setsPipelineResults")
        if spr is None:
            continue
        if not isinstance(spr, dict):
            raise AppError(f"phases[{i}].setsPipelineResults must be a mapping in {path}", 2)
        keys: set[str] = set()
        for rk, rv in spr.items():
            if rv is True:
                if not isinstance(rk, str) or not rk.strip():
                    raise AppError(
                        f"phases[{i}].setsPipelineResults[{rk!r}] has an invalid result key; "
                        f"keys must be non-empty strings in {path}",
                        2,
                    )
                k = rk.strip()
                keys.add(k)
                known_keys.add(k)
            elif rv in (False, None):
                continue
            else:
                raise AppError(
                    f"phases[{i}].setsPipelineResults[{rk!r}] must be true or false/omitted in {path}",
                    2,
                )
        if keys:
            sets_results[pid] = frozenset(keys)

    if not known_keys:
        raise AppError(
            f"At least one phase must set pipelineResults (setsPipelineResults) in {path}", 2
        )

    default_csv = _canonical_csv(default_parts, phase_ids)
    return TestsCatalog(
        schema_version=1,
        phase_ids=tuple(phase_ids),
        required_ids=frozenset(required),
        default_csv=default_csv,
        sets_results=sets_results,
        known_result_keys=frozenset(known_keys),
    )


def _canonical_csv(selected: list[str], phase_order: list[str]) -> str:
    """Order: same as phase_order in config; only include selected ids."""
    sel = [p for p in phase_order if p in selected]
    return ",".join(sel)


def compute_pipeline_result_flags(selected_ids: frozenset[str], catalog: TestsCatalog) -> dict[str, bool]:
    """Map selected phase ids to Tekton parse-pipeline-tests result booleans (string true/false)."""
    out = {k: False for k in catalog.known_result_keys}
    for pid, keys in catalog.sets_results.items():
        if pid not in selected_ids:
            continue
        for k in keys:
            out[k] = True
    return out
