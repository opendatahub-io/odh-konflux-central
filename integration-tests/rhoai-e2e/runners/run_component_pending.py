#!/usr/bin/env python3
"""Run a catalog component whose Konflux runner is not yet implemented (pending)."""

from __future__ import annotations

import fcntl
import json
import os
import sys
from pathlib import Path
from xml.sax.saxutils import escape, quoteattr


def _artifacts_dir() -> Path:
    raw = os.environ.get("ARTIFACTS_DIR", "").strip()
    if raw:
        return Path(raw)
    return Path("/artifacts")


def _pending_metadata(plan_path: Path, filter_id: str) -> tuple[str, str]:
    plan_raw = json.loads(plan_path.read_text(encoding="utf-8"))
    for item in plan_raw.get("components") or []:
        if not isinstance(item, dict) or item.get("id") != filter_id:
            continue
        artifact_prefix = str(item.get("artifact_prefix") or f"{filter_id.replace('_', '-')}-smoke").strip()
        runner = item.get("runner")
        if isinstance(runner, dict):
            phase_commands = runner.get("phaseCommands")
            if isinstance(phase_commands, dict):
                for key in ("smoke", "tier1"):
                    msg = phase_commands.get(key)
                    if isinstance(msg, str) and msg.strip():
                        return msg.strip(), artifact_prefix
        return "Component test not yet implemented in Konflux.", artifact_prefix
    return "Component test not yet implemented in Konflux.", f"{filter_id.replace('_', '-')}-smoke"


def _write_skip_junit(artifacts_dir: Path, *, component_id: str, artifact_prefix: str, message: str) -> None:
    junit_path = artifacts_dir / f"{artifact_prefix}.xml"
    artifacts_dir.mkdir(parents=True, exist_ok=True)
    component_attr = quoteattr(component_id)
    message_attr = quoteattr(message)
    junit_path.write_text(
        "\n".join(
            [
                '<?xml version="1.0" encoding="UTF-8"?>',
                f"<testsuite name={component_attr} tests=\"1\" failures=\"1\" errors=\"0\" skipped=\"0\">",
                f"  <testcase classname={component_attr} name=\"pending_runner\">",
                f"    <failure message={message_attr}>{escape(message)}</failure>",
                "  </testcase>",
                "</testsuite>",
                "",
            ]
        ),
        encoding="utf-8",
    )


def main() -> int:
    filter_id = os.environ.get("COMPONENT_TEST_COMPONENT_ID", "").strip()
    plan_path_raw = os.environ.get("COMPONENT_TEST_PLAN_JSON", "").strip()
    if not filter_id or not plan_path_raw:
        print("COMPONENT_TEST_COMPONENT_ID and COMPONENT_TEST_PLAN_JSON are required", file=sys.stderr)
        return 2
    plan_path = Path(plan_path_raw)
    if not plan_path.is_file():
        print(f"ERROR: component plan missing: {plan_path}", file=sys.stderr)
        return 2

    message, artifact_prefix = _pending_metadata(plan_path, filter_id)
    artifacts_dir = _artifacts_dir()
    _write_skip_junit(artifacts_dir, component_id=filter_id, artifact_prefix=artifact_prefix, message=message)
    exit_path = artifacts_dir / "component-test.exit"
    artifacts_dir.mkdir(parents=True, exist_ok=True)
    with open(exit_path, "a+", encoding="ascii") as fh:
        fcntl.flock(fh, fcntl.LOCK_EX)
        fh.seek(0)
        try:
            prev = int(fh.read().strip() or "0")
        except ValueError:
            prev = 0
        fh.seek(0)
        fh.truncate()
        fh.write(str(max(prev, 1)))
    print(f"FAIL pending {filter_id}: {message}", flush=True)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
