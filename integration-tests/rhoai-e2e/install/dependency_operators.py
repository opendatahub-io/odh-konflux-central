"""Kuadrant/Authorino dependency operators (setup-dependencies.sh recovery and checks).

Used by Tekton ``install-dep-operators`` (``install_minimal_deps``) and smoke cluster prep
(``require_maas_dependency_operators``).
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

from install.dsc_install import components_need_models_as_service, oc_run

_KEDA_NAMESPACE = "openshift-keda"
_KEDA_CSV_NAME_PREFIX = "custom-metrics-autoscaler."
_KEDA_POD_LABEL_SELECTORS = (
    "name=custom-metrics-autoscaler-operator",
    "app=keda-operator",
)
_DEFAULT_SETUP_DEPENDENCY_NS_READY_TIMEOUT_SEC = 600
# Namespaces cleanup-external deletes; setup-dependencies.sh recreates operatorgroups/subs.
_SETUP_DEPENDENCY_NAMESPACES: tuple[str, ...] = (
    "kuadrant-system",
    "rh-connectivity-link",
    "cert-manager",
    "cert-manager-operator",
    "openshift-keda",
    "openshift-kueue-operator",
    "openshift-lws-operator",
    "openshift-jobset-operator",
)
_AUTHORINO_AUTHCONFIG_CRD = "authconfigs.authorino.kuadrant.io"
_AUTHORINO_CR_NAME = "authorino"
# Jenkins InstallDeps: resources/post-install-jobset-operator.sh + post-install-leader-worker-set.sh
_JOBSET_POST_INSTALL = "resources/post-install-jobset-operator.sh"
_LWS_POST_INSTALL = "resources/post-install-leader-worker-set.sh"
_JOBSET_OPERATOR_KIND = "jobsetoperator.operator.openshift.io"
_LWS_OPERATOR_KIND = "leaderworkersetoperator.operator.openshift.io"


def maas_dependency_operators_ready() -> bool:
    if not _authorino_crd_available() or not _authorino_cr_exists():
        return False
    from install.rhcl_deps import rhcl_operators_ready, rhcl_starting_csv

    return rhcl_operators_ready(rhcl_starting_csv())


def _keda_operator_pod_ready() -> bool:
    for selector in _KEDA_POD_LABEL_SELECTORS:
        proc = oc_run(
            [
                "get",
                "pods",
                "-n",
                _KEDA_NAMESPACE,
                "-l",
                selector,
                "-o",
                "jsonpath={.items[?(@.status.phase=='Running')].metadata.name}",
            ],
            check=False,
            capture_output=True,
            timeout=30,
        )
        if proc.returncode == 0 and (proc.stdout or "").strip():
            return True
    return False


def custom_metrics_autoscaler_operator_ready() -> bool:
    """True when OpenShift KEDA CSV succeeded and the operator pod is Running."""
    proc = oc_run(
        [
            "get",
            "csv",
            "-n",
            _KEDA_NAMESPACE,
            "-o",
            "jsonpath={range .items[*]}{.metadata.name}{'\\t'}{.status.phase}{'\\n'}{end}",
        ],
        check=False,
        capture_output=True,
        timeout=30,
    )
    if proc.returncode != 0:
        return False
    for line in (proc.stdout or "").splitlines():
        name, _, phase = line.partition("\t")
        if name.startswith(_KEDA_CSV_NAME_PREFIX) and phase.strip() == "Succeeded":
            return _keda_operator_pod_ready()
    return False


def existing_dependency_stack_ready() -> bool:
    """True when pooled external clusters already have MaaS + KEDA deps for smoke."""
    return maas_dependency_operators_ready() and custom_metrics_autoscaler_operator_ready()


def patch_odh_gitops_keda_pod_selector(olm_dir: Path) -> None:
    """Align odh-gitops KEDA pod wait with OpenShift custom-metrics-autoscaler labels."""
    gitops = olm_dir / "odh-gitops"
    if not gitops.is_dir():
        return
    patched = 0
    for path in gitops.rglob("*"):
        if not path.is_file():
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        if "app=keda-operator" not in text:
            continue
        path.write_text(
            text.replace("app=keda-operator", "name=custom-metrics-autoscaler-operator"),
            encoding="utf-8",
        )
        patched += 1
    if patched:
        print(
            f"✓ Patched odh-gitops KEDA pod selector in {patched} file(s) "
            "(custom-metrics-autoscaler-operator)",
            flush=True,
        )


def require_maas_dependency_operators(*, allow_deferred_authorino: bool = False) -> None:
    """Fail fast when MaaS smoke is selected but install-dep-operators did not install deps."""
    if maas_dependency_operators_ready():
        if not (
            allow_deferred_authorino and authorino_deferred_to_component_prep()
        ):
            from components.maas_billing.auth import _wait_authorino_workload_ready

            timeout = int(os.environ.get("AUTHORINO_READY_TIMEOUT_SEC", "900"))
            _wait_authorino_workload_ready(timeout_sec=timeout)
        print("✓ MaaS dependency operators present (install-dep-operators)", flush=True)
        return
    if allow_deferred_authorino and authorino_deferred_to_component_prep():
        from install.rhcl_deps import rhcl_stack_functional

        if rhcl_stack_functional():
            target = "prepare-components-prerequisites"
            if os.environ.get("INSTALL_DEPENDENCIES", "").strip().lower() in (
                "1",
                "true",
                "yes",
            ):
                target = "prepare-component-cluster"
            print(
                "WARN: Authorino CR not ready but RHCL stack is functional; "
                f"deferring Authorino readiness to {target}",
                file=sys.stderr,
                flush=True,
            )
            return
    raise RuntimeError(_maas_deps_missing_message())


def components_csv_requires_authorino(components_csv: str) -> bool:
    ids = {c.strip() for c in (components_csv or "").split(",") if c.strip()}
    if not ids:
        return False
    return components_need_models_as_service(ids)


def product_install_path() -> bool:
    """True when the pipeline is installing RHOAI/ODH (not test-only smoke)."""
    return os.environ.get("PRODUCT", "").strip().lower() in ("rhoai", "odh")


def _cluster_operator_cr_exists(kind: str, name: str = "cluster") -> bool:
    proc = oc_run(
        ["get", kind, name, "-o", "name"],
        check=False,
        capture_output=True,
        timeout=30,
    )
    return proc.returncode == 0 and bool((proc.stdout or "").strip())


def _cluster_operator_crd_available(kind: str) -> bool:
    """True when the operator CRD is on the API (CSV may still be installing)."""
    # kind is like jobsetoperator.operator.openshift.io — CRD name matches plural API group.
    crd = {
        _JOBSET_OPERATOR_KIND: "jobsetoperators.operator.openshift.io",
        _LWS_OPERATOR_KIND: "leaderworkersetoperators.operator.openshift.io",
    }.get(kind)
    if not crd:
        return False
    proc = oc_run(
        ["get", "crd", crd, "-o", "name"],
        check=False,
        capture_output=True,
        timeout=30,
    )
    return proc.returncode == 0 and bool((proc.stdout or "").strip())


def _run_operator_install_post_install_script(olm_dir: Path, rel_script: str) -> bool:
    script = olm_dir / rel_script
    if not script.is_file():
        print(f"WARN: {rel_script} missing under {olm_dir}; skipping", flush=True)
        return False
    print(f"Running {script.name} (Jenkins InstallDeps parity)...", flush=True)
    proc = subprocess.run(
        ["bash", str(script)],
        cwd=str(olm_dir),
        env=os.environ.copy(),
        check=False,
        timeout=300,
    )
    if proc.returncode != 0:
        print(
            f"WARN: {script.name} exited {proc.returncode}",
            file=sys.stderr,
            flush=True,
        )
        return False
    return True


def ensure_jobset_and_lws_operator_crs(*, olm_dir: Path | None = None) -> None:
    """Create JobSetOperator/cluster and LeaderWorkerSetOperator/cluster (Jenkins parity).

    ``setup-dependencies.sh`` / odh-gitops may install the operator CSV without the
    cluster CR. After CLEANUP that CR is gone; without it TrainerReady stays False
    (JobSetOperator CR not found). Prefer rhoai-e2e ``post-install-*.sh`` scripts.
    """
    root = olm_dir
    if root is None:
        raw = os.environ.get("OLMINSTALL_DIR", "").strip()
        root = Path(raw) if raw else None
    if root is None or not root.is_dir():
        print(
            "WARN: OLMINSTALL_DIR unset; cannot run JobSet/LWS post-install scripts",
            file=sys.stderr,
            flush=True,
        )
        return

    for rel, kind, label in (
        (_JOBSET_POST_INSTALL, _JOBSET_OPERATOR_KIND, "JobSetOperator"),
        (_LWS_POST_INSTALL, _LWS_OPERATOR_KIND, "LeaderWorkerSetOperator"),
    ):
        if not _cluster_operator_crd_available(kind):
            print(
                f"NOTE: {label} CRD not present yet; skip {rel} until operator installs",
                flush=True,
            )
            continue
        if _cluster_operator_cr_exists(kind):
            print(f"✓ {label}/cluster already present", flush=True)
            continue
        if not _run_operator_install_post_install_script(root, rel):
            # Inline apply matching Jenkins resources/*.yaml when script missing/failed
            manifest = root / "resources" / (
                "jobset-instance.yaml"
                if "jobset" in rel
                else "leader-worker-set-instance.yaml"
            )
            if manifest.is_file():
                print(f"Applying {manifest.name}...", flush=True)
                apply = oc_run(
                    ["apply", "-f", str(manifest)],
                    check=False,
                    capture_output=True,
                    timeout=60,
                )
                if apply.returncode != 0:
                    err = (apply.stderr or apply.stdout or "").strip()
                    raise RuntimeError(f"Failed to create {label}/cluster: {err[:300]}")
            else:
                raise RuntimeError(
                    f"{label}/cluster missing and neither {rel} nor {manifest.name} available"
                )
        if not _cluster_operator_cr_exists(kind):
            raise RuntimeError(f"{label}/cluster still missing after post-install")
        print(f"✓ {label}/cluster created", flush=True)


def existing_smoke_without_install_dependencies() -> bool:
    """True when install-dep-operators is skipped on a pooled external cluster."""
    if product_install_path():
        return False
    return os.environ.get("INSTALL_DEPENDENCIES", "").strip().lower() not in (
        "1",
        "true",
        "yes",
    )


def _maas_deps_missing_message() -> str:
    if existing_smoke_without_install_dependencies():
        return (
            "Kuadrant/Authorino dependency operators are missing or RHCL CSV is not pinned. "
            "On test-only runs (omit --product), install-dep-operators runs only with INSTALL_DEPENDENCIES=true. "
            "Retrigger with rhoai_e2e.py --install-dependencies, or ensure RHCL/Authorino "
            "are already installed on the cluster."
        )
    return (
        "Kuadrant/Authorino dependency operators are missing or RHCL CSV is not pinned. "
        "Expected Tekton task install-dep-operators (RUN_MINIMAL_DEPS=true) to run "
        "setup-dependencies.sh before smoke. Check that task succeeded for this PipelineRun."
    )


def authorino_deferred_to_component_prep() -> bool:
    """True when Authorino/Kuadrant TLS can finish in prepare-component-cluster."""
    if product_install_path():
        return True
    if os.environ.get("INSTALL_DEPENDENCIES", "").strip().lower() in ("1", "true", "yes"):
        return True
    return os.environ.get("RUN_COMPONENT_CLUSTER_PREP_IN_DEP_OPERATORS", "").strip().lower() in (
        "1",
        "true",
        "yes",
    )


def _namespace_phase(name: str) -> str:
    r = oc_run(
        ["get", "namespace", name, "-o", "jsonpath={.status.phase}"],
        check=False,
        capture_output=True,
        timeout=30,
    )
    if r.returncode != 0:
        return ""
    return (r.stdout or "").strip()


_TERMINATING_FORCE_DELETE_KINDS: tuple[str, ...] = (
    "cronjob",
    "job",
    "pod",
    "replicaset",
    "deployment",
    "statefulset",
)


def _force_delete_terminating_namespace_resources(name: str) -> None:
    """Delete leftover workload objects that keep a namespace in Terminating.

    ``oc delete --all`` can hang on stuck pods. Use a soft subprocess timeout so
    ``dsc_install.oc_run`` does not ``sys.exit`` the whole setup-dependencies step.
    """
    print(
        f"Namespace {name} still Terminating - force-deleting remaining workload objects...",
        flush=True,
    )
    oc = shutil.which("oc") or "oc"
    for kind in _TERMINATING_FORCE_DELETE_KINDS:
        cmd = [
            oc,
            "delete",
            kind,
            "--all",
            "-n",
            name,
            "--grace-period=0",
            "--force",
            "--ignore-not-found",
            "--wait=false",
        ]
        try:
            proc = subprocess.run(
                cmd,
                check=False,
                capture_output=True,
                text=True,
                timeout=45,
            )
            if proc.returncode != 0:
                err = (proc.stderr or proc.stdout or "").strip()
                print(
                    f"WARN: force-delete {kind} in {name} exit {proc.returncode}: {err[:200]}",
                    flush=True,
                )
        except subprocess.TimeoutExpired:
            print(
                f"WARN: force-delete {kind} in {name} timed out after 45s; continuing unblock",
                flush=True,
            )


def unblock_terminating_namespace(name: str) -> None:
    """Clear finalizers on namespaces stuck Terminating (blocks setup-dependencies KEDA apply)."""
    if _namespace_phase(name) != "Terminating":
        return
    print(
        f"Namespace {name} is stuck Terminating - clearing spec.finalizers so dependency apply can proceed...",
        flush=True,
    )
    patch = oc_run(
        ["patch", "namespace", name, "--type=merge", "-p", '{"spec":{"finalizers":[]}}'],
        check=False,
        capture_output=True,
        timeout=60,
    )
    if _namespace_phase(name) != "Terminating":
        return
    _force_delete_terminating_namespace_resources(name)
    if _namespace_phase(name) != "Terminating":
        return
    get = oc_run(["get", "namespace", name, "-o", "json"], check=False, capture_output=True, timeout=30)
    if get.returncode != 0:
        return
    try:
        body = json.loads(get.stdout or "{}")
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"Could not parse namespace {name} JSON: {exc}") from exc
    body.setdefault("spec", {})["finalizers"] = []
    finalize = oc_run(
        ["replace", "--raw", f"/api/v1/namespaces/{name}/finalize", "-f", "-"],
        stdin_text=json.dumps(body),
        check=False,
        capture_output=True,
        timeout=60,
    )
    if _namespace_phase(name) != "Terminating":
        return
    _force_delete_terminating_namespace_resources(name)
    if _namespace_phase(name) != "Terminating":
        return
    wait = oc_run(
        ["wait", "--for=delete", f"namespace/{name}", "--timeout=60s"],
        check=False,
        capture_output=True,
        timeout=75,
    )
    if wait.returncode != 0 and _namespace_phase(name) == "Terminating":
        err = (finalize.stderr or finalize.stdout or patch.stderr or patch.stdout or "").strip()
        raise RuntimeError(f"Could not unblock Terminating namespace {name}: {err or 'unknown error'}")


def _setup_dependency_namespace_ready_timeout_sec() -> int:
    raw = os.environ.get("SETUP_DEPENDENCY_NAMESPACE_READY_TIMEOUT_SEC", "").strip()
    if raw:
        return int(raw)
    return _DEFAULT_SETUP_DEPENDENCY_NS_READY_TIMEOUT_SEC


def ensure_setup_dependency_namespaces_ready(
    namespaces: tuple[str, ...] | None = None,
    *,
    timeout_sec: int | None = None,
) -> None:
    """Wait for dependency namespaces to leave Terminating after cleanup-external."""
    targets = namespaces or _SETUP_DEPENDENCY_NAMESPACES
    timeout = timeout_sec if timeout_sec is not None else _setup_dependency_namespace_ready_timeout_sec()
    deadline = time.time() + timeout
    pending = set(targets)
    while pending and time.time() < deadline:
        still_pending: set[str] = set()
        for ns in sorted(pending):
            phase = _namespace_phase(ns)
            if not phase or phase == "Active":
                continue
            if phase == "Terminating":
                unblock_terminating_namespace(ns)
                phase = _namespace_phase(ns)
            if not phase or phase == "Active":
                continue
            still_pending.add(ns)
        pending = still_pending
        if pending:
            time.sleep(5)
    if pending:
        phases = {ns: _namespace_phase(ns) or "missing" for ns in sorted(pending)}
        strict = os.environ.get("SETUP_NS_READY_STRICT", "").strip().lower() in (
            "1",
            "true",
            "yes",
        )
        terminating = {ns: phase for ns, phase in phases.items() if phase == "Terminating"}
        if strict or terminating:
            raise RuntimeError(
                f"Dependency namespaces not ready after {timeout}s: {phases}"
            )
        print(
            f"WARN: Dependency namespaces not ready after {timeout}s: {phases}",
            file=sys.stderr,
            flush=True,
        )
        return
    print("✓ setup-dependencies target namespaces ready", flush=True)


def _gitops_make_env() -> dict[str, str]:
    from install.install_minimal_deps import _ensure_kubectl_on_path, _ensure_yq_on_path

    env = _ensure_yq_on_path(_ensure_kubectl_on_path(dict(os.environ)))
    env["K8S_CLI"] = "oc"
    return env


def _seed_odh_gitops_yq(gitops: Path, env: dict[str, str]) -> None:
    """Point odh-gitops Makefile at PATH yq so ``bin/yq`` download (Error 127) is skipped."""
    yq = shutil.which("yq", path=env.get("PATH", ""))
    if not yq:
        return
    bin_dir = gitops / "bin"
    target = bin_dir / "yq"
    try:
        bin_dir.mkdir(parents=True, exist_ok=True)
        if target.is_file() and os.access(target, os.X_OK):
            return
        if target.exists() or target.is_symlink():
            target.unlink()
        target.symlink_to(yq)
        print(f"Seeded odh-gitops bin/yq -> {yq}", flush=True)
    except OSError as exc:
        print(f"WARN: could not seed odh-gitops bin/yq ({exc})", file=sys.stderr, flush=True)


def _run_odh_gitops_make(olm_dir: Path, *make_args: str) -> int:
    gitops = olm_dir / "odh-gitops"
    if not gitops.is_dir():
        return 127
    env = _gitops_make_env()
    _seed_odh_gitops_yq(gitops, env)
    cmd = ["make", "-C", str(gitops), *make_args]
    print(f"Running: {' '.join(cmd)}", flush=True)
    return subprocess.run(cmd, env=env, check=False).returncode


def _authorino_crd_available() -> bool:
    r = oc_run(
        ["get", "crd", _AUTHORINO_AUTHCONFIG_CRD],
        check=False,
        capture_output=True,
        timeout=30,
    )
    return r.returncode == 0


def _authorino_cr_exists() -> bool:
    for ns in ("kuadrant-system", "rh-connectivity-link"):
        r = oc_run(
            ["get", "authorino", _AUTHORINO_CR_NAME, "-n", ns],
            check=False,
            capture_output=True,
            timeout=15,
        )
        if r.returncode == 0:
            return True
    return False


def _wait_authorino_cr_exists(*, timeout_sec: int = 120) -> bool:
    """Short poll for Authorino CR to appear after Kuadrant reconciles."""
    deadline = time.time() + timeout_sec
    while time.time() < deadline:
        if _authorino_cr_exists():
            return True
        remaining = int(deadline - time.time())
        if remaining > 0 and int(time.time()) % 30 < 12:
            print(
                f"Waiting for Kuadrant to create Authorino CR ({remaining}s remaining)...",
                flush=True,
            )
        time.sleep(10)
    return False


def _ensure_authorino_operators_after_setup(olm_dir: Path, setup_rc: int) -> bool:
    """Wait for Authorino on slow EPHC clusters, then apply odh-gitops configs and TLS.

    Returns True when any recoverable issue was logged (WARN) during recovery.
    """
    had_warnings = False
    if not (olm_dir / "odh-gitops").is_dir():
        return had_warnings

    os.environ.setdefault("OLMINSTALL_DIR", str(olm_dir))
    from components.maas_billing.auth import _wait_authorino_workload_ready

    if not _authorino_cr_exists():
        print("Authorino CR missing - applying odh-gitops configurations...", flush=True)
        cfg_rc = _run_odh_gitops_make(olm_dir, "apply", "FOLDER=configurations")
        if cfg_rc != 0:
            raise RuntimeError(f"odh-gitops configurations apply failed (exit {cfg_rc})")

    if not _authorino_cr_exists():
        cr_timeout = int(os.environ.get("AUTHORINO_CR_WAIT_SEC", "120"))
        if not _wait_authorino_cr_exists(timeout_sec=cr_timeout):
            print(
                f"WARN: Authorino CR not created by Kuadrant after {cr_timeout}s; "
                "skipping workload wait (Kuadrant reconciliation incomplete)",
                file=sys.stderr,
                flush=True,
            )
            had_warnings = True
            if not maas_dependency_operators_ready() or setup_rc != 0:
                tls_rc = _run_odh_gitops_make(olm_dir, "prepare-authorino-tls", "KUSTOMIZE_MODE=false")
                if tls_rc != 0:
                    print(
                        f"WARN: prepare-authorino-tls exited {tls_rc}; smoke prereqs may retry TLS setup",
                        file=sys.stderr,
                        flush=True,
                    )
            return True

    timeout = int(os.environ.get("AUTHORINO_READY_TIMEOUT_SEC", "300"))
    print(
        f"Waiting up to {timeout}s for Authorino workload after setup-dependencies...",
        flush=True,
    )
    try:
        _wait_authorino_workload_ready(timeout_sec=timeout)
    except RuntimeError as exc:
        print(f"WARN: {exc}", file=sys.stderr, flush=True)
        had_warnings = True

    if not maas_dependency_operators_ready() or setup_rc != 0:
        tls_rc = _run_odh_gitops_make(olm_dir, "prepare-authorino-tls", "KUSTOMIZE_MODE=false")
        if tls_rc != 0:
            print(
                f"WARN: prepare-authorino-tls exited {tls_rc}; smoke prereqs may retry TLS setup",
                file=sys.stderr,
                flush=True,
            )
            had_warnings = True
    return had_warnings


def recover_authorino_after_setup_script(olm_dir: Path, setup_rc: int) -> bool:
    """Apply odh-gitops Authorino recovery when setup-dependencies did not leave MaaS deps ready."""
    return _ensure_authorino_operators_after_setup(olm_dir, setup_rc)


def _reconcile_rhcl_after_gitops_with_warning(olm_dir: Path) -> bool:
    """Re-pin RHCL after gitops apply; return True when recovery logged a warning."""
    from install.rhcl_deps import reconcile_rhcl_after_gitops_apply

    os.environ.setdefault("OLMINSTALL_DIR", str(olm_dir))
    try:
        reconcile_rhcl_after_gitops_apply(olm_dir=olm_dir)
    except RuntimeError as exc:
        print(
            f"WARN: RHCL reconcile during dependency recovery failed ({exc})",
            file=sys.stderr,
            flush=True,
        )
        return True
    return False


def finalize_dependency_operators_after_setup_script(olm_dir: Path, setup_rc: int) -> int:
    """Retry odh-gitops / Authorino CR apply when setup-dependencies.sh did not finish cleanly.

    Exit codes: 0 clean success, 2 recovered with warnings, non-zero failure.
    """
    had_warnings = setup_rc != 0
    ensure_setup_dependency_namespaces_ready()

    rc = setup_rc
    if rc != 0:
        had_warnings = _reconcile_rhcl_after_gitops_with_warning(olm_dir) or had_warnings
        print(
            f"WARN: setup-dependencies.sh exited {rc}; retrying odh-gitops apply-and-verify-dependencies...",
            flush=True,
        )
        rc = _run_odh_gitops_make(olm_dir, "apply-and-verify-dependencies")
        had_warnings = _reconcile_rhcl_after_gitops_with_warning(olm_dir) or had_warnings

    if rc != 0 and not _authorino_crd_available():
        return rc

    if rc != 0:
        if product_install_path():
            print(
                f"ERROR: dependency setup exited {rc} on product install; "
                "not soft-continuing (Jenkins InstallDeps hard-fail parity)",
                file=sys.stderr,
                flush=True,
            )
            # Still ensure JobSet/LWS CRs after CLEANUP — early return used to skip this and
            # leave TrainerReady=False (JobSetOperator/cluster missing) on product reinstall.
            try:
                ensure_jobset_and_lws_operator_crs(olm_dir=olm_dir)
            except Exception as exc:  # noqa: BLE001 - oc timeouts must not crash finalize
                print(
                    f"ERROR: JobSet/LWS operator CR ensure failed ({exc})",
                    file=sys.stderr,
                    flush=True,
                )
            return rc
        print(
            f"WARN: dependency setup exited {rc} but Authorino CRD is available; continuing",
            flush=True,
        )
        had_warnings = True

    recovery_warnings = _ensure_authorino_operators_after_setup(olm_dir, setup_rc)
    had_warnings = had_warnings or recovery_warnings

    try:
        ensure_jobset_and_lws_operator_crs(olm_dir=olm_dir)
    except Exception as exc:  # noqa: BLE001 - oc timeouts must not crash finalize
        print(f"ERROR: JobSet/LWS operator CR ensure failed ({exc})", file=sys.stderr)
        return 1

    if maas_dependency_operators_ready():
        print("✓ MaaS dependency operators ready after setup-dependencies recovery", flush=True)
        return 2 if had_warnings else 0
    return rc if rc != 0 else 1
