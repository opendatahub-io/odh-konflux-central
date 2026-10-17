#!/usr/bin/env python3
"""Send a Slack notification summarising the pipeline run.

Env (required):
    TEST_STATUS       -- aggregate Tekton tasks status (Succeeded/Failed/Completed)
    OPERATOR_NAME     -- e.g. rhods-operator, opendatahub-operator
    PIPELINE_RUN_NAME
Env (optional):
    OPERATOR_VERSION, FBCF_IMAGE, ARTIFACTS_URL, TEST_GATES,
    SLACK_CHANNEL_ID, SLACK_WEBHOOK_URL
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from urllib.request import Request, urlopen
from urllib.error import URLError
from urllib.parse import urlparse

from _bootstrap import ensure_rhoai_e2e_path

ensure_rhoai_e2e_path()

from runners.report.junit_suite_report import suite_lines_from_test_output
from runners.report.pipelinerun_summary import pick_pipeline_test_output
from runners.report.test_artifacts import resolve_artifacts_notification_lines
from steps.tekton_incluster import list_taskruns_in_cluster, namespace_from_env
from steps.tekton_util import read_tekton_result_env, require_env

_PRODUCT_LABELS = {
    "opendatahub-operator": "ODH",
    "rhods-operator": "RHOAI",
}


def _slack_incoming_webhook_ok(url: str) -> bool:
    p = urlparse(url.strip())
    if p.scheme != "https" or not p.netloc:
        return False
    host = p.netloc.lower().split("@")[-1]
    return host == "hooks.slack.com" and p.path.startswith("/services/")


def main() -> int:
    read_tekton_result_env(
        "TEST_STATUS", "PIPELINE_RUN_NAME", "OPERATOR_VERSION", "FBCF_IMAGE", "ARTIFACTS_URL"
    )
    test_status = require_env("TEST_STATUS")
    operator_name = require_env("OPERATOR_NAME")
    pipeline_run = require_env("PIPELINE_RUN_NAME")
    operator_version = os.environ.get("OPERATOR_VERSION", "(unknown)").strip() or "(unknown)"
    fbcf_image = os.environ.get("FBCF_IMAGE", "(unknown)").strip() or "(unknown)"
    tests_csv = os.environ.get("TEST_GATES", "").strip() or os.environ.get("TESTS", "").strip()
    components_csv = os.environ.get("COMPONENTS", "").strip()
    channel_id = os.environ.get("SLACK_CHANNEL_ID", "").strip()
    webhook_url = os.environ.get("SLACK_WEBHOOK_URL", "").strip()

    if test_status in ("Succeeded", "Completed"):
        status_text = "PASSED"
        emoji = "\u2705"
    else:
        status_text = f"FAILED (pipeline tasks status: {test_status})"
        emoji = "\u274c"

    product = _PRODUCT_LABELS.get(operator_name, "ODH/RHOAI")
    lines = [
        f"{emoji} {product} rhoai-e2e integration test {status_text}",
        f"Operator : {operator_version}",
        f"FBCF     : {fbcf_image}",
        f"Run      : {pipeline_run}",
    ]
    artifact_lines = resolve_artifacts_notification_lines(
        tests_csv=tests_csv,
        pipeline_run=pipeline_run,
    )
    lines.extend(artifact_lines)
    suite_lines: list[str] = []
    ns = namespace_from_env()
    if ns:
        taskruns = list_taskruns_in_cluster(pipeline_run, ns)
        test_output = pick_pipeline_test_output(taskruns)
        suite_lines = suite_lines_from_test_output(test_output)
        if suite_lines:
            lines.append("Suites:")
            lines.extend(suite_lines)
    if components_csv and not suite_lines:
        lines.append(f"Components: {components_csv}")
    if not artifact_lines:
        predicted = os.environ.get("ARTIFACTS_URL", "").strip()
        if predicted:
            lines.append(f"Artifacts: {predicted}")
    msg = "\n".join(lines)

    if not channel_id:
        print("SLACK_CHANNEL_ID not set -- Slack notification disabled")
        print("---")
        print(msg)
        print("---")
        return 0

    if not webhook_url:
        print("WARN: SLACK_CHANNEL_ID is set but slack-webhook secret is missing -- skipping Slack")
        return 0

    if not _slack_incoming_webhook_ok(webhook_url):
        print(
            "WARN: SLACK_WEBHOOK_URL must be https://hooks.slack.com/services/... — skipping Slack",
            file=sys.stderr,
        )
        return 0

    payload = json.dumps({"text": msg, "channel": channel_id}).encode()
    req = Request(
        webhook_url,
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urlopen(req, timeout=30) as resp:
            final_url = resp.geturl()
            if not _slack_incoming_webhook_ok(final_url):
                raise URLError(
                    f"Slack webhook redirect left allowlisted host (final URL: {final_url})"
                )
            resp.read()
        print(f"Slack notification sent to channel {channel_id}")
    except URLError as exc:
        print(f"WARN: Slack POST failed: {exc}", file=sys.stderr)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
