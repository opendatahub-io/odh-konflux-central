"""Free pooled-cluster capacity before KubeRay smoke."""

from __future__ import annotations

import json
import re
import time

from install.dsc_install import oc_run

# KubeRay pytest fixtures use test-ns-<id> (see integration-tests/kuberay pr-testing-pipeline).
_KUBERAY_TEST_NS_RE = re.compile(r"^test-ns-[a-z0-9]+$")
_NS_GONE_POLL_SEC = 2
_NS_GONE_MAX_WAIT_SEC = 120


def _list_kuberay_test_namespaces() -> list[str]:
    listed = oc_run(
        ["get", "namespace", "-o", "json"],
        check=False,
        capture_output=True,
        timeout=60,
    )
    if listed.returncode != 0:
        return []
    try:
        doc = json.loads(listed.stdout or "{}")
    except json.JSONDecodeError:
        return []
    names: list[str] = []
    for item in doc.get("items") or []:
        if not isinstance(item, dict):
            continue
        name = str((item.get("metadata") or {}).get("name") or "")
        if _KUBERAY_TEST_NS_RE.match(name):
            names.append(name)
    return sorted(names)


def _namespace_not_found(result) -> bool:
    if result.returncode == 0:
        return False
    combined = f"{result.stderr or ''}\n{result.stdout or ''}"
    return "NotFound" in combined


def _wait_namespace_gone(name: str) -> bool:
    deadline = time.monotonic() + _NS_GONE_MAX_WAIT_SEC
    while time.monotonic() < deadline:
        listed = oc_run(
            ["get", "namespace", name],
            check=False,
            capture_output=True,
            timeout=30,
        )
        if _namespace_not_found(listed):
            return True
        time.sleep(_NS_GONE_POLL_SEC)
    return False


def cleanup_kuberay_smoke_leaks() -> None:
    """Delete leaked KubeRay pytest namespaces so RayClusters can schedule."""
    for name in _list_kuberay_test_namespaces():
        oc_run(
            ["delete", "namespace", name, "--ignore-not-found", "--wait=false"],
            check=False,
            capture_output=True,
            timeout=120,
        )
        if _wait_namespace_gone(name):
            print(f"✓ Removed stale KubeRay test namespace {name}", flush=True)
        else:
            print(f"✗ Timed out waiting for namespace {name} to be removed", flush=True)
