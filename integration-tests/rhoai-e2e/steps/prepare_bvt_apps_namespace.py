#!/usr/bin/env python3
"""Reconcile app-namespace pods that block BVT operator_health pod checks on existing clusters."""

from __future__ import annotations

import json
import os
import re
import time

from install.dsc_install import oc_run

_APPS_NS = "redhat-ods-applications"
_DASHBOARD_POD_PREFIXES = ("rhods-dashboard-", "odh-dashboard-")
_MLFLOW_MIGRATION_PREFIX = "mlflow-mg-"
_MLFLOW_MIGRATION_JOB_RE = re.compile(r"^mlflow-mg-(\d+)-g\d+$")
_MLFLOW_QUIESCE_ROUNDS = 3
_MLFLOW_QUIESCE_SLEEP_SEC = 2
_MLFLOW_OPERATOR_DEPLOY = "mlflow-operator-controller-manager"
# opendatahub-tests wait_for_pods_running treats Succeeded/Failed as not-running.
_FINISHED_JOB_POD_PHASES = frozenset({"Succeeded", "Failed"})


def _mlflow_deployment_available(namespace: str) -> bool:
    r = oc_run(
        [
            "get",
            "deploy",
            "mlflow",
            "-n",
            namespace,
            "-o",
            "jsonpath={.status.conditions[?(@.type=='Available')].status}",
        ],
        check=False,
        capture_output=True,
        timeout=30,
    )
    return (r.stdout or "").strip().lower() == "true"


def _stuck_mlflow_migration_pods(namespace: str) -> list[str]:
    r = oc_run(
        ["get", "pods", "-n", namespace, "-o", "json"],
        check=False,
        capture_output=True,
        timeout=60,
    )
    if r.returncode != 0:
        return []
    try:
        doc = json.loads(r.stdout or "{}")
    except json.JSONDecodeError:
        return []
    stuck: list[str] = []
    for item in doc.get("items") or []:
        if not isinstance(item, dict):
            continue
        name = str((item.get("metadata") or {}).get("name") or "")
        if not name.startswith(_MLFLOW_MIGRATION_PREFIX):
            continue
        phase = str((item.get("status") or {}).get("phase") or "")
        if phase in ("Pending", "Failed", "Unknown"):
            stuck.append(name)
    return stuck


def _mlflow_status_version() -> str:
    r = oc_run(
        ["get", "mlflow", "mlflow", "-o", "jsonpath={.status.version}"],
        check=False,
        capture_output=True,
        timeout=30,
    )
    return (r.stdout or "").strip()


def _migration_version_from_job_name(job_name: str) -> str | None:
    match = _MLFLOW_MIGRATION_JOB_RE.match(job_name.strip())
    if not match:
        return None
    digits = match.group(1)
    if len(digits) == 4:
        return f"{digits[0]}.{digits[1:3]}.{digits[3]}"
    if len(digits) == 5:
        return f"{digits[0]}.{digits[1:3]}.{digits[3:]}"
    return digits


def _list_mlflow_migration_job_names(namespace: str) -> list[str]:
    r = oc_run(
        [
            "get",
            "jobs",
            "-n",
            namespace,
            "-o",
            'jsonpath={range .items[*]}{.metadata.name}{"\\n"}{end}',
        ],
        check=False,
        capture_output=True,
        timeout=30,
    )
    if r.returncode != 0:
        return []
    return [
        name.strip()
        for name in (r.stdout or "").splitlines()
        if name.strip().startswith(_MLFLOW_MIGRATION_PREFIX)
    ]


def _patch_mlflow_status_version(version: str) -> None:
    patch = json.dumps({"status": {"version": version}})
    oc_run(
        [
            "patch",
            "mlflow",
            "mlflow",
            "--type=merge",
            "--subresource=status",
            "-p",
            patch,
        ],
        check=False,
        capture_output=True,
        timeout=60,
    )


def _delete_mlflow_migration_jobs(namespace: str) -> None:
    for name in _list_mlflow_migration_job_names(namespace):
        oc_run(
            ["delete", "job", name, "-n", namespace, "--ignore-not-found"],
            check=False,
            capture_output=True,
            timeout=60,
        )


def _resolve_mlflow_migration_version(namespace: str) -> str:
    for job_name in _list_mlflow_migration_job_names(namespace):
        version = _migration_version_from_job_name(job_name)
        if version:
            return version
    return "3.12.0"


