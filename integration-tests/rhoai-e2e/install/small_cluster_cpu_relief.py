"""Free schedulable CPU on small external clusters so rhods-dashboard can land (single pod ~3100m)."""

from __future__ import annotations

import json
import os

from install.install_and_verify import oc_run

_DASHBOARD_DEPLOY_NAMES = ("rhods-dashboard", "odh-dashboard")
_RHODS_DASHBOARD_NS = "redhat-ods-applications"
_DASHBOARD_POD_PREFIXES = ("rhods-dashboard-", "odh-dashboard-")

_SMALL_CLUSTER_SOURCE_MARKERS: tuple[str, ...] = ("rh-nightly-pm",)

_CPU_RELIEF_TARGETS: tuple[tuple[str, str, int], ...] = (
    ("redhat-ods-operator", "rhods-operator", 1),
    ("redhat-ods-applications", "dashboard-redirect", 1),
    ("openshift-lws-operator", "lws-controller-manager", 1),
)

_relief_applied = False


def _truthy_env(name: str) -> bool:
    return os.environ.get(name, "").strip().lower() in ("1", "true", "yes")


def small_cluster_install_prep_enabled() -> bool:
    if _truthy_env("RHOAI_E2E_SKIP_SMALL_CLUSTER_CPU_RELIEF"):
        return False
    if _truthy_env("RHOAI_E2E_SMALL_CLUSTER_CPU_RELIEF"):
        return True
    source = os.environ.get("CLUSTER_SOURCE", "").strip().lower()
    return any(marker in source for marker in _SMALL_CLUSTER_SOURCE_MARKERS)


def _dashboard_pod_name(name: str) -> bool:
    return any(name.startswith(prefix) for prefix in _DASHBOARD_POD_PREFIXES)


def _deployment_replica_counts(deploy: str) -> tuple[int, int] | None:
    spec = oc_run(
        [
            "get",
            "deployment",
            deploy,
            "-n",
            _RHODS_DASHBOARD_NS,
            "-o",
            "jsonpath={.spec.replicas},{.status.readyReplicas}",
        ],
        check=False,
        capture_output=True,
        text=True,
        timeout=30,
    )
    if spec.returncode != 0:
        return None
    raw = (spec.stdout or "").strip()
    if not raw:
        return None
    parts = raw.split(",", 1)
    try:
        desired = int((parts[0] if parts else "") or "0")
        ready = int((parts[1] if len(parts) > 1 else "") or "0")
    except ValueError:
        return None
    return desired, ready


def _pending_dashboard_has_resource_pressure() -> bool:
    pending = oc_run(
        [
            "get",
            "pods",
            "-n",
            _RHODS_DASHBOARD_NS,
            "--field-selector=status.phase=Pending",
            "-o",
            "json",
        ],
        check=False,
        capture_output=True,
        text=True,
        timeout=60,
    )
    if pending.returncode != 0:
        return False
    try:
        pod_list = json.loads(pending.stdout or "{}").get("items") or []
    except json.JSONDecodeError:
        return False
    for pod in pod_list:
        pod_name = (pod.get("metadata") or {}).get("name", "")
        if not _dashboard_pod_name(pod_name):
            continue
        for cond in (pod.get("status") or {}).get("conditions") or []:
            if cond.get("type") == "PodScheduled" and cond.get("status") == "False":
                msg = (cond.get("message") or "").lower()
                if "insufficient cpu" in msg or "insufficient memory" in msg:
                    return True
    return False


def _scale_deployment_max(namespace: str, deploy: str, max_replicas: int) -> bool:
    spec = oc_run(
        [
            "get",
            "deployment",
            deploy,
            "-n",
            namespace,
            "-o",
            "jsonpath={.spec.replicas}",
        ],
        check=False,
        capture_output=True,
        text=True,
        timeout=30,
    )
    if spec.returncode != 0:
        return False
    try:
        desired = int((spec.stdout or "").strip() or "0")
    except ValueError:
        return False
    if desired <= max_replicas:
        return False
    scale = oc_run(
        [
            "scale",
            "deployment",
            deploy,
            "-n",
            namespace,
            f"--replicas={max_replicas}",
        ],
        check=False,
        capture_output=False,
        timeout=60,
    )
    if scale.returncode != 0:
        return False
    print(
        f"Scaled {namespace}/{deploy} {desired} -> {max_replicas} "
        "(small-cluster CPU relief for dashboard scheduling)",
        flush=True,
    )
    return True


def _dashboard_still_blocked() -> bool:
    if not _pending_dashboard_has_resource_pressure():
        return False
    for deploy in _DASHBOARD_DEPLOY_NAMES:
        counts = _deployment_replica_counts(deploy)
        if counts is None:
            continue
        desired, ready = counts
        if desired >= 1 and ready == 0:
            return True
    return False


def relieve_cpu_for_dashboard_install(*, proactive: bool = False) -> None:
    """Lower HA replica counts so a ~3100m dashboard pod can fit on one worker."""
    global _relief_applied
    if _relief_applied and not proactive:
        return
    if not proactive and not _dashboard_still_blocked():
        return
    if proactive and not small_cluster_install_prep_enabled():
        return
    if not proactive and not (
        small_cluster_install_prep_enabled() or _pending_dashboard_has_resource_pressure()
    ):
        return

    changed = False
    for namespace, deploy, max_rep in _CPU_RELIEF_TARGETS:
        if _scale_deployment_max(namespace, deploy, max_rep):
            changed = True
    if changed or proactive:
        _relief_applied = True


def maybe_small_cluster_install_prep() -> None:
    """Proactive relief before DSC wait on known small clusters (e.g. rh-nightly-pm)."""
    if small_cluster_install_prep_enabled():
        relieve_cpu_for_dashboard_install(proactive=True)
