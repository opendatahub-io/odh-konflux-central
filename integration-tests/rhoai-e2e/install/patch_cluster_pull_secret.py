#!/usr/bin/env python3
"""
Create openshift-marketplace pull secrets for OLM (rhoai-quay-pull).

Cluster-wide registry mirror + global pull-secret merge run earlier in
prepare-cluster-registry. This step is idempotent OLM SA wiring only.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

from install.cluster_registry import (
    dockerconfig_pull_secret_apply_manifest,
    extract_quay_auth,
    merge_docker_auths,
)

__all__ = [
    "dockerconfig_pull_secret_apply_manifest",
    "extract_quay_auth",
    "merge_docker_auths",
    "ensure_olm_marketplace_pull_secrets",
    "main",
]
from k8s.oc_util import run_oc
from suite.errors import AppError

QUAY_SECRET_PATH = Path("/var/secret/quay/.dockerconfigjson")


def ensure_olm_marketplace_pull_secrets(quay: dict) -> int:
    auths = quay.get("auths") or {}
    if not extract_quay_auth(auths):
        print(f"❌ No quay.io/rhoai auth token found in mounted secret")
        return 1

    print("Creating rhoai-quay-pull imagePullSecret in openshift-marketplace for OLM SA-level pulls...")
    quay_json = json.dumps(quay, separators=(",", ":"))
    run_oc(
        ["apply", "-f", "-"],
        stdin_text=dockerconfig_pull_secret_apply_manifest("rhoai-quay-pull", "openshift-marketplace", quay_json),
        check=True,
    )

    ls = run_oc(
        ["get", "sa", "-n", "openshift-marketplace", "--no-headers", "-o", "custom-columns=:metadata.name"],
        check=False,
    )
    if ls.returncode != 0:
        print("⚠ Failed to list service accounts in openshift-marketplace")
        return 0
    failures: list[str] = []
    successes = 0
    for line in ls.stdout.splitlines():
        name = line.strip()
        if not name:
            continue
        cp = run_oc(
            ["secrets", "link", name, "rhoai-quay-pull", "-n", "openshift-marketplace", "--for=pull"],
            check=False,
        )
        if cp.returncode != 0:
            failures.append(f"{name}: {(cp.stderr or cp.stdout or '').strip()}")
        else:
            successes += 1
    if failures:
        print("❌ Failed linking rhoai-quay-pull to some SAs:", file=sys.stderr)
        for item in failures:
            print(f"  {item}", file=sys.stderr)
        return 1
    if successes > 0:
        print("✓ rhoai-quay-pull linked to all SAs in openshift-marketplace")
    else:
        print("⚠ No service accounts found in openshift-marketplace")
    return 0


def main() -> int:
    if not QUAY_SECRET_PATH.is_file():
        print(f"❌ Quay secret not mounted at {QUAY_SECRET_PATH}")
        return 1
    quay = json.loads(QUAY_SECRET_PATH.read_text(encoding="utf-8"))
    return ensure_olm_marketplace_pull_secrets(quay)


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except AppError as exc:
        print(f"ERROR {exc}", file=sys.stderr)
        raise SystemExit(exc.code or 1) from exc
