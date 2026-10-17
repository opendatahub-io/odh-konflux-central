"""Local operator cleanup (--cleanup maintenance) without triggering Konflux."""

from __future__ import annotations

from pathlib import Path

from install.operator_install_scripts_checkout import resolve_olminstall_dir
from install.run_operator_install_cleanup import run_cleanup_operator
from k8s.external_kubeconfig import (
    _tenant_secret_kubeconfig_file,
    materialize_kubeconfig_for_tekton,
    validate_kubeconfig_path,
    verify_external_cluster_login,
)
from suite.errors import AppError


class RunnerCleanupMixin:
    def run_operator_cleanup(self) -> int:
        """Run rhoai-e2e ``cleanup.sh -t operator`` on an external cluster locally."""
        ext_path = getattr(self.args, "external_kubeconfig_path", None)
        if ext_path is not None:
            path = validate_kubeconfig_path(str(ext_path))
            kubeconfig, ephemeral = materialize_kubeconfig_for_tekton(
                path,
                preferred_context=(
                    getattr(self.args, "external_kubeconfig_context", "") or ""
                ).strip(),
            )
            return self._execute_operator_cleanup(kubeconfig, ephemeral)

        secret = (getattr(self.args, "external_kubeconfig_secret", "") or "").strip()
        if not secret:
            raise AppError(
                "--cleanup requires --external-kubeconfig or --external-kubeconfig-secret.",
                2,
            )
        namespace = (getattr(self.args, "namespace", "") or "").strip()
        with _tenant_secret_kubeconfig_file(namespace=namespace, secret_name=secret) as kc:
            if kc is None or not kc.is_file():
                raise AppError(
                    f"Could not read kubeconfig from secret {namespace}/{secret}.",
                    2,
                )
            kubeconfig, ephemeral = materialize_kubeconfig_for_tekton(kc)
            return self._execute_operator_cleanup(kubeconfig, ephemeral)

    def _execute_operator_cleanup(self, kubeconfig: Path, ephemeral: bool) -> int:
        try:
            olm_dir = resolve_olminstall_dir(marker_script="cleanup.sh")
            who = verify_external_cluster_login(kubeconfig)
            print(f"External cluster preflight OK as {who}")
            print(f"Running operator cleanup (rhoai-e2e repo: {olm_dir})...", flush=True)
            run_cleanup_operator(operator_install_dir=olm_dir, kubeconfig=kubeconfig)
            print("Operator cleanup finished.", flush=True)
            return 0
        finally:
            if ephemeral:
                kubeconfig.unlink(missing_ok=True)
