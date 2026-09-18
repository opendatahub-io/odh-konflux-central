#!/usr/bin/env python3
"""Compare rhoai-e2e ``component:`` slices to TestOps Jenkins components-testing main.yaml.

Maintainers run this locally when updating ``rhoai-e2e-components-smoke.yaml``; Konflux
does not clone Jenkins at pipeline runtime.

Requires a checkout of TestOps Jenkins (GitLab ``ods/jenkins``) via ``--jenkins-repo`` or
``JENKINS_REPO``.
"""

from __future__ import annotations

import argparse
import os
import re
import sys
from pathlib import Path
from typing import Any

from _bootstrap import ensure_rhoai_e2e_path

ensure_rhoai_e2e_path()

try:
    import yaml
except ImportError:
    print("ERROR: pip install pyyaml", file=sys.stderr)
    sys.exit(2)

_JUNIT_RE = re.compile(r"junit_suite_name=(\S+)", re.I)
_TESTS_RE = re.compile(r"^tests/.+/?$")
_RHOAI_E2E_ROOT = Path(__file__).resolve().parent.parent
_DEFAULT_CATALOG = _RHOAI_E2E_ROOT / "config" / "rhoai-e2e-components-smoke.yaml"
_DISTRIBUTED_WORKLOADS_FRAMEWORK = "distributed-workloads"
_JUNIT_HIDE_SKIPPED = "--junitfile-hide-skipped-tests"


def _normalize_marker(raw: str) -> str:
    s = raw.strip().strip('"').strip("'")
    return s[3:].strip().strip('"').strip("'") if s.startswith("-m ") else s


def _extract_slice(doc: dict[str, Any]) -> dict[str, str]:
    merge = doc.get("merge") or {}
    if not isinstance(merge, dict):
        jenkins = doc.get("component")
        merge = jenkins.get("merge", {}) if isinstance(jenkins, dict) else {}
    image = merge.get("image") or {}
    args = image.get("args") or []
    if isinstance(args, str):
        args = [args]
    tests_subdir = ""
    junit_suite = ""
    for item in args:
        s = str(item).strip()
        bare = s[3:].strip() if s.startswith("-o ") else s
        path = bare.rstrip("/")
        if _TESTS_RE.match(path):
            tests_subdir = path
        m = _JUNIT_RE.search(bare)
        if m:
            junit_suite = m.group(1)
    qg = (merge.get("qualityGatesMap") or {}).get("default") or {}
    smoke = _normalize_marker(str(qg.get("smoke", "")))
    meta = merge.get("metadata") or {}
    name = str(meta.get("name", "")).strip()
    return {
        "jenkins_name": name,
        "tests_subdir": tests_subdir,
        "smoke_marker": smoke,
        "junit_suite": junit_suite,
    }


def _load_yaml(path: Path) -> dict[str, Any]:
    with path.open(encoding="utf-8") as f:
        doc = yaml.safe_load(f)
    return doc if isinstance(doc, dict) else {}


def _catalog_konflux_phase_commands(catalog_path: Path, cid: str) -> dict[str, str]:
    doc = _load_yaml(catalog_path)
    for item in doc.get("components") or []:
        if not isinstance(item, dict):
            continue
        konflux = item.get("konflux") or {}
        if str(konflux.get("id") or "").strip() != cid:
            continue
        runner = konflux.get("runner") or {}
        phase_commands = runner.get("phaseCommands") or {}
        if isinstance(phase_commands, dict):
            return {str(k): str(v) for k, v in phase_commands.items()}
    return {}


def _unwrap_component(doc: dict[str, Any]) -> dict[str, Any]:
    """Some upstream main.yaml wrap fields under a top-level ``component:`` key."""
    if "merge" in doc or "copyFromFramework" in doc:
        return doc
    inner = doc.get("component")
    return inner if isinstance(inner, dict) else doc


def _upstream_copy_framework(upstream_doc: dict[str, Any]) -> str:
    return str(_unwrap_component(upstream_doc).get("copyFromFramework") or "").strip()


def _golang_junit_parity_issues(
    *,
    cid: str,
    catalog_path: Path,
    upstream_doc: dict[str, Any],
) -> list[str]:
    """Jenkins distributed-workloads golang framework omits tier skips from junit."""
    framework = _upstream_copy_framework(upstream_doc)
    doc = _unwrap_component(upstream_doc)
    jname = str((doc.get("merge") or {}).get("metadata", {}).get("name", "")).strip()
    if framework != _DISTRIBUTED_WORKLOADS_FRAMEWORK and jname != _DISTRIBUTED_WORKLOADS_FRAMEWORK:
        return []
    phase_commands = _catalog_konflux_phase_commands(catalog_path, cid)
    if not phase_commands:
        return []
    issues: list[str] = []
    for phase, command in phase_commands.items():
        if "run-test.sh" not in command or "--junitfile" not in command:
            continue
        if _JUNIT_HIDE_SKIPPED not in command:
            issues.append(
                f"junit_hide_skipped: phase={phase!r} missing {_JUNIT_HIDE_SKIPPED!r} "
                f"(Jenkins shared-frameworks/{_DISTRIBUTED_WORKLOADS_FRAMEWORK})"
            )
    if cid == "trainer":
        for phase, command in phase_commands.items():
            if "run-test.sh" in command and "--junitfile-project-name=trainer" not in command:
                issues.append(
                    f"junit_project_name: phase={phase!r} missing --junitfile-project-name=trainer "
                    "(Jenkins trainer/main.yaml image.args)"
                )
    return issues


