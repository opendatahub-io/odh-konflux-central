#!/usr/bin/env python3
"""Stage target-cluster kubeconfig at /credentials/kubeconfig for install/test tasks.

Env:
    CLUSTER_SOURCE     -- EPHC, or tenant Secret with key kubeconfig (external cluster)
    EPHC_KUBECONFIG_REL -- path under /credentials from EPHC get-kubeconfig step
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

from _bootstrap import ensure_rhoai_e2e_path

ensure_rhoai_e2e_path()

from steps.external_kubeconfig_mount import copy_external_kubeconfig_mount  # noqa: E402
from steps.prepare_diagnostics_kubeconfig import _fetch_external_kubeconfig, _namespace  # noqa: E402
from suite.its_trigger_params import external_kubeconfig_secret_name, is_s3_install_cluster_source  # noqa: E402

_CREDENTIALS = Path("/credentials")
_KUBECONFIG = _CREDENTIALS / "kubeconfig"


def _write_secure(path: Path, data: bytes) -> None:
    with open(path, "wb", opener=lambda p, flags: os.open(p, flags, 0o600)) as handle:
        os.fchmod(handle.fileno(), 0o600)
        handle.write(data)


def _stage_from_tests_shared(dest: Path) -> int:
    tests_shared = (os.environ.get("TESTS_SHARED", "") or "").strip()
    if not tests_shared:
        print("ERROR: TESTS_SHARED required to stage external kubeconfig from tests-shared", file=sys.stderr)
        return 1
    src = Path(tests_shared) / "credentials" / "kubeconfig"
    if not src.is_file():
        print(
            f"ERROR: external kubeconfig missing at {src} (expected external-cluster-ready to stage it)",
            file=sys.stderr,
        )
        return 1
    _write_secure(dest, src.read_bytes())
    print(f"External kubeconfig staged at {dest} (from tests-shared)")
    return 0


def main() -> int:
    cluster_source = (os.environ.get("CLUSTER_SOURCE", "") or "").strip()
    external = external_kubeconfig_secret_name(cluster_source)
    ephc_rel = (os.environ.get("EPHC_KUBECONFIG_REL") or "").strip()
    _CREDENTIALS.mkdir(parents=True, exist_ok=True)

    if is_s3_install_cluster_source(cluster_source):
        return _stage_from_tests_shared(_KUBECONFIG)

    if external:
        if copy_external_kubeconfig_mount(_KUBECONFIG):
            return 0
        if (os.environ.get("TESTS_SHARED", "") or "").strip():
            if _stage_from_tests_shared(_KUBECONFIG) == 0:
                return 0
        ns = _namespace()
        if not ns:
            print("ERROR: namespace required to read external kubeconfig secret", file=sys.stderr)
            return 1
        try:
            content = _fetch_external_kubeconfig(external, ns)
        except (OSError, ValueError, RuntimeError) as exc:
            print(f"ERROR: could not load external kubeconfig from secret/{external}: {exc}", file=sys.stderr)
            return 1
        _write_secure(_KUBECONFIG, content.encode("utf-8"))
        print(f"External kubeconfig staged at {_KUBECONFIG}")
        return 0

    if ephc_rel:
        src = _CREDENTIALS / ephc_rel
        if not src.is_file():
            print(f"ERROR: EPHC kubeconfig missing at {src}", file=sys.stderr)
            return 1
        if src.resolve() != _KUBECONFIG.resolve():
            _write_secure(_KUBECONFIG, src.read_bytes())
        print(f"EPHC kubeconfig staged at {_KUBECONFIG} (from {ephc_rel})")
        return 0

    print("ERROR: set CLUSTER_SOURCE (tenant Secret name) or EPHC_KUBECONFIG_REL", file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
