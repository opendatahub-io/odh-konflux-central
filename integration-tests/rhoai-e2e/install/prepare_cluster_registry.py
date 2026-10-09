#!/usr/bin/env python3
"""Tekton step: prepare cluster registry (IDMS/Kyverno + pull-secret) before dep-operators."""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

from install.cluster_registry import ensure_cluster_registry_for_rhoai
from suite.errors import AppError

QUAY_SECRET_PATH = Path(os.environ.get("QUAY_PULL_SECRET_PATH", "/var/secret/quay/.dockerconfigjson"))


def main() -> int:
    product = os.environ.get("PRODUCT", "").strip()
    quay: dict | None = None
    if QUAY_SECRET_PATH.is_file():
        quay = json.loads(QUAY_SECRET_PATH.read_text(encoding="utf-8"))
    elif os.environ.get("QUAY_PULL_SECRET_NAME", "").strip():
        print(f"WARN: QUAY_PULL_SECRET_NAME set but {QUAY_SECRET_PATH} not mounted", file=sys.stderr)

    print(f"prepare-cluster-registry: product={product}")
    ensure_cluster_registry_for_rhoai(quay, product=product, quay_path=str(QUAY_SECRET_PATH))
    print("✓ prepare-cluster-registry completed")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except AppError as exc:
        print(f"ERROR {exc}", file=sys.stderr)
        raise SystemExit(exc.code or 1) from exc
