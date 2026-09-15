#!/usr/bin/env python3
"""Run olminstall setup-dependencies.sh for dependency operators before install or MaaS smoke.

Env (required):
    KUBECONFIG              -- path to cluster kubeconfig
    OLMINSTALL_DIR          -- cloned olminstall repo root
Env (optional):
    SETUP_DEPENDENCIES_ARGS -- extra args for setup-dependencies.sh (e.g. -M)
    COMPONENTS_CSV          -- when set, Authorino/Kuadrant must be ready after setup (MaaS smoke)
                             and/or Llama Stack DSC component is enabled (llama_stack smoke)
    OLMINSTALL_GITOPS_BRANCH -- passed as -b to setup-dependencies.sh (Jenkins GITOPS_REPO_BRANCH parity)
    PRODUCT                 -- passed from pipeline; documented for install-dep-operators task env
"""

from __future__ import annotations

import os
import shlex
import shutil
import subprocess
import sys
from pathlib import Path

from install.dependency_operators import (
    authorino_deferred_to_component_prep,
    components_csv_requires_authorino,
    ensure_jobset_and_lws_operator_crs,
    ensure_setup_dependency_namespaces_ready,
    existing_dependency_stack_ready,
    finalize_dependency_operators_after_setup_script,
    maas_dependency_operators_ready,
    patch_odh_gitops_keda_pod_selector,
    product_install_path,
    recover_authorino_after_setup_script,
    require_maas_dependency_operators,
)
from install.kserve_deps import (
    components_csv_requires_kserve_deps,
    ensure_serverless_operator,
)
from install.llama_stack_deps import (
    components_csv_requires_llama_stack,
    try_prepare_llama_stack_operator,
)
from install.dsc_install import ensure_dsc_models_as_service, _install_requires_dashboard_gateway, _smoke_components_need_servicemesh
from install.approve_transitive_installplans import approve_pending_installplans
from install.gateway_config import (
    ensure_openshift_gateway_istio_for_dep_operators,
    reconcile_servicemesh_olm_conflicts,
    wait_servicemesh_csv_succeeded,
)
from suite.its_trigger_params import is_pooled_external_cluster_source
from install.rhcl_deps import (
    ensure_maas_rhcl_dependency_stack,
    reconcile_rhcl_after_gitops_apply,
)
from steps.cluster_prep_state import mark_dep_operators_done

_GO_VERSION = "1.22.5"
_GO_INSTALL_ROOT = Path("/tmp/olminstall-go")
_TOOL_BIN_DIR = Path("/tmp/olminstall-bin")
_YQ_VERSION = "v4.44.1"


def _ensure_go_on_path(env: dict[str, str]) -> dict[str, str]:
    """odh-gitops Makefile builds kustomize via ``go install``; rhoai-task-toolset has no Go."""
    path = env.get("PATH", os.environ.get("PATH", ""))
    if shutil.which("go", path=path):
        return env

    go_bin = _GO_INSTALL_ROOT / "bin" / "go"
    if not go_bin.is_file():
        archive = f"go{_GO_VERSION}.linux-amd64.tar.gz"
        url = f"https://go.dev/dl/{archive}"
        print(f"Installing Go {_GO_VERSION} for setup-dependencies.sh (kustomize bootstrap)...", flush=True)
        subprocess.run(
            [
                "bash",
                "-c",
                (
                    "set -euo pipefail\n"
                    f"curl -fsSL '{url}' -o /tmp/{archive}\n"
                    f"rm -rf '{_GO_INSTALL_ROOT}' /tmp/go\n"
                    f"tar -C /tmp -xzf /tmp/{archive}\n"
                    f"mv /tmp/go '{_GO_INSTALL_ROOT}'\n"
                ),
            ],
            check=True,
        )

    out = dict(env)
    out["GOROOT"] = str(_GO_INSTALL_ROOT)
    out["PATH"] = f"{_GO_INSTALL_ROOT / 'bin'}:{path}"
    return out


