#!/usr/bin/env python3
"""Parse JUnit XML files and write a Konflux-standardised TEST_OUTPUT result.

Env (required):
    TEST_OUTPUT_PATH    -- Tekton result file for the JSON summary
    ARTIFACTS_URL_PATH  -- Tekton result file for the artifact browser URL
    ARTIFACT_BROWSER_BASE -- base URL for the artifact browser (no trailing slash)
    PR_NAME             -- PipelineRun name
    OCI_TAG_SUFFIX      -- tag suffix (e.g. "bvt" or "bvt-env")
Env (optional):
    ARTIFACT_BROWSER_REPO_PATH -- path segment in browser (default odh-ci-artifacts)
    ARTIFACTS_DIR       -- directory containing JUnit XML (default /artifacts)
    NOTE_PREFIX         -- prefix for the note field (default "BVT")
"""

from __future__ import annotations

import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

_OLMINSTALL = Path(__file__).resolve().parent.parent
if str(_OLMINSTALL) not in sys.path:
    sys.path.insert(0, str(_OLMINSTALL))

from helpers.tekton_util import parse_junit_summary, require_env, write_result


def main() -> int:
    test_output_path = require_env("TEST_OUTPUT_PATH")
    artifacts_url_path = require_env("ARTIFACTS_URL_PATH")
    browser_base = require_env("ARTIFACT_BROWSER_BASE")
    repo_path = os.environ.get("ARTIFACT_BROWSER_REPO_PATH", "odh-ci-artifacts").strip().strip("/")
    pr_name = require_env("PR_NAME")
    oci_tag_suffix = require_env("OCI_TAG_SUFFIX")
    artifacts_dir = os.environ.get("ARTIFACTS_DIR", "/artifacts").strip()
    note_prefix = os.environ.get("NOTE_PREFIX", "BVT").strip()

    s = parse_junit_summary(artifacts_dir)
    result = "SUCCESS" if (s["failures"] + s["errors"]) == 0 else "FAILURE"
    note = f"{note_prefix}: {s['passed']}/{s['total']} passed, {s['failures']} failed, {s['errors']} errors, {s['skipped']} skipped"
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    output = json.dumps({
        "result": result,
        "timestamp": ts,
        "failures": s["failures"] + s["errors"],
        "warnings": 0,
        "successes": s["passed"],
        "note": note,
    }, separators=(",", ":"))
    write_result(test_output_path, output)
    print(note)

    base = browser_base.rstrip("/")
    artifacts_url = f"{base}/{repo_path}/{pr_name}-{oci_tag_suffix}/"
    write_result(artifacts_url_path, artifacts_url)
    print(f"Artifacts: {artifacts_url}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
