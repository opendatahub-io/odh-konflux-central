"""Shared JUnit helpers for component test runners (pytest, golang, cypress)."""

import sys
import time
from pathlib import Path
from typing import Any
from xml.sax.saxutils import escape, quoteattr

from suite.cluster_api_health import is_definitive_infra_error


def prereq_junit_outcome(reason: str) -> str:
    """JUnit testcase outcome for prereq blocks: hard infra failures vs soft skips."""
    return "failure" if is_definitive_infra_error(reason) else "skip"


def write_single_failure_junit(
    comp: dict[str, str],
    *,
    artifacts_dir: Path,
    testcase_name: str,
    message: str,
    time_seconds: float = 0.0,
    outcome: str = "failure",
) -> None:
    """Write a synthetic JUnit XML file with a single failure or skip testcase."""
    prefix = comp.get("artifact_prefix", "").strip()
    if not prefix:
        return
    junit_path = artifacts_dir / f"{prefix}.xml"
    if junit_path.exists():
        return
    cid = comp.get("id", "unknown")
    cid_attr = quoteattr(cid)
    testcase_attr = quoteattr(testcase_name)
    message_attr = quoteattr(message)
    time_attr = quoteattr(f"{time_seconds:g}")
    is_skip = outcome == "skip"
    failures_attr = "0" if is_skip else "1"
    skipped_attr = "1" if is_skip else "0"
    body = (
        f"    <skipped message={message_attr}>{escape(message)}</skipped>\n"
        if is_skip
        else f"    <failure message={message_attr}>{escape(message)}</failure>\n"
    )
    xml = (
        '<?xml version="1.0" encoding="utf-8"?>\n'
        f"<testsuite name={cid_attr} tests=\"1\" failures=\"{failures_attr}\" errors=\"0\" skipped=\"{skipped_attr}\" "
        f"time={time_attr} timestamp=\"{time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())}\">\n"
        f"  <testcase classname={cid_attr} name={testcase_attr} time={time_attr}>\n"
        f"{body}"
        "  </testcase>\n"
        "</testsuite>\n"
    )
    junit_path.write_text(xml, encoding="utf-8")
    kind = "skip" if is_skip else "failure"
    print(
        f"WARN: wrote synthetic {kind} JUnit for {comp.get('id', '?')} at {junit_path}",
        file=sys.stderr,
        flush=True,
    )