def _ensure_yq_on_path(env: dict[str, str]) -> dict[str, str]:
    """odh-gitops TLS prep scripts require Mike Farah ``yq`` on PATH."""
    path = env.get("PATH", os.environ.get("PATH", ""))
    if shutil.which("yq", path=path):
        return env
    _TOOL_BIN_DIR.mkdir(parents=True, exist_ok=True)
    yq_bin = _TOOL_BIN_DIR / "yq"
    if not yq_bin.is_file():
        url = f"https://github.com/mikefarah/yq/releases/download/{_YQ_VERSION}/yq_linux_amd64"
        print(f"Installing yq {_YQ_VERSION} for setup-dependencies.sh...", flush=True)
        subprocess.run(["curl", "-fsSL", url, "-o", str(yq_bin)], check=True)
        yq_bin.chmod(0o755)
    out = dict(env)
    out["PATH"] = f"{_TOOL_BIN_DIR}:{path}"
    return out


def _ensure_maas_bvt_prerequisites() -> None:
    """MaaS DB secret and modelsAsService=Managed before BVT when MaaS smoke ids are selected."""
    from components.maas_billing.common import maas_api_deployment_exists
    from components.maas_billing.database import ensure_maas_database

    ensure_maas_database()
    wait_aigateway = maas_api_deployment_exists()
    if not wait_aigateway:
        print(
            "NOTE: maas-api not deployed yet; patching DSC modelsAsAService only and "
            "deferring AIGateway reconcile wait to prepare-components-prerequisites",
            flush=True,
        )
    ensure_dsc_models_as_service(wait_for_aigateway=wait_aigateway)


def _ensure_kubectl_on_path(env: dict[str, str]) -> dict[str, str]:
    """setup-dependencies.sh invokes ``kubectl``; Tekton toolset images often ship only ``oc``."""
    path = env.get("PATH", os.environ.get("PATH", ""))
    if shutil.which("kubectl", path=path):
        return env
    oc = shutil.which("oc", path=path)
    if not oc:
        print("WARN: kubectl and oc missing; setup-dependencies.sh may fail", file=sys.stderr, flush=True)
        return env
    _TOOL_BIN_DIR.mkdir(parents=True, exist_ok=True)
    kubectl = _TOOL_BIN_DIR / "kubectl"
    if not kubectl.exists():
        kubectl.symlink_to(oc)
    out = dict(env)
    out["PATH"] = f"{_TOOL_BIN_DIR}:{path}"
    return out


def _prepare_setup_env(base: dict[str, str]) -> dict[str, str]:
    """Ensure go, yq, and kubectl (oc shim) are on PATH for setup-dependencies.sh."""
    return _ensure_kubectl_on_path(_ensure_yq_on_path(_ensure_go_on_path(base)))


def _ensure_pyyaml_for_catalog() -> None:
    """install-dep-operators loads smoke catalog for llama_stack gating before tests payload exists."""
    try:
        import yaml  # noqa: F401
        return
    except ImportError:
        pass
    from components.dashboard_cypress.runtime import _ensure_pyyaml_available

    _ensure_pyyaml_available()


def _dep_operators_need_openshift_gateway_istio(components_csv: str) -> bool:
    """Gateway controller must reconcile before RHOAI install creates GatewayConfig."""
    return _install_requires_dashboard_gateway() or _smoke_components_need_servicemesh(components_csv)


def _gateway_istio_failure_fatal() -> bool:
    cluster_source = os.environ.get("CLUSTER_SOURCE", "").strip()
    if is_pooled_external_cluster_source(cluster_source):
        return True
    return os.environ.get("PRODUCT", "").strip().lower() in ("rhoai", "odh")


def _ensure_openshift_gateway_istio_stack(components_csv: str) -> None:
    if not _dep_operators_need_openshift_gateway_istio(components_csv):
        return
    try:
        removed = reconcile_servicemesh_olm_conflicts("openshift-operators")
        if removed:
            print(
                f"✓ Reconciled {removed} orphan Service Mesh CSV(s) before openshift-gateway Istio",
                flush=True,
            )
        approved = approve_pending_installplans("openshift-operators")
        if approved:
            print(
                f"✓ Approved {approved} Service Mesh InstallPlan(s) before openshift-gateway Istio",
                flush=True,
            )
            wait_servicemesh_csv_succeeded(
                timeout_sec=int(os.environ.get("SERVICEMESH_CSV_WAIT_SEC", "300")),
            )
        if not ensure_openshift_gateway_istio_for_dep_operators():
            msg = (
                "openshift-gateway Istio/controller not ready after install-dep-operators reconcile"
            )
            if _gateway_istio_failure_fatal():
                raise RuntimeError(f"{msg}; install-rhoai will fail on GatewayConfig")
            print(
                f"WARN: {msg}; install-rhoai may time out on GatewayConfig",
                file=sys.stderr,
                flush=True,
            )
    except RuntimeError:
        raise
    except Exception as exc:
        if _gateway_istio_failure_fatal():
            raise RuntimeError(
                f"openshift-gateway Istio reconcile failed ({exc})"
            ) from exc
        print(
            f"WARN: openshift-gateway Istio reconcile failed ({exc})",
            file=sys.stderr,
            flush=True,
        )


