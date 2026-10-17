#!/usr/bin/env python3
"""HyperShift registry prep — delegates to prepare-cluster-registry logic."""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

from install.cluster_registry import ensure_cluster_registry_for_rhoai
from install.rosa_hcp_pull_setup import is_hypershift_managed_cluster, load_quay_dockerconfig


def ensure_external_cluster_hcp_registry_prep() -> None:
    if not is_hypershift_managed_cluster():
        print("Not HyperShift-managed; skipping external HCP registry prep")
        return
    quay_path = os.environ.get("QUAY_PULL_SECRET_PATH", "/var/secret/quay/.dockerconfigjson").strip()
    product = os.environ.get("PRODUCT", "").strip()
    quay = None
    if Path(quay_path).is_file():
        quay = json.loads(Path(quay_path).read_text(encoding="utf-8"))
    elif os.environ.get("QUAY_PULL_SECRET_NAME", "").strip():
        quay = load_quay_dockerconfig()
    ensure_cluster_registry_for_rhoai(quay, product=product, quay_path=quay_path)


def main() -> int:
    try:
        ensure_external_cluster_hcp_registry_prep()
    except Exception as exc:
        print(f"❌ External HCP registry prep failed: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