def _quiesce_mlflow_migration_for_bvt(namespace: str) -> None:
    """Delete stuck migration workloads and mark bootstrap complete when deploy is already up."""
    stuck = _stuck_mlflow_migration_pods(namespace)
    migration_jobs = _list_mlflow_migration_job_names(namespace)
    if not stuck and not migration_jobs and _mlflow_status_version():
        return

    version = _resolve_mlflow_migration_version(namespace)
    for round_idx in range(1, _MLFLOW_QUIESCE_ROUNDS + 1):
        stuck = _stuck_mlflow_migration_pods(namespace)
        migration_jobs = _list_mlflow_migration_job_names(namespace)
        if not stuck and not migration_jobs and _mlflow_status_version():
            return

        if stuck:
            print(
                f"Removing {len(stuck)} stuck mlflow migration pod(s) before BVT "
                f"(round {round_idx}/{_MLFLOW_QUIESCE_ROUNDS}): {', '.join(stuck)}",
                flush=True,
            )
            for name in stuck:
                oc_run(
                    ["delete", "pod", name, "-n", namespace, "--ignore-not-found"],
                    check=False,
                    capture_output=True,
                    timeout=60,
                )
        if migration_jobs:
            print(
                f"Removing {len(migration_jobs)} mlflow migration job(s) before BVT "
                f"(round {round_idx}/{_MLFLOW_QUIESCE_ROUNDS}): {', '.join(migration_jobs)}",
                flush=True,
            )
            _delete_mlflow_migration_jobs(namespace)

        if not _mlflow_status_version():
            print(
                f"Patching MLflow status.version={version} to bypass unschedulable bootstrap migration",
                flush=True,
            )
            _patch_mlflow_status_version(version)

        if round_idx < _MLFLOW_QUIESCE_ROUNDS:
            time.sleep(_MLFLOW_QUIESCE_SLEEP_SEC)


def _mlflow_operator_replicas(namespace: str) -> int | None:
    r = oc_run(
        [
            "get",
            "deploy",
            _MLFLOW_OPERATOR_DEPLOY,
            "-n",
            namespace,
            "-o",
            "jsonpath={.spec.replicas}",
        ],
        check=False,
        capture_output=True,
        timeout=30,
    )
    if r.returncode != 0:
        return None
    raw = (r.stdout or "").strip()
    if not raw:
        return None
    try:
        return int(raw)
    except ValueError:
        return None


def _scale_mlflow_operator(namespace: str, replicas: int) -> None:
    oc_run(
        [
            "scale",
            "deploy",
            _MLFLOW_OPERATOR_DEPLOY,
            "-n",
            namespace,
            f"--replicas={replicas}",
        ],
        check=False,
        capture_output=True,
        timeout=60,
    )


_MLFLOW_OPERATOR_POD_GONE_TIMEOUT_SEC = 90


def _mlflow_cr_exists() -> bool:
    r = oc_run(
        ["get", "mlflow", "mlflow"],
        check=False,
        capture_output=True,
        timeout=30,
    )
    return r.returncode == 0


def _mlflow_operator_pod_names(namespace: str) -> list[str]:
    """Pods from the mlflow-operator Deployment (name prefix is stable across releases)."""
    r = oc_run(
        [
            "get",
            "pods",
            "-n",
            namespace,
            "-o",
            'jsonpath={range .items[*]}{.metadata.name}{"\\n"}{end}',
        ],
        check=False,
        capture_output=True,
        timeout=30,
    )
    if r.returncode != 0:
        return []
    return [
        name.strip()
        for name in (r.stdout or "").splitlines()
        if name.strip().startswith(f"{_MLFLOW_OPERATOR_DEPLOY}-")
    ]


def _wait_mlflow_operator_pods_gone(namespace: str, *, timeout_sec: int) -> None:
    """BVT apps pod check fails while scaled-down operator pods linger Terminating."""
    deadline = time.time() + timeout_sec
    while time.time() < deadline:
        names = _mlflow_operator_pod_names(namespace)
        if not names:
            return
        print(
            f"Waiting for {_MLFLOW_OPERATOR_DEPLOY} pod(s) to leave {namespace}: "
            f"{', '.join(names)}",
            flush=True,
        )
        time.sleep(_MLFLOW_QUIESCE_SLEEP_SEC)
    leftover = _mlflow_operator_pod_names(namespace)
    if leftover:
        print(
            f"WARN: {_MLFLOW_OPERATOR_DEPLOY} still has pod(s) after {timeout_sec}s: "
            f"{', '.join(leftover)}",
            flush=True,
        )


def _pod_owned_by_job(item: dict) -> bool:
    for ref in (item.get("metadata") or {}).get("ownerReferences") or []:
        if isinstance(ref, dict) and str(ref.get("kind") or "") == "Job":
            return True
    return False


