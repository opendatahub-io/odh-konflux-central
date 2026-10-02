"""Shared tests-payload layout under the Tekton tests-shared workspace."""

from __future__ import annotations

import fnmatch
import json
import shutil
from pathlib import Path

TESTS_PAYLOAD_DIRNAME = "tests-payload"
TESTS_PAYLOAD_RESULTS_DIRNAME = "results"
TESTS_PAYLOAD_TOOLS_DIRNAME = ".tools"
TESTS_PAYLOAD_UPLOAD_STAGING_DIRNAME = ".upload-staging"
COMPONENT_TEST_PLAN_FILENAME = "component-test-plan.json"

_SKIP_DIR_NAMES = frozenset({".tools", ".upload-staging"})
_SKIP_FILE_NAMES = frozenset(
    {
        "component-test-plan.json",
        ".oci-upload-ok",
        "component-test.exit",
        "component-smoke.exit",
    }
)
DEFAULT_UPLOAD_PATTERNS = ("*.xml", "*.log", "*.console.log")
AGGREGATE_TEST_OUTPUT_FILENAME = ".rhoai-e2e-smoke-test-output.json"
BVT_AGGREGATE_TEST_OUTPUT_FILENAME = ".rhoai-e2e-bvt-test-output.json"


def gate_test_output_sidecar_filename(note_prefix: str) -> str | None:
    """Workspace sidecar filename for gate TEST_OUTPUT, or None for per-component runs."""
    prefix = note_prefix.strip().upper()
    if prefix == "BVT":
        return BVT_AGGREGATE_TEST_OUTPUT_FILENAME
    if prefix == "COMPONENT":
        return AGGREGATE_TEST_OUTPUT_FILENAME
    return None


def gate_test_output_sidecar_path(
    artifacts_dir: str | Path,
    *,
    note_prefix: str,
) -> Path | None:
    """Absolute path for a gate-level TEST_OUTPUT sidecar under tests-payload."""
    filename = gate_test_output_sidecar_filename(note_prefix)
    if filename is None:
        return None
    return resolve_tests_payload_root(artifacts_dir) / filename


def bvt_test_output_sidecar_path(tests_shared: str | Path) -> Path:
    return tests_payload_root(tests_shared) / BVT_AGGREGATE_TEST_OUTPUT_FILENAME


def smoke_test_output_sidecar_path(tests_shared: str | Path) -> Path:
    return tests_payload_root(tests_shared) / AGGREGATE_TEST_OUTPUT_FILENAME


COLLECT_DIAGNOSTICS_DONE_MARKER = ".collect-diagnostics-done"


def tests_payload_root(tests_shared: str | Path) -> Path:
    root = Path(tests_shared)
    if root.name == TESTS_PAYLOAD_DIRNAME:
        return root
    return root / TESTS_PAYLOAD_DIRNAME


def tests_payload_results_dir(tests_shared: str | Path) -> Path:
    return tests_payload_root(tests_shared) / TESTS_PAYLOAD_RESULTS_DIRNAME


def tests_payload_tools_bin_dir(tests_shared: str | Path) -> Path:
    return tests_payload_root(tests_shared) / TESTS_PAYLOAD_TOOLS_DIRNAME / "bin"


def tests_payload_tools_python_dir(tests_shared: str | Path) -> Path:
    return tests_payload_root(tests_shared) / TESTS_PAYLOAD_TOOLS_DIRNAME / "python"


def component_test_plan_path(tests_shared: str | Path) -> Path:
    return tests_payload_root(tests_shared) / COMPONENT_TEST_PLAN_FILENAME


def ensure_tests_payload_layout(tests_shared: str | Path) -> Path:
    """Create tests-payload with results/ for JUnit + logs (tools live under .tools/)."""
    root = tests_payload_root(tests_shared)
    tests_payload_results_dir(tests_shared).mkdir(parents=True, exist_ok=True)
    return root


