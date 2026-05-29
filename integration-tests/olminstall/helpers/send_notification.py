#!/usr/bin/env python3
"""Send a Slack notification summarising the pipeline run.

Env (required):
    TEST_STATUS       -- aggregate Tekton tasks status (Succeeded/Failed/Completed)
    OPERATOR_NAME     -- e.g. rhods-operator, opendatahub-operator
    PIPELINE_RUN_NAME
Env (optional):
    OPERATOR_VERSION, FBCF_IMAGE, ARTIFACTS_URL, TESTS,
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

_OLMINSTALL = Path(__file__).resolve().parent.parent
if str(_OLMINSTALL) not in sys.path:
    sys.path.insert(0, str(_OLMINSTALL))

from helpers.bvt_artifacts import resolve_artifacts_notification_line, tests_include_bvt
from helpers.tekton_util import require_env

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
    test_status = require_env("TEST_STATUS")
    operator_name = require_env("OPERATOR_NAME")
    pipeline_run = require_env("PIPELINE_RUN_NAME")
    operator_version = os.environ.get("OPERATOR_VERSION", "(unknown)").strip() or "(unknown)"
    fbcf_image = os.environ.get("FBCF_IMAGE", "(unknown)").strip() or "(unknown)"
    tests_csv = os.environ.get("TESTS", "").strip()
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
        f"{emoji} {product} olminstall integration test {status_text}",
        f"Operator : {operator_version}",
        f"FBCF     : {fbcf_image}",
        f"Run      : {pipeline_run}",
    ]
    artifacts_line = resolve_artifacts_notification_line(
        tests_csv=tests_csv,
        pipeline_run=pipeline_run,
    )
    if not artifacts_line and tests_include_bvt(tests_csv):
        predicted = os.environ.get("ARTIFACTS_URL", "").strip()
        if predicted:
            artifacts_line = f"Artifacts: {predicted}"
    if artifacts_line:
        lines.append(artifacts_line)
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
