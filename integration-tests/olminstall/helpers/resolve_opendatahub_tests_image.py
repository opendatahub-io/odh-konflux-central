#!/usr/bin/env python3
"""Resolve quay.io/opendatahub/opendatahub-tests image reference for BVT.

Tag rules mirror Jenkins vars/validateHealth.groovy (RHOAI_VERSION -> image tag).
Writes the resolved image reference to RESULT_PATH for Tekton results.

Env: OPERATOR_VERSION, OPENDATAHUB_TESTS_REPO (default quay.io/opendatahub/opendatahub-tests),
     RESULT_PATH (required -- Tekton result file).
"""

from __future__ import annotations

import os
import re
import shutil
import sys
from pathlib import Path

_OLMINSTALL = Path(__file__).resolve().parent.parent
if str(_OLMINSTALL) not in sys.path:
    sys.path.insert(0, str(_OLMINSTALL))

from helpers.tekton_util import require_env, run, write_result

_EA_RE = re.compile(r"^(\d+)\.(\d+)\.\d+-ea\.(\d+)$")
_MAJOR_MINOR_RE = re.compile(r"^(\d+)\.(\d+)\.")


def main() -> int:
    result_path = require_env("RESULT_PATH")
    repo = os.environ.get("OPENDATAHUB_TESTS_REPO", "").strip() or "quay.io/opendatahub/opendatahub-tests"
    csv_version = os.environ.get("OPERATOR_VERSION", "").strip()
    latest_img = f"{repo}:latest"

    if not csv_version or csv_version == "latest":
        print(f"No CSV version (empty or latest) -- using {latest_img}")
        write_result(result_path, latest_img)
        return 0

    tag = ""
    m = _EA_RE.match(csv_version)
    if m:
        tag = f"{m.group(1)}.{m.group(2)}ea{m.group(3)}"
        print(f"EA CSV version {csv_version} -> image tag {tag}")
    else:
        m = _MAJOR_MINOR_RE.match(csv_version)
        if m:
            tag = f"{m.group(1)}.{m.group(2)}"
            print(f"CSV version {csv_version} -> image tag {tag} (major.minor)")
        else:
            print(f"Unrecognized CSV version format: {csv_version} -- using {latest_img}")
            write_result(result_path, latest_img)
            return 0

    candidate = f"{repo}:{tag}"
    skopeo = shutil.which("skopeo")
    if not skopeo:
        print(f"skopeo not found in PATH -- using {latest_img}")
        write_result(result_path, latest_img)
        return 0

    probe = run(
        [skopeo, "inspect", f"docker://{candidate}", "--no-tags"],
        check=False,
        capture=True,
    )
    if probe.returncode == 0:
        print(f"opendatahub-tests tag exists: {candidate}")
        write_result(result_path, candidate)
    else:
        print(f"Tag not found for {candidate} -- falling back to {latest_img}")
        write_result(result_path, latest_img)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
