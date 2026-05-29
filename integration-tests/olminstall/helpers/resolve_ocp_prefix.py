#!/usr/bin/env python3
"""Write the OCP minor prefix step result for EaaS pick-version.

Env:
  OVERRIDE — optional ``OCP_VERSION_PREFIX`` from the pipeline (whitespace stripped).
  DEFAULT_MINOR — first supported minor from ``eaas-get-supported-versions`` (e.g. ``4.20``).
  PREFIX_RESULT_PATH — Tekton ``step.results.prefix`` file path.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

_OLMINSTALL = Path(__file__).resolve().parent.parent
if str(_OLMINSTALL) not in sys.path:
    sys.path.insert(0, str(_OLMINSTALL))

from helpers.tekton_util import require_env, write_result


def compute_prefix(override_raw: str, default_minor: str) -> str:
    """Match legacy bash: empty override → ``{default_minor}.``; else strip spaces and ensure trailing ``.``."""
    override = "".join(override_raw.split())
    if not override:
        return f"{default_minor}."
    return override if override.endswith(".") else f"{override}."


def main() -> int:
    override_raw = os.environ.get("OVERRIDE", "")
    default_minor = require_env("DEFAULT_MINOR").strip()
    if not default_minor:
        print("DEFAULT_MINOR is empty after trim", file=sys.stderr)
        return 1
    result_path = require_env("PREFIX_RESULT_PATH")
    write_result(result_path, compute_prefix(override_raw, default_minor))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