def _catalog_entries(catalog_path: Path) -> list[tuple[str, dict[str, Any]]]:
    doc = _load_yaml(catalog_path)
    out: list[tuple[str, dict[str, Any]]] = []
    for item in doc.get("components") or []:
        if not isinstance(item, dict):
            continue
        konflux = item.get("konflux") or {}
        jenkins = item.get("component") or {}
        cid = str(konflux.get("id") or "").strip()
        if cid and isinstance(jenkins, dict):
            out.append((cid, jenkins))
    return out


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Compare rhoai-e2e-components-smoke.yaml component: slices to TestOps Jenkins main.yaml",
    )
    parser.add_argument(
        "--jenkins-repo",
        default=os.environ.get("JENKINS_REPO", "").strip(),
        help="Path to TestOps Jenkins checkout (ods/jenkins). Default: JENKINS_REPO env var.",
    )
    parser.add_argument(
        "--catalog",
        type=Path,
        default=_DEFAULT_CATALOG,
        help=f"Smoke catalog YAML (default: {_DEFAULT_CATALOG.name} beside this repo).",
    )
    parser.add_argument("--list-upstream", action="store_true")
    parser.add_argument("--show-patch-hints", action="store_true")
    args = parser.parse_args()

    if not args.jenkins_repo:
        print(
            "ERROR: set JENKINS_REPO or pass --jenkins-repo (TestOps Jenkins checkout)",
            file=sys.stderr,
        )
        return 2

    jenkins_root = Path(args.jenkins_repo)
    catalog = args.catalog
    components_base = jenkins_root / "resources/configs/components-testing/components"

    if not catalog.is_file():
        print(f"ERROR: catalog not found: {catalog}", file=sys.stderr)
        return 2
    if not components_base.is_dir():
        print(f"ERROR: Jenkins components dir not found: {components_base}", file=sys.stderr)
        return 2

    entries = _catalog_entries(catalog)
    if args.list_upstream:
        for cid, jenkins in entries:
            name = (jenkins.get("merge") or {}).get("metadata", {}).get("name", "?")
            print(f"{cid}\t{name}\t{components_base / name / 'main.yaml'}")
        return 0

    drift = 0
    for cid, jenkins_block in entries:
        local = _extract_slice({"merge": jenkins_block.get("merge")})
        jname = local["jenkins_name"]
        upstream_path = components_base / jname / "main.yaml"
        label = f"{cid} (component:{jname})"

        if not upstream_path.is_file():
            print(f"MISSING\t{label}\t{upstream_path}")
            drift += 1
            continue

        upstream_doc = _load_yaml(upstream_path)
        upstream = _extract_slice(upstream_doc)
        overrides = sorted(p.name for p in upstream_path.parent.glob("*.yaml") if p.name != "main.yaml")

        diffs: list[str] = []
        for key in ("tests_subdir", "smoke_marker"):
            if local[key] != upstream[key]:
                diffs.append(f"{key}: catalog={local[key]!r} upstream={upstream[key]!r}")
        if local["junit_suite"] and upstream["junit_suite"] and local["junit_suite"] != upstream["junit_suite"]:
            diffs.append(
                f"junit_suite: catalog={local['junit_suite']!r} upstream={upstream['junit_suite']!r}"
            )
        diffs.extend(
            _golang_junit_parity_issues(
                cid=cid,
                catalog_path=catalog,
                upstream_doc=upstream_doc,
            )
        )

        if diffs:
            print(f"DRIFT\t{label}")
            for d in diffs:
                print(f"  {d}")
            if overrides:
                print(f"  note: override files present: {', '.join(overrides)}")
                print(
                    "  note: Konflux uses installed CSV + minRhoai/maxRhoai gates "
                    "(see component_version_gate.py); pin only exceptions in konflux.opendatahubTestsImage"
                )
            if args.show_patch_hints:
                print(
                    "  hint: update component.merge in catalog from upstream main.yaml "
                    "(merge.image.args, qualityGatesMap.default.smoke)"
                )
            drift += 1
        else:
            extra = f" (overrides: {', '.join(overrides)})" if overrides else ""
            if overrides:
                extra += " — Konflux: CSV probe + gates (not per-version YAML)"
            print(f"OK\t{label}{extra}")

    return 1 if drift else 0


if __name__ == "__main__":
    raise SystemExit(main())