def main() -> int:
    kubeconfig = os.environ.get("KUBECONFIG", "").strip()
    if not kubeconfig:
        print("KUBECONFIG is required", file=sys.stderr)
        return 1

    olm_dir = os.environ.get("OLMINSTALL_DIR", "").strip()
    if not olm_dir:
        print("OLMINSTALL_DIR is required", file=sys.stderr)
        return 1

    olm_path = Path(olm_dir)
    os.environ.setdefault("OLMINSTALL_DIR", str(olm_path))

    _ensure_pyyaml_for_catalog()
    extra = os.environ.get("SETUP_DEPENDENCIES_ARGS", "").strip()
    components_csv = os.environ.get("COMPONENTS_CSV", "").strip()
    try:
        _ensure_openshift_gateway_istio_stack(components_csv)
    except RuntimeError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    require_authorino = components_csv_requires_authorino(components_csv)
    require_llama = components_csv_requires_llama_stack(components_csv)
    will_run_setup = bool(extra or require_authorino)

    if will_run_setup:
        try:
            ensure_setup_dependency_namespaces_ready()
        except RuntimeError as exc:
            print(f"ERROR: {exc}", file=sys.stderr)
            return 1

    if require_authorino:
        try:
            ensure_maas_rhcl_dependency_stack(olm_dir=olm_path)
        except RuntimeError as exc:
            print(
                f"WARN: RHCL preflight before setup-dependencies.sh failed ({exc}); "
                "continuing with setup-dependencies.sh",
                file=sys.stderr,
                flush=True,
            )

    if not extra and not require_authorino and not require_llama:
        print(
            "Skipping setup-dependencies.sh (no SETUP_DEPENDENCIES_ARGS and no component deps)",
            flush=True,
        )
        return 0

    run_setup_script = will_run_setup
    if run_setup_script:
        script = olm_path / "setup-dependencies.sh"
        if not script.is_file():
            print(f"setup-dependencies.sh not found under {olm_path}", file=sys.stderr)
            return 1
        patch_odh_gitops_keda_pod_selector(olm_path)
        skip_existing_stack = (
            not product_install_path()
            and require_authorino
            and existing_dependency_stack_ready()
        )
        if skip_existing_stack:
            print(
                "Skipping setup-dependencies.sh (MaaS/KEDA dependency stack already ready "
                "on existing cluster)",
                flush=True,
            )
        elif require_authorino and not extra and maas_dependency_operators_ready():
            print(
                "Skipping setup-dependencies.sh (MaaS dependency operators already at pinned RHCL CSV)",
                flush=True,
            )
        else:
            branch = os.environ.get("OLMINSTALL_GITOPS_BRANCH", "").strip()
            cmd = ["bash", str(script)]
            if branch:
                cmd.extend(["-b", branch])
            if extra:
                cmd.extend(shlex.split(extra))

            env = _prepare_setup_env(dict(os.environ))
            env["KUBECONFIG"] = kubeconfig
            print(f"Running: {' '.join(cmd)}", flush=True)
            proc = subprocess.run(cmd, cwd=olm_path, env=env, check=False)
            setup_rc = proc.returncode

            if require_authorino:
                try:
                    reconcile_rhcl_after_gitops_apply(olm_dir=olm_path)
                except RuntimeError as exc:
                    print(
                        f"WARN: RHCL reconcile after setup-dependencies.sh failed ({exc}); "
                        "continuing dependency recovery",
                        file=sys.stderr,
                        flush=True,
                    )

            # Post-install RHCL runs below (ensure_maas_rhcl_dependency_stack).
            # Finalize is only for setup-dependencies.sh failures.
            if setup_rc == 0:
                if require_authorino and not maas_dependency_operators_ready():
                    recover_authorino_after_setup_script(olm_path, setup_rc)
                try:
                    ensure_jobset_and_lws_operator_crs(olm_dir=olm_path)
                except RuntimeError as exc:
                    print(f"ERROR: {exc}", file=sys.stderr)
                    return 1
            elif setup_rc != 0:
                try:
                    finalize_rc = finalize_dependency_operators_after_setup_script(
                        olm_path,
                        setup_rc,
                    )
                except RuntimeError as exc:
                    print(f"ERROR: {exc}", file=sys.stderr)
                    return 1
                if finalize_rc == 2:
                    if require_authorino and product_install_path():
                        print(
                            "WARN: dependency setup recovered with warnings; "
                            "continuing with RHCL pin/post-install for product install",
                            file=sys.stderr,
                            flush=True,
                        )
                    elif require_authorino:
                        try:
                            ensure_maas_rhcl_dependency_stack(olm_dir=olm_path)
                            require_maas_dependency_operators()
                            mark_dep_operators_done()
                            print(
                                "WARN: recovered MaaS dependency operators after "
                                "setup-dependencies.sh warnings",
                                file=sys.stderr,
                                flush=True,
                            )
                        except RuntimeError as exc:
                            print(
                                "ERROR: install-dep-operators recovered with warnings but "
                                f"MaaS/smoke dependency setup failed ({exc})",
                                file=sys.stderr,
                            )
                            return 1
                    elif product_install_path():
                        print(
                            "WARN: dependency setup completed with recoverable issues; "
                            "continuing product install — verify-operator-ready will gate "
                            "dashboard readiness",
                            file=sys.stderr,
                            flush=True,
                        )
                        return 2
                    else:
                        print(
                            "ERROR: install-dep-operators completed with warnings outside "
                            "product-install path",
                            file=sys.stderr,
                        )
                        return 1
                elif finalize_rc != 0:
                    if require_authorino and product_install_path():
                        print(
                            f"ERROR: dependency finalize exited {finalize_rc} on product install; "
                            "not soft-continuing (Jenkins InstallDeps hard-fail parity)",
                            file=sys.stderr,
                            flush=True,
                        )
                        return 1
                    elif require_authorino:
                        print(
                            f"ERROR: install-dep-operators failed (exit {finalize_rc})",
                            file=sys.stderr,
                        )
                        return 1
                    elif product_install_path():
                        print(
                            f"WARN: dependency setup exited {finalize_rc} but continuing "
                            "product install; verify-operator-ready will gate dashboard readiness",
                            file=sys.stderr,
                            flush=True,
                        )
                        return 2
                    else:
                        print(
                            f"ERROR: install-dep-operators failed (exit {finalize_rc})",
                            file=sys.stderr,
                        )
                        return finalize_rc

    if require_authorino:
        try:
            ensure_maas_rhcl_dependency_stack(olm_dir=olm_path)
            require_maas_dependency_operators(
                allow_deferred_authorino=authorino_deferred_to_component_prep(),
            )
            _ensure_maas_bvt_prerequisites()
            from install.dsc_install import components_need_models_as_service

            ids = {c.strip() for c in components_csv.split(",") if c.strip()}
            prep_in_dep = os.environ.get(
                "RUN_COMPONENT_CLUSTER_PREP_IN_DEP_OPERATORS", ""
            ).strip().lower() in ("1", "true", "yes")
            if components_need_models_as_service(ids) and not prep_in_dep:
                from components.maas_billing.prep import try_prepare_maas_smoke

                try_prepare_maas_smoke()
            mark_dep_operators_done()
        except RuntimeError as exc:
            print(f"ERROR: {exc}", file=sys.stderr)
            return 1

    if require_llama:
        try_prepare_llama_stack_operator(timeout_sec=120)

    if components_csv_requires_kserve_deps(components_csv):
        try:
            ensure_serverless_operator()
        except RuntimeError as exc:
            print(f"ERROR: {exc}", file=sys.stderr)
            return 1

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
