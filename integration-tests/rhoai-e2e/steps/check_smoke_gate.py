#!/usr/bin/env python3
"""Smoke Tekton gate — standard TEST_OUTPUT check (allows WARNING partial pass)."""

from __future__ import annotations

from pathlib import Path

from steps.check_test_output_gate import run_test_output_gate
from steps.tekton_util import require_env


def main() -> int:
    return run_test_output_gate(
        Path(require_env("TEST_OUTPUT_PATH")),
        gate_label="Smoke",
    )


if __name__ == "__main__":
    raise SystemExit(main())
