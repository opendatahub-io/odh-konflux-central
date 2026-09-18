"""Wait for schedulable cluster nodes before cluster_health BVT."""

from __future__ import annotations

import json
import time

from install.dsc_install import oc_run


def _unschedulable_node_names() -> list[str]:
    proc = oc_run(["get", "nodes", "-o", "json"], check=False)
    if proc.returncode != 0:
        raise RuntimeError(
            f"cluster_health precheck: oc get nodes failed (rc={proc.returncode}): "
            f"{(proc.stderr or proc.stdout or '').strip()}"
        )
    try:
        payload = json.loads(proc.stdout or "{}")
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"cluster_health precheck: invalid nodes JSON: {exc}") from exc

    blocked: list[str] = []
    for item in payload.get("items") or []:
        meta = item.get("metadata") or {}
        name = (meta.get("name") or "").strip()
        if not name:
            continue
        spec = item.get("spec") or {}
        if spec.get("unschedulable"):
            blocked.append(name)
            continue
        status = item.get("status") or {}
        ready = False
        for cond in status.get("conditions") or []:
            if cond.get("type") == "Ready":
                ready = cond.get("status") == "True"
                break
        if not ready:
            blocked.append(name)
    return blocked


def wait_for_schedulable_nodes_for_bvt(*, timeout_sec: int, poll_sec: int = 10) -> None:
    """Block until every node is Ready and schedulable (matches cluster_health pytest)."""
    deadline = time.monotonic() + max(1, timeout_sec)
    last_blocked: list[str] = []
    while time.monotonic() < deadline:
        last_blocked = _unschedulable_node_names()
        if not last_blocked:
            return
        time.sleep(max(1, poll_sec))
    names = ", ".join(sorted(last_blocked))
    raise RuntimeError(
        "cluster_health precheck timed out waiting for schedulable nodes "
        f"({timeout_sec}s): {names}"
    )


def prepare_bvt_cluster_nodes() -> int:
    """Entry for BVT: wait for schedulable nodes before cluster_health pytest."""
    import os
    import sys

    from components.maas_billing.timeouts import bvt_cluster_nodes_timeout_sec

    try:
        wait_for_schedulable_nodes_for_bvt(timeout_sec=bvt_cluster_nodes_timeout_sec())
    except RuntimeError as exc:
        print(f"ERROR: {exc}", file=sys.stderr, flush=True)
        return 1
    return 0
