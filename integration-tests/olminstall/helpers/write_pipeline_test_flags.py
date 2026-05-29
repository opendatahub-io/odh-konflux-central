#!/usr/bin/env python3
"""
Tekton step: read TESTS param + olminstall-tests-config.yaml, write RUN_SMOKE / RUN_BVT / RUN_TIER1 result files.

Invoked from parse-pipeline-tests after SCRIPTS_REPO is cloned to REPO_ROOT (e.g. /tmp/repo).
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

_OLMINSTALL = Path(__file__).resolve().parent.parent
if str(_OLMINSTALL) not in sys.path:
    sys.path.insert(0, str(_OLMINSTALL))

from helpers.errors import AppError
from helpers.tests_config import compute_pipeline_result_flags, load_tests_catalog
from helpers.tests_plan import parse_tests_selection, validate_and_normalize_tests_csv

try:
    import yaml
except ImportError:
    yaml = None  # type: ignore[assignment]


def main() -> int:
    tests_raw = os.environ.get("TESTS", "").strip()
    repo_root = os.environ.get("REPO_ROOT", "").strip()
    if not repo_root:
        print("REPO_ROOT is required (clone destination of SCRIPTS_REPO).", file=sys.stderr)
        return 1
    root = Path(repo_root)
    cfg = root / "integration-tests" / "olminstall" / "olminstall-tests-config.yaml"

    try:
        catalog = load_tests_catalog(cfg)
        csv = validate_and_normalize_tests_csv(tests_raw if tests_raw else None, catalog)
        selected = parse_tests_selection(csv, catalog)
        flags = compute_pipeline_result_flags(selected, catalog)
    except AppError as exc:
        print(
            f"ERROR: tests config or selection failed (fix YAML/CSV or paths): {exc}",
            file=sys.stderr,
        )
        return 1
    except FileNotFoundError as exc:
        print(
            f"ERROR: file not found — verify REPO_ROOT={repo_root!r} and that the repo contains "
            f"integration-tests/olminstall/olminstall-tests-config.yaml: {exc}",
            file=sys.stderr,
        )
        return 1
    except PermissionError as exc:
        print(
            f"ERROR: permission denied reading tests config under REPO_ROOT={repo_root!r}: {exc}",
            file=sys.stderr,
        )
        return 1
    except Exception as exc:
        if yaml is not None and isinstance(exc, yaml.YAMLError):
            print(
                f"ERROR: invalid YAML in tests config ({cfg}): {exc}. Fix indentation/quoting in the file.",
                file=sys.stderr,
            )
            return 1
        raise

    print(f"TESTS={csv!r} selection={sorted(selected)} -> {flags}")
    results_base = Path(os.environ.get("RESULTS_DIR", "/tekton/results")).resolve()
    for key, val in flags.items():
        path_var = f"{key}_PATH"
        p = os.environ.get(path_var, "").strip()
        if not p:
            print(f"Missing env {path_var} for result {key}", file=sys.stderr)
            return 1
        result_path = Path(p).resolve()
        if not result_path.is_relative_to(results_base):
            print(
                f"ERROR: {path_var}={p!r} resolves outside allowed results directory {results_base}",
                file=sys.stderr,
            )
            return 1
        try:
            result_path.parent.mkdir(parents=True, exist_ok=True)
            result_path.write_text("true" if val else "false", encoding="utf-8")
        except (FileNotFoundError, PermissionError, OSError) as exc:
            print(
                f"ERROR: could not write result file {path_var}={p!r}: {exc}",
                file=sys.stderr,
            )
            return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
