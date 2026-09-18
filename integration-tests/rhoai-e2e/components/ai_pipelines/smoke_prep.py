"""Free pooled-cluster capacity before ds-pipelines-tests smoke."""

from __future__ import annotations

import json
import re
import time

from install.dsc_install import oc_run

_DSPA_TEST_NS_RE = re.compile(r"^dspa-test-[a-z0-9]+$")
_NS_GONE_POLL_SEC = 2
_NS_GONE_MAX_WAIT_SEC = 180


def _list_dspa_test_namespaces() -> list[str]:
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
        if _DSPA_TEST_NS_RE.match(name):
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


def cleanup_ai_pipelines_smoke_leaks() -> None:
    """Delete leaked DSPA pytest namespaces so ds-pipeline-dspa can schedule."""
    for name in _list_dspa_test_namespaces():
        oc_run(
            ["delete", "namespace", name, "--ignore-not-found", "--wait=false"],
            check=False,
            capture_output=True,
            timeout=120,
        )
        if _wait_namespace_gone(name):
            print(f"✓ Removed stale AI Pipelines test namespace {name}", flush=True)
        else:
            print(f"⚠ Timed out waiting for namespace {name} to terminate", flush=True)
