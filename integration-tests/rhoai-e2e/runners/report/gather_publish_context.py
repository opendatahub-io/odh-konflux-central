#!/usr/bin/env python3
"""Gather TaskRun facts for publish-results (Slack and future external portals).

Writes Tekton results consumed by send-notification and pipeline-run-summary.
External portal/Jira URLs are placeholders until those integrations exist.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

from _bootstrap import ensure_rhoai_e2e_path

ensure_rhoai_e2e_path()

from runners.report.publish_context_load import load_publish_context  # noqa: E402
from steps.tekton_util import write_result  # noqa: E402


def _write_optional(path_key: str, value: str) -> None:
    path = os.environ.get(path_key, "").strip()
    if path:
        write_result(path, value)


def _read_existing_result(path_key: str) -> str:
    path = os.environ.get(path_key, "").strip()
    if not path:
        return ""
    try:
        return Path(path).read_text(encoding="utf-8").strip()
    except OSError:
        return ""


def main() -> int:
    ctx = load_publish_context()
    if ctx is None:
        print("PIPELINE_RUN_NAME or namespace missing", file=sys.stderr)
        return 1

    _write_optional("PIPELINE_RUN_NAME_PATH", ctx.pipeline_run)
    _write_optional("TEST_STATUS_PATH", ctx.pipeline_status)
    _write_optional("FBCF_IMAGE_PATH", ctx.fbcf_image)
    _write_optional("OPERATOR_VERSION_PATH", ctx.operator_version)
    artifacts = _read_existing_result("ARTIFACTS_URL_PATH") or ctx.artifacts_url
    _write_optional("ARTIFACTS_URL_PATH", artifacts)
    _write_optional("REPORT_PORTAL_URL_PATH", "")
    _write_optional("JIRA_URL_PATH", "")

    print(
        f"Publish context: status={ctx.pipeline_status} operator={ctx.operator_version} "
        f"artifacts={'yes' if ctx.artifacts_url else 'no'}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
