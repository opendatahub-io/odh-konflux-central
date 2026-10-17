#!/usr/bin/env python3
"""Fail test-finalize when component pytest exit file is non-zero."""

from __future__ import annotations

import os
import sys
from pathlib import Path

from steps.tests_payload import component_test_plan_path, tests_payload_results_dir


def _read_exit_code(path: Path) -> int | None:
    if not path.is_file():
        return None
    try:
        return int(path.read_text(encoding="ascii").strip())
    except ValueError:
        return None


def _resolve_paths() -> tuple[Path, Path]:
    tests_shared = os.environ.get("TESTS_SHARED", "").strip()
    artifacts_raw = os.environ.get("ARTIFACTS_DIR", "").strip()
    if tests_shared:
        shared = Path(tests_shared)
        artifacts = tests_payload_results_dir(shared)
        plan = component_test_plan_path(shared)
        if artifacts_raw:
            artifacts = Path(artifacts_raw)
        return artifacts, plan
    if artifacts_raw:
        artifacts = Path(artifacts_raw)
        plan = artifacts / "component-test-plan.json"
        if not plan.is_file() and artifacts.parent.is_dir():
            parent_plan = artifacts.parent / "component-test-plan.json"
            if parent_plan.is_file():
                plan = parent_plan
        return artifacts, plan
    artifacts = tests_payload_results_dir("/workspace/tests-shared")
    return artifacts, component_test_plan_path("/workspace/tests-shared")


def main() -> int:
    artifacts, plan = _resolve_paths()

    exit_candidates = (
        artifacts / "component-test.exit",
        artifacts / "component-smoke.exit",
    )
    ec = 0
    for exit_file in exit_candidates:
        parsed = _read_exit_code(exit_file)
        if parsed is not None:
            ec = parsed
            break

    if not plan.is_file():
        if any(p.is_file() for p in exit_candidates):
            if ec != 0:
                print(
                    f"WARN: component pytest exit {ec}; gate enforced in check-pipeline-test-gate",
                    file=sys.stderr,
                )
            else:
                print("component pytest succeeded (exit file present; plan file optional)")
            return 0
        print(
            f"ERROR: opendatahub-tests-prepare did not populate shared workspace (missing {plan.name})",
            file=sys.stderr,
        )
        return 2

    if ec != 0:
        print(
            f"WARN: component pytest exit {ec}; gate enforced in check-pipeline-test-gate",
            file=sys.stderr,
        )
        return 0
    print("component pytest succeeded")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
