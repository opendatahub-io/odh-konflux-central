#!/usr/bin/env python3
"""Write ARTIFACTS_URL Tekton result after tests-payload OCI upload.

Env (required):
    ARTIFACTS_URL_PATH
    ARTIFACT_BROWSER_BASE
    PR_NAME
Env (optional):
    ARTIFACT_BROWSER_REPO_PATH -- default odh-ci-artifacts
    TESTS_PAYLOAD_DIR -- tests-payload root (default /workspace/tests-shared/tests-payload)
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

from _bootstrap import ensure_rhoai_e2e_path

ensure_rhoai_e2e_path()

from steps.tests_payload import oci_upload_marker, resolve_tests_payload_root
from steps.tekton_util import require_env, write_result
from runners.report.test_artifacts import artifacts_browser_run_url


def write_artifacts_url_result(
    *,
    artifacts_url_path: str,
    pr_name: str,
    browser_base: str,
    repo_path: str,
    tests_payload_dir: Path,
) -> str:
    """Write browser URL when upload marker exists; otherwise empty."""
    marker = oci_upload_marker(tests_payload_dir)
    if not marker.is_file():
        write_result(artifacts_url_path, "")
        return ""
    url = artifacts_browser_run_url(pr_name, browser_base=browser_base, repo_path=repo_path)
    write_result(artifacts_url_path, url)
    return url


def main() -> int:
    artifacts_url_path = require_env("ARTIFACTS_URL_PATH")
    browser_base = require_env("ARTIFACT_BROWSER_BASE")
    repo_path = os.environ.get("ARTIFACT_BROWSER_REPO_PATH", "odh-ci-artifacts").strip().strip("/")
    pr_name = require_env("PR_NAME")
    payload_raw = os.environ.get("TESTS_PAYLOAD_DIR", "").strip()
    tests_payload_dir = (
        resolve_tests_payload_root(Path(payload_raw))
        if payload_raw
        else resolve_tests_payload_root(Path("/workspace/tests-shared"))
    )

    url = write_artifacts_url_result(
        artifacts_url_path=artifacts_url_path,
        pr_name=pr_name,
        browser_base=browser_base,
        repo_path=repo_path,
        tests_payload_dir=tests_payload_dir,
    )
    if url:
        print(f"Artifacts: {url}")
    else:
        print("Artifacts: (not published)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
