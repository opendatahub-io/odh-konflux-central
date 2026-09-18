#!/usr/bin/env python3
"""Refresh external-cluster kubeconfig from Konflux htpasswd credentials (RHOAIENG-57718-b)."""

from __future__ import annotations

import os
import shutil
import sys
from pathlib import Path

from steps.tekton_incluster import namespace_from_env
from k8s.external_credentials import (
    external_credentials_secret_name,
    refresh_working_kubeconfig_from_credentials,
    update_external_kubeconfig_secret,
)
from k8s.external_kubeconfig import sync_external_kubeconfig_secret_cluster_metadata, verify_external_cluster_login
from suite.errors import AppError
from suite.its_trigger_params import is_external_cluster_source


def _env_path(name: str, default: str) -> Path:
    raw = (os.environ.get(name) or default).strip()
    return Path(raw or default)


def _sync_to_tests_shared(work_path: Path) -> None:
    tests_shared = (os.environ.get("TESTS_SHARED", "") or "").strip()
    if not tests_shared:
        return
    dest = Path(tests_shared) / "credentials" / "kubeconfig"
    dest.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(work_path, dest)
    try:
        dest.chmod(0o644)
    except OSError:
        pass
    print(f"Staged refreshed kubeconfig at {dest}")


def refresh_external_kubeconfig() -> int:
    cluster_source = os.environ.get("CLUSTER_SOURCE", "").strip()
    if not is_external_cluster_source(cluster_source):
        return 0

    namespace = namespace_from_env(required=True)
    bootstrap_path = _env_path("KUBECONFIG_BOOTSTRAP", "/credentials/bootstrap/kubeconfig")
    work_path = _env_path("KUBECONFIG", "/credentials/kubeconfig")
    creds_override = os.environ.get("EXTERNAL_CREDENTIALS_SECRET", "")
    creds_secret = external_credentials_secret_name(
        cluster_source,
        override=creds_override,
    )

    refreshed = False
    refresh_source = ""
    try:
        refreshed, refresh_source = refresh_working_kubeconfig_from_credentials(
            namespace=namespace,
            cluster_source=cluster_source,
            bootstrap_path=bootstrap_path,
            work_path=work_path,
            credentials_secret_override=creds_override,
        )
    except AppError as exc:
        print(str(exc), file=sys.stderr)
        return 1

    if refreshed:
        if refresh_source:
            print(f"Refreshed external kubeconfig via {refresh_source}")
        else:
            print(f"Refreshed external kubeconfig via htpasswd Secret {creds_secret!r}")
    elif bootstrap_path.is_file():
        print(
            f"No credentials Secret {creds_secret!r}; using bootstrap kubeconfig from {cluster_source!r}"
        )
        work_path.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(bootstrap_path, work_path)
        work_path.chmod(0o600)
    else:
        print(
            f"ERROR: no credentials Secret {creds_secret!r} and no bootstrap kubeconfig at {bootstrap_path}",
            file=sys.stderr,
        )
        return 1

    try:
        who = verify_external_cluster_login(work_path)
    except AppError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    print(f"External cluster login OK after refresh: {who}")

    if refreshed:
        try:
            update_external_kubeconfig_secret(
                namespace=namespace,
                secret_name=cluster_source,
                kubeconfig_path=str(work_path),
            )
            sync_external_kubeconfig_secret_cluster_metadata(
                namespace=namespace,
                secret_name=cluster_source,
                kubeconfig_path=work_path,
            )
        except AppError as exc:
            print(
                f"ERROR: refreshed kubeconfig but failed to write back to Secret {cluster_source!r}: {exc}",
                file=sys.stderr,
            )
            return 1

    _sync_to_tests_shared(work_path)
    return 0


def main() -> int:
    try:
        return refresh_external_kubeconfig()
    except AppError as exc:
        print(str(exc), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
