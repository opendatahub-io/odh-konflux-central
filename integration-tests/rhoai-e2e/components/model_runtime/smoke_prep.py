"""Free pooled-cluster capacity before model_runtime (vLLM) smoke."""

from __future__ import annotations

import json
import re
import time

from install.dsc_install import oc_run

# Fixture namespaces from opendatahub-tests model_runtime vLLM CPU/probe suites.
_MODEL_RUNTIME_TEST_NS_RE = re.compile(
    r"^(opt-125m-|onnx-|vllm-|facebook-opt-|test-vllm)"
)
_NS_GONE_POLL_SEC = 2
_NS_GONE_MAX_WAIT_SEC = 120


def _list_model_runtime_test_namespaces() -> list[str]:
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
        if _MODEL_RUNTIME_TEST_NS_RE.match(name):
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