def _finished_job_pod_names(namespace: str) -> list[str]:
    r = oc_run(
        ["get", "pods", "-n", namespace, "-o", "json"],
        check=False,
        capture_output=True,
        timeout=60,
    )
    if r.returncode != 0:
        return []
    try:
        doc = json.loads(r.stdout or "{}")
    except json.JSONDecodeError:
        return []
    names: list[str] = []
    for item in doc.get("items") or []:
        if not isinstance(item, dict):
            continue
        phase = str((item.get("status") or {}).get("phase") or "")
        if phase not in _FINISHED_JOB_POD_PHASES:
            continue
        if not _pod_owned_by_job(item):
            continue
        name = str((item.get("metadata") or {}).get("name") or "")
        if name:
            names.append(name)
    return names


def delete_finished_job_pods_for_bvt(*, namespace: str = _APPS_NS) -> list[str]:
    """Remove Succeeded/Failed Job pods that fail ``test_application_namespace_pod_healthy``.

    CronJob leftovers (e.g. ``maas-api-key-cleanup-*``) stay ``Succeeded`` and are treated as
    not-running by opendatahub-tests ``wait_for_pods_running``.
    """
    names = _finished_job_pod_names(namespace)
    if not names:
        return []
    print(
        f"Removing {len(names)} finished Job pod(s) before BVT in {namespace}: "
        f"{', '.join(names)}",
        flush=True,
    )
    for name in names:
        oc_run(
            ["delete", "pod", name, "-n", namespace, "--ignore-not-found"],
            check=False,
            capture_output=True,
            timeout=60,
        )
    return names


def _list_cronjob_names(namespace: str) -> list[str]:
    r = oc_run(
        [
            "get",
            "cronjob",
            "-n",
            namespace,
            "-o",
            'jsonpath={range .items[*]}{.metadata.name}{"\\n"}{end}',
        ],
        check=False,
        capture_output=True,
        timeout=30,
    )
    if r.returncode != 0:
        return []
    return [name.strip() for name in (r.stdout or "").splitlines() if name.strip()]


def _cronjob_is_suspended(namespace: str, name: str) -> bool:
    r = oc_run(
        [
            "get",
            "cronjob",
            name,
            "-n",
            namespace,
            "-o",
            "jsonpath={.spec.suspend}",
        ],
        check=False,
        capture_output=True,
        timeout=30,
    )
    return (r.stdout or "").strip().lower() == "true"


def _patch_cronjob_suspend(namespace: str, name: str, suspend: bool) -> None:
    patch = json.dumps({"spec": {"suspend": suspend}})
    oc_run(
        [
            "patch",
            "cronjob",
            name,
            "-n",
            namespace,
            "--type=merge",
            "-p",
            patch,
        ],
        check=False,
        capture_output=True,
        timeout=60,
    )


def suspend_apps_cronjobs_for_bvt(*, namespace: str = _APPS_NS) -> list[str]:
    """Suspend apps CronJobs and delete finished Job pods before apps pod-health BVT.

    Returns CronJob names this call suspended (for ``resume_apps_cronjobs``). Already-suspended
    CronJobs are left alone. Suspend first so a mid-wait ``*/15`` schedule cannot recreate
    Succeeded pods during the 180s ``wait_for_pods_running`` window.
    """
    suspended: list[str] = []
    for name in _list_cronjob_names(namespace):
        if _cronjob_is_suspended(namespace, name):
            continue
        _patch_cronjob_suspend(namespace, name, True)
        suspended.append(name)
    if suspended:
        print(
            f"Suspended CronJob(s) before BVT in {namespace}: {', '.join(suspended)}",
            flush=True,
        )
    delete_finished_job_pods_for_bvt(namespace=namespace)
    return suspended


def resume_apps_cronjobs(names: list[str] | None, *, namespace: str = _APPS_NS) -> None:
    if not names:
        return
    for name in names:
        _patch_cronjob_suspend(namespace, name, False)
    print(
        f"Restored CronJob suspend=false in {namespace}: {', '.join(names)}",
        flush=True,
    )


