"""Free pooled-cluster capacity before model_runtime (vLLM) smoke."""

from __future__ import annotations

import time

from install.dsc_install import oc_run
from install.e2e_leaked_namespace_patterns import (
    LEAKED_COMPONENT_TEST_NS_EXACT,
    LEAKED_COMPONENT_TEST_NS_PATTERNS,
)
from install.e2e_namespace_bulk_delete import list_cluster_namespace_names

_MODEL_RUNTIME_NS_EXACT = frozenset(
    name
    for name in LEAKED_COMPONENT_TEST_NS_EXACT
    if name.startswith(("ovms-", "triton-"))
)
_MODEL_RUNTIME_NS_PATTERNS = tuple(
    pattern
    for pattern in LEAKED_COMPONENT_TEST_NS_PATTERNS
    if pattern.pattern.startswith(("^opt-125m", "^onnx-", "^vllm-", "^facebook-opt", "^test-vllm"))
)
_NS_GONE_POLL_SEC = 2
_NS_GONE_MAX_WAIT_SEC = 120


def _matches_model_runtime_test_namespace(name: str) -> bool:
    if name in _MODEL_RUNTIME_NS_EXACT:
        return True
    return any(pattern.match(name) for pattern in _MODEL_RUNTIME_NS_PATTERNS)


def _list_model_runtime_test_namespaces() -> list[str]:
    try:
        return sorted(
            name for name in list_cluster_namespace_names() if _matches_model_runtime_test_namespace(name)
        )
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


def cleanup_model_runtime_smoke_leaks() -> None:
    """Delete leaked vLLM pytest namespaces so fixtures can create them again."""
    for name in _list_model_runtime_test_namespaces():
        oc_run(
            ["delete", "namespace", name, "--ignore-not-found", "--wait=false"],
            check=False,
            capture_output=True,
            timeout=120,
        )
        if _wait_namespace_gone(name):
            print(f"✓ Removed stale model_runtime test namespace {name}", flush=True)
        else:
            print(f"⚠ Timed out waiting for namespace {name} to terminate", flush=True)