def artifact_prefix_for_component(component_id: str, plan_path: Path) -> str:
    if plan_path.is_file():
        try:
            plan_raw = json.loads(plan_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            plan_raw = {}
        for item in plan_raw.get("components") or []:
            if isinstance(item, dict) and str(item.get("id", "")).strip() == component_id:
                prefix = str(item.get("artifact_prefix", "")).strip()
                if prefix:
                    return prefix
    return component_id.replace("_", "-") + "-smoke"


def junit_xml_for_component(component_id: str, artifacts_dir: Path, plan_path: Path) -> Path | None:
    prefix = artifact_prefix_for_component(component_id, plan_path)
    direct = artifacts_dir / f"{prefix}.xml"
    if direct.is_file():
        return direct
    matches = sorted(artifacts_dir.glob(f"{prefix}*.xml"))
    return matches[0] if matches else None


def resolve_tests_payload_root(path: str | Path) -> Path:
    """Return tests-payload root from tests-shared workspace or payload path."""
    path = Path(path)
    if path.name == TESTS_PAYLOAD_DIRNAME:
        return path
    nested = path / TESTS_PAYLOAD_DIRNAME
    if nested.is_dir():
        return nested
    for parent in (path, *path.parents):
        if parent.name == TESTS_PAYLOAD_DIRNAME:
            return parent
    return nested


def oci_upload_marker(tests_shared_or_payload: Path) -> Path:
    """`.oci-upload-ok` at tests-payload root after publish-results upload."""
    return resolve_tests_payload_root(tests_shared_or_payload) / ".oci-upload-ok"


def collect_diagnostics_done_marker(tests_shared_or_payload: Path) -> Path:
    """Written when collect-diagnostics finishes (publish-results waits on this)."""
    return resolve_tests_payload_root(tests_shared_or_payload) / COLLECT_DIAGNOSTICS_DONE_MARKER


def mark_collect_diagnostics_done(
    tests_shared_or_payload: str | Path,
    *,
    artifact_name: str = "",
    status: str = "done",
) -> Path:
    root = resolve_tests_payload_root(Path(tests_shared_or_payload))
    root.mkdir(parents=True, exist_ok=True)
    marker = root / COLLECT_DIAGNOSTICS_DONE_MARKER
    body = status.strip() or "done"
    if artifact_name.strip():
        body = f"{body} artifact={artifact_name.strip()}"
    marker.write_text(body + "\n", encoding="utf-8")
    return marker


def _matches_upload_pattern(name: str, pattern: str) -> bool:
    return fnmatch.fnmatch(name, pattern)


def _should_skip_upload_file(path: Path, payload_root: Path) -> bool:
    try:
        rel = path.relative_to(payload_root)
    except ValueError:
        return True
    if rel.name in _SKIP_FILE_NAMES:
        return True
    if any(part.startswith(".") or part in _SKIP_DIR_NAMES for part in rel.parts):
        return True
    if path.name == "oc" and "bin" in path.parts:
        return True
    return False


def collect_upload_files(
    payload_root: Path,
    *,
    patterns: tuple[str, ...] = DEFAULT_UPLOAD_PATTERNS,
) -> list[Path]:
    """Return JUnit/log files under results/ (and legacy gate dirs) matching *patterns*."""
    scan_dirs: list[Path] = []
    results = tests_payload_results_dir(payload_root)
    if results.is_dir():
        scan_dirs.append(results)
    for legacy in ("bvt", "smoke"):
        legacy_dir = payload_root / legacy
        if legacy_dir.is_dir():
            scan_dirs.append(legacy_dir)

    matched: list[Path] = []
    seen: set[str] = set()
    for base in scan_dirs:
        for path in sorted(base.rglob("*")):
            if not path.is_file():
                continue
            if _should_skip_upload_file(path, payload_root):
                continue
            if not any(_matches_upload_pattern(path.name, pat) for pat in patterns):
                continue
            key = str(path.resolve())
            if key in seen:
                continue
            seen.add(key)
            matched.append(path)
    return matched


def stage_tests_payload_for_upload(
    payload_root: Path,
    *,
    oci_subdir: str,
    patterns: tuple[str, ...] = DEFAULT_UPLOAD_PATTERNS,
) -> Path:
    """Populate ``.upload-staging/<oci_subdir>/`` and return the staging root."""
    staging_root = payload_root / TESTS_PAYLOAD_UPLOAD_STAGING_DIRNAME
    target = staging_root / oci_subdir.strip().strip("/")
    if staging_root.exists():
        shutil.rmtree(staging_root)
    target.mkdir(parents=True)

    files = collect_upload_files(payload_root, patterns=patterns)
    for src in files:
        dest = target / src.name
        if dest.exists():
            dest = target / f"{src.parent.name}-{src.name}"
        shutil.copy2(src, dest)
    return staging_root


def has_publishable_artifacts(
    tests_shared_or_payload: Path,
    *,
    patterns: tuple[str, ...] = DEFAULT_UPLOAD_PATTERNS,
) -> bool:
    root = resolve_tests_payload_root(tests_shared_or_payload)
    if not root.is_dir():
        return False
    return bool(collect_upload_files(root, patterns=patterns))