def pause_mlflow_operator_reconcile_for_bvt(*, namespace: str = _APPS_NS) -> int:
    """Stop MLflow operator reconciliation while BVT runs on resource-tight pooled clusters.

    Skip when no MLflow instance exists (operator-only / fresh install): scaling the operator
    to 0 leaves Terminating pods that fail ``test_application_namespace_pod_healthy``.
    """
    if not _mlflow_cr_exists() and not _mlflow_deployment_available(namespace):
        print(
            f"NOTE: no mlflow CR/deploy in {namespace}; "
            f"skipping {_MLFLOW_OPERATOR_DEPLOY} pause before BVT",
            flush=True,
        )
        return 0

    prior = _mlflow_operator_replicas(namespace)
    if prior is None:
        prior = 1
    if prior > 0:
        print(
            f"Scaling {_MLFLOW_OPERATOR_DEPLOY} to 0 before BVT (was {prior})",
            flush=True,
        )
        _scale_mlflow_operator(namespace, 0)
        _wait_mlflow_operator_pods_gone(
            namespace,
            timeout_sec=_MLFLOW_OPERATOR_POD_GONE_TIMEOUT_SEC,
        )
    _quiesce_mlflow_migration_for_bvt(namespace)
    if _mlflow_cr_exists() and not _mlflow_status_version():
        version = _resolve_mlflow_migration_version(namespace)
        print(
            f"Patching MLflow status.version={version} after operator pause",
            flush=True,
        )
        _patch_mlflow_status_version(version)
    return prior


def resume_mlflow_operator_reconcile(*, namespace: str = _APPS_NS, prior_replicas: int = 1) -> None:
    if prior_replicas <= 0:
        return
    print(
        f"Restoring {_MLFLOW_OPERATOR_DEPLOY} replicas to {prior_replicas}",
        flush=True,
    )
    _scale_mlflow_operator(namespace, prior_replicas)


def _dashboard_pod_blockers(namespace: str = _APPS_NS) -> list[str]:
    """Return dashboard pods that are not Running+Ready (or a missing-pod marker)."""
    r = oc_run(
        ["get", "pods", "-n", namespace, "-o", "json"],
        check=False,
        capture_output=True,
        timeout=60,
    )
    if r.returncode != 0:
        return ["dashboard pod list failed"]
    try:
        doc = json.loads(r.stdout or "{}")
    except json.JSONDecodeError:
        return ["dashboard pod list invalid"]
    matching = 0
    blockers: list[str] = []
    for item in doc.get("items") or []:
        if not isinstance(item, dict):
            continue
        name = str((item.get("metadata") or {}).get("name") or "")
        if not name.startswith(_DASHBOARD_POD_PREFIXES):
            continue
        matching += 1
        phase = str((item.get("status") or {}).get("phase") or "")
        statuses = (item.get("status") or {}).get("containerStatuses") or []
        ready = bool(statuses) and all(
            bool(cs.get("ready")) for cs in statuses if isinstance(cs, dict)
        )
        if phase != "Running" or not ready:
            blockers.append(f"{name}:{phase or '?'}")
    if matching == 0:
        return ["no dashboard pods"]
    return blockers


def wait_dashboard_pods_ready_for_bvt(
    *, timeout_sec: int | None = None, namespace: str = _APPS_NS
) -> None:
    """Block operator_health BVT until dashboard pods are Running (pytest wait is 180s)."""
    if timeout_sec is None:
        raw = (os.environ.get("BVT_DASHBOARD_POD_WAIT_SEC") or "").strip()
        try:
            timeout_sec = int(raw) if raw else 600
        except ValueError:
            print(
                f"WARN: invalid BVT_DASHBOARD_POD_WAIT_SEC={raw!r}; using 600s",
                flush=True,
            )
            timeout_sec = 600
    deadline = time.time() + timeout_sec
    last = "reconciling..."
    while time.time() < deadline:
        blockers = _dashboard_pod_blockers(namespace)
        if not blockers:
            print(
                f"✓ dashboard pods Running in {namespace} (BVT gate)",
                flush=True,
            )
            return
        last = ", ".join(blockers[:8])
        if int(time.time()) % 60 < 12:
            print(
                f"Waiting for dashboard pods before operator_health BVT: {last}",
                flush=True,
            )
        time.sleep(10)
    raise RuntimeError(
        f"dashboard pods not Running in {namespace} after {timeout_sec}s "
        f"before operator_health BVT: {last[:300]}"
    )


def reconcile_stuck_mlflow_migration_pods_for_bvt(*, namespace: str = _APPS_NS) -> None:
    """Drop unschedulable mlflow bootstrap migration pods when mlflow deploy is already Available.

    MLflow operator migration Jobs can stay Pending on resource-tight pooled clusters and cause
    ``test_application_namespace_pod_healthy`` to fail BVT even though the mlflow Deployment is up.
    """
    if not _mlflow_deployment_available(namespace):
        print(
            f"WARN: mlflow deployment not Available in {namespace}; "
            "skipping mlflow migration pod cleanup before BVT",
            flush=True,
        )
        return

    _quiesce_mlflow_migration_for_bvt(namespace)


def main() -> int:
    reconcile_stuck_mlflow_migration_pods_for_bvt()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
