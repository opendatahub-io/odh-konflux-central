#!/usr/bin/env python3
"""BVT Tekton gate — strict TEST_OUTPUT check (blocks downstream smoke on any failure)."""

from __future__ import annotations

from pathlib import Path

from steps.check_test_output_gate import check_test_output_file, run_test_output_gate
from steps.tekton_util import require_env


def check_bvt_test_output(path: Path) -> tuple[int, str]:
    """Return (exit_code, message) for BVT strict gate (shared with pipeline finalize)."""
    return check_test_output_file(
        path,
        gate_label="BVT",
        allow_all_skipped_note_prefix="bvt:",
        strict=True,
    )


def main() -> int:
    return run_test_output_gate(
        Path(require_env("TEST_OUTPUT_PATH")),
        gate_label="BVT",
        allow_all_skipped_note_prefix="bvt:",
        strict=True,
    )


if __name__ == "__main__":
    raise SystemExit(main())
