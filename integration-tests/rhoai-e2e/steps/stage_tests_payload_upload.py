#!/usr/bin/env python3
"""Stage filtered JUnit/log files for OCI upload (excludes tool binaries and workspace junk).

Reads ``artifactUpload`` from rhoai-e2e-tests-config.yaml unless overridden by env.

Env:
    TESTS_PAYLOAD_DIR   -- tests-payload root (required)
    TESTS_CONFIG_PATH   -- optional path to rhoai-e2e-tests-config.yaml
    OCI_UPLOAD_SUBDIR   -- optional override for artifactUpload.ociSubdir
    OCI_UPLOAD_PATTERNS -- optional comma-separated fnmatch patterns
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

from _bootstrap import ensure_rhoai_e2e_path

ensure_rhoai_e2e_path()

from suite.constants import default_tests_config_path
from suite.tests_config import load_artifact_upload_config
from steps.tests_payload import (
    collect_upload_files,
    resolve_tests_payload_root,
    stage_tests_payload_for_upload,
)


def _config_path() -> Path:
    raw = os.environ.get("TESTS_CONFIG_PATH", "").strip()
    if raw:
        return Path(raw)
    return default_tests_config_path()


def main() -> int:
    payload_raw = os.environ.get("TESTS_PAYLOAD_DIR", "").strip()
    if not payload_raw:
        print("TESTS_PAYLOAD_DIR is required", file=sys.stderr)
        return 1
    payload_root = resolve_tests_payload_root(Path(payload_raw))

    cfg = load_artifact_upload_config(_config_path())
    subdir = os.environ.get("OCI_UPLOAD_SUBDIR", "").strip() or cfg.oci_subdir
    patterns_raw = os.environ.get("OCI_UPLOAD_PATTERNS", "").strip()
    patterns = (
        tuple(p.strip() for p in patterns_raw.split(",") if p.strip())
        if patterns_raw
        else cfg.include_patterns
    )

    files = collect_upload_files(payload_root, patterns=patterns)
    if not files:
        print("No matching JUnit/log files to upload; skipping staging")
        return 0

    staging_root = stage_tests_payload_for_upload(
        payload_root, oci_subdir=subdir, patterns=patterns
    )
    print(
        f"Staged {len(files)} file(s) under {staging_root / subdir} "
        f"(patterns: {', '.join(patterns)})"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
