#!/usr/bin/env python3
"""Resolve distributed-workloads-tests image (Jenkins :latest parity)."""

from __future__ import annotations

import os

from _bootstrap import ensure_olminstall_path

ensure_olminstall_path()

from steps.resolve_opendatahub_tests_image import resolve_csv_version_for_tests_image
from steps.tekton_util import require_env, write_result
from suite.resolve_versioned_image import resolve_versioned_image

_DEFAULT_REPO = "quay.io/opendatahub/distributed-workloads-tests"


def main() -> int:
    result_path = require_env("RESULT_PATH")
    repo = os.environ.get("DISTRIBUTED_WORKLOADS_TESTS_REPO", "").strip() or _DEFAULT_REPO
    csv_version = resolve_csv_version_for_tests_image()
    resolved = resolve_versioned_image(repo, csv_version)
    write_result(result_path, resolved)
    print(
        f"Using distributed-workloads-tests image: {resolved} "
        f"(CSV {csv_version or 'latest'})"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
