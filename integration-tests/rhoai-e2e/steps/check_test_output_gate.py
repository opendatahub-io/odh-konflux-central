#!/usr/bin/env python3
"""Shared Konflux TEST_OUTPUT gate checks for BVT, smoke, and pipeline finally."""

from __future__ import annotations

import json
from pathlib import Path


def load_test_output_json(path: Path) -> tuple[dict[str, object] | None, str]:
    """Return (payload, error_message). *error_message* is empty on success."""
    if not path.is_file() or path.stat().st_size == 0:
        return None, "TEST_OUTPUT missing or empty"
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        return None, f"TEST_OUTPUT unreadable: {exc}"
    if not isinstance(data, dict):
        return None, "TEST_OUTPUT is not a JSON object"
    return data, ""


def _read_count(data: dict[str, object], *keys: str) -> tuple[int | None, str]:
    for key in keys:
        if key in data:
            value = data.get(key)
            break
    else:
        return 0, ""
    try:
        count = int(value or 0)
    except (TypeError, ValueError):
        return None, f"TEST_OUTPUT field {key!r} is not an integer"
    if count < 0:
        return None, f"TEST_OUTPUT field {key!r} is negative"
    return count, ""


def check_test_output_json(
    data: dict[str, object],
    *,
    gate_label: str,
    allow_all_skipped_note_prefix: str = "",
    strict: bool = False,
) -> tuple[int, str]:
    """Return (exit_code, message).

    Aligns Tekton step exit with Konflux TEST_OUTPUT coloring:
    - SUCCESS → pass (green)
    - WARNING → pass when at least one test passed (yellow; partial or incomplete gate)
    - FAILURE → fail when zero tests passed (red)

    When *strict* is true (BVT), any failure count blocks the gate even when some tests
    passed (no WARNING partial pass). Component smoke uses the default non-strict gate.
    """
    result = str(data.get("result", "")).strip().upper()
    note = str(data.get("note", "")).strip()
    successes, err = _read_count(data, "successes", "passed")
    if err:
        return 1, f"{gate_label} gate failed: {err}"
    failures, err = _read_count(data, "failures")
    if err:
        return 1, f"{gate_label} gate failed: {err}"
    skipped, err = _read_count(data, "skipped")
    if err:
        return 1, f"{gate_label} gate failed: {err}"
    prefix = allow_all_skipped_note_prefix.strip().lower()
    if (
        prefix
        and result != "SUCCESS"
        and successes == 0
        and failures == 0
        and skipped > 0
        and note.lower().startswith(prefix)
    ):
        return 0, note or f"{gate_label} skipped (nothing runnable)"
    if strict and failures > 0:
        summary = note or f"{failures} failed, {successes} passed"
        return 1, f"{gate_label} gate failed (strict): {summary}"
    if result == "SUCCESS":
        return 0, note or f"{gate_label} passed"
    if result == "WARNING" and successes > 0:
        return 0, note or f"{gate_label} partial pass (WARNING)"
    if result == "FAILURE" and successes == 0:
        summary = note or f"result={result}"
        return 1, f"{gate_label} gate failed ({result}): {summary}"
    summary = note or f"result={result or 'unknown'}"
    return 1, f"{gate_label} gate failed ({result or 'unknown'}): {summary}"


def check_test_output_file(
    path: Path,
    *,
    gate_label: str,
    allow_all_skipped_note_prefix: str = "",
    strict: bool = False,
) -> tuple[int, str]:
    data, err = load_test_output_json(path)
    if data is None:
        return 1, err.replace("TEST_OUTPUT", f"{gate_label} TEST_OUTPUT", 1)
    return check_test_output_json(
        data,
        gate_label=gate_label,
        allow_all_skipped_note_prefix=allow_all_skipped_note_prefix,
        strict=strict,
    )


def run_test_output_gate(
    path: Path,
    *,
    gate_label: str,
    allow_all_skipped_note_prefix: str = "",
    strict: bool = False,
) -> int:
    """Print gate message and return Tekton step exit code."""
    import sys

    ec, msg = check_test_output_file(
        path,
        gate_label=gate_label,
        allow_all_skipped_note_prefix=allow_all_skipped_note_prefix,
        strict=strict,
    )
    if ec == 0:
        print(msg, flush=True)
        return 0
    print(f"ERROR: {msg}", file=sys.stderr, flush=True)
    return ec


def main() -> int:
    """Tekton step: enforce TEST_OUTPUT gate (BVT strict or component-style partial pass).

    Env:
        TEST_OUTPUT_PATH -- Tekton result file (required)
        GATE_LABEL -- e.g. BVT, Smoke (required)
        GATE_STRICT -- when true, any failure blocks (BVT; default false)
        ALLOW_ALL_SKIPPED_NOTE_PREFIX -- e.g. bvt: for placeholder-only runs
    """
    import os
    import sys

    from steps.tekton_util import require_env

    path = Path(require_env("TEST_OUTPUT_PATH"))
    gate_label = require_env("GATE_LABEL")
    strict = os.environ.get("GATE_STRICT", "").strip().lower() in ("1", "true", "yes")
    allow_prefix = os.environ.get("ALLOW_ALL_SKIPPED_NOTE_PREFIX", "").strip()
    return run_test_output_gate(
        path,
        gate_label=gate_label,
        allow_all_skipped_note_prefix=allow_prefix,
        strict=strict,
    )


if __name__ == "__main__":
    import sys

    raise SystemExit(main())
