#!/usr/bin/env python3
"""Resolve quay.io/opendatahub/opendatahub-operator-e2e image from installed CSV.

Platform and spark_operator golang smokes must match the cluster operator build,
not :main (which expects CRDs/APIs from newer EA builds).

Env: OPERATOR_VERSION, OPERATOR_E2E_REPO (default quay.io/opendatahub/opendatahub-operator-e2e),
     RESULT_PATH (required Tekton result file).
"""

from __future__ import annotations

import os

from _bootstrap import ensure_rhoai_e2e_path

ensure_rhoai_e2e_path()

from steps.resolve_opendatahub_tests_image import resolve_csv_version_for_tests_image
from steps.tekton_util import require_env, write_result
from suite.resolve_versioned_image import resolve_versioned_image

_DEFAULT_REPO = "quay.io/opendatahub/opendatahub-operator-e2e"


def main() -> int:
    result_path = require_env("RESULT_PATH")
    repo = os.environ.get("OPERATOR_E2E_REPO", "").strip() or _DEFAULT_REPO
    csv_version = resolve_csv_version_for_tests_image()
    if csv_version:
        print(f"Using operator version {csv_version!r} for operator-e2e image resolve")
    resolved = resolve_versioned_image(repo, csv_version)
    write_result(result_path, resolved)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
