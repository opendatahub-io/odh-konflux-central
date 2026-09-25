"""Free pooled-cluster capacity before ds-pipelines-tests smoke."""

from __future__ import annotations

import time

from install.dsc_install import oc_run
from install.e2e_leaked_namespace_patterns import (
    LEAKED_COMPONENT_TEST_NS_EXACT,
    LEAKED_COMPONENT_TEST_NS_PATTERNS,
)
from install.e2e_namespace_bulk_delete import list_cluster_namespace_names

_DSPA_TEST_NS_EXACT = frozenset(name for name in LEAKED_COMPONENT_TEST_NS_EXACT if name == "dspa-test")
_DSPA_TEST_NS_RE = next(
    pattern for pattern in LEAKED_COMPONENT_TEST_NS_PATTERNS if pattern.pattern == r"^dspa-test-[a-z0-9]+$"
)
_NS_GONE_POLL_SEC = 2
_NS_GONE_MAX_WAIT_SEC = 180


def _matches_dspa_test_namespace(name: str) -> bool:
    return name in _DSPA_TEST_NS_EXACT or bool(_DSPA_TEST_NS_RE.match(name))


def _list_dspa_test_namespaces() -> list[str]:
    try:
        return sorted(name for name in list_cluster_namespace_names() if _matches_dspa_test_namespace(name))
    except RuntimeError:
        return []


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
