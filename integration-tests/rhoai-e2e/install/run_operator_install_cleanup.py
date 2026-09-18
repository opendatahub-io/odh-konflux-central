"""Tekton step: run data-hub/rhoai-e2e ``cleanup.sh -t operator`` on the target cluster."""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

from _bootstrap import ensure_rhoai_e2e_path

ensure_rhoai_e2e_path()

from suite.errors import AppError
from k8s.external_kubeconfig import verify_external_cluster_login

_CLEANUP_SCRIPT = "cleanup.sh"


def _require_tool(name: str) -> None:
    if shutil.which(name) is None:
        raise AppError(
            f"cleanup-external requires {name!r} on PATH (rhoai-e2e cleanup.sh uses it).",
            2,
        )


def run_cleanup_operator(*, operator_install_dir: Path, kubeconfig: str | Path) -> None:
    """Run MaaS Postgres cleanup, leaked tenant NS cleanup, ``cleanup.sh -t operator``, then tenant NS cleanup."""
    from components.maas_billing.database import (
        cleanup_maas_postgres_infra,
        cleanup_maas_tenant_namespace,
    )
    from components.maas_billing.bbr_pre_processing import cleanup_stale_maas_ingress_workloads
    from install.leaked_tenant_namespace_cleanup import cleanup_leaked_tenant_namespaces

    maas_exc: BaseException | None = None
    cleanup_exc: AppError | None = None
    try:
        cleanup_stale_maas_ingress_workloads()
    except Exception as exc:
        print(
            f"WARN: MaaS ingress cleanup failed ({exc}); continuing with operator cleanup",
            file=sys.stderr,
            flush=True,
        )
    try:
        cleanup_maas_postgres_infra()
    except Exception as exc:
        maas_exc = exc
        print(
            f"WARN: MaaS Postgres infra cleanup failed ({exc}); continuing with operator cleanup",
            file=sys.stderr,
            flush=True,
        )
    try:
        cleanup_leaked_tenant_namespaces()
    except Exception as exc:
        print(
            f"WARN: leaked tenant namespace cleanup failed ({exc}); continuing with operator cleanup",
            file=sys.stderr,
            flush=True,
        )
    script = operator_install_dir.resolve() / _CLEANUP_SCRIPT
    if not script.is_file():
        raise AppError(f"rhoai-e2e repo missing {_CLEANUP_SCRIPT}: {script}", 2)
    try:
        _invoke_cleanup(script, kubeconfig=Path(kubeconfig))
    except AppError as exc:
        cleanup_exc = exc
        print(
            f"WARN: rhoai-e2e cleanup.sh failed ({exc}); continuing with MaaS tenant namespace cleanup",
            file=sys.stderr,
            flush=True,
        )
    try:
        cleanup_maas_tenant_namespace()
    except Exception as exc:
        if maas_exc is None:
            maas_exc = exc
        print(
            f"WARN: MaaS tenant namespace cleanup failed ({exc})",
            file=sys.stderr,
            flush=True,
        )
    if cleanup_exc is not None:
        raise cleanup_exc
    if maas_exc is not None:
        if isinstance(maas_exc, AppError):
            raise maas_exc
        raise AppError(f"MaaS infra cleanup failed: {maas_exc}", 1) from maas_exc


def _invoke_cleanup(script: Path, *, kubeconfig: Path) -> None:
    olm_dir = script.parent.resolve()
    cmd = ["bash", script.name, "-t", "operator"]
    env = os.environ.copy()
    env["KUBECONFIG"] = str(kubeconfig)
    print(f"INFO (cwd={olm_dir}) {' '.join(cmd)}", flush=True)
    proc = subprocess.run(cmd, cwd=olm_dir, env=env, check=False)
    if proc.returncode != 0:
        raise AppError(f"rhoai-e2e cleanup.sh failed (exit {proc.returncode})", proc.returncode)


def main() -> int:
    """Tekton step entrypoint: ``KUBECONFIG`` + ``OLMINSTALL_DIR`` required."""
    kubeconfig = os.environ.get("KUBECONFIG", "").strip()
    olm_dir = os.environ.get("OLMINSTALL_DIR", "").strip()
    if not kubeconfig:
        print("KUBECONFIG is required", file=sys.stderr)
        return 1
    if not olm_dir:
        print("OLMINSTALL_DIR is required", file=sys.stderr)
        return 1
    for tool in ("oc", "bash", "jq"):
        _require_tool(tool)
    try:
        who = verify_external_cluster_login(Path(kubeconfig))
        print(f"INFO Running rhoai-e2e cleanup on cluster as {who} (KUBECONFIG={kubeconfig})", flush=True)
        run_cleanup_operator(operator_install_dir=Path(olm_dir), kubeconfig=kubeconfig)
    except AppError as exc:
        print(exc, file=sys.stderr)
        return exc.code if exc.code else 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
