"""OpenShift `oc` subprocess helpers."""

from __future__ import annotations

import json
import re
import shutil
import subprocess
import time
from typing import Any
from urllib.parse import urlparse

from .errors import AppError

_OC_PATH = shutil.which("oc")
_DEFAULT_TIMEOUT_S = 180


def run_cmd(
    cmd: list[str],
    *,
    capture: bool = True,
    check: bool = True,
    input_text: str | None = None,
    env: dict[str, str] | None = None,
    timeout: float | None = _DEFAULT_TIMEOUT_S,
) -> subprocess.CompletedProcess[str]:
    exec_cmd = list(cmd)
    if exec_cmd and exec_cmd[0] == "oc":
        if not _OC_PATH:
            raise AppError("'oc' binary not found in PATH", 1)
        exec_cmd = [_OC_PATH, *exec_cmd[1:]]
    try:
        proc = subprocess.run(
            exec_cmd,
            text=True,
            input=input_text,
            capture_output=capture,
            env=env,
            check=False,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired as exc:
        raise AppError(f"Command timed out after {timeout}s: {' '.join(cmd)}", 1) from exc
    if check and proc.returncode != 0:
        stderr = (proc.stderr or "").strip()
        stdout = (proc.stdout or "").strip()
        detail = stderr or stdout or "<no output>"
        raise AppError(
            f"Command failed ({proc.returncode}): {' '.join(cmd)}\n{detail}",
            1,
        )
    return proc


def parse_json_output(cmd: list[str]) -> dict[str, Any]:
    proc = run_cmd(cmd, capture=True, check=False)
    if proc.returncode != 0 or not proc.stdout.strip():
        return {}
    try:
        return json.loads(proc.stdout)
    except json.JSONDecodeError:
        return {}


def get_jsonpath(cmd: list[str]) -> str:
    proc = run_cmd(cmd, capture=True, check=False)
    if proc.returncode != 0:
        return ""
    return proc.stdout.strip()


def ts_now() -> str:
    return time.strftime("%H:%M:%S")


def filter_warning_lines(text: str) -> str:
    return "\n".join(line for line in text.splitlines() if not line.startswith("Warning"))


def _openshift_apps_middle_labels_valid(middle: str) -> bool:
    """Reject api..openshiftapps.com, empty labels, and invalid DNS label chars."""
    if not middle or ".." in middle or middle.startswith(".") or middle.endswith("."):
        return False
    label_re = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$")
    for label in middle.split("."):
        if not label or not label_re.match(label):
            return False
    return True


def hosted_openshift_apps_cluster_suffix(api_server: str) -> str:
    """Return DNS suffix after ``api.`` for ``api.<cluster>.openshiftapps.com`` API servers, else ``\"\"``."""
    parsed = urlparse((api_server or "").strip())
    host = (parsed.hostname or "").lower()
    if not host.startswith("api.") or not host.endswith(".openshiftapps.com"):
        return ""
    tail = ".openshiftapps.com"
    middle = host[4 : -len(tail)]
    if not middle or not _openshift_apps_middle_labels_valid(middle):
        return ""
    return host[4:]


def derive_kubearchive_host(api_server: str) -> str:
    """
    Best-effort KubeArchive base URL for Konflux on hosted OpenShift (api.*.openshiftapps.com).

    Returns empty string when the API hostname does not match the expected pattern.
    """
    suffix = hosted_openshift_apps_cluster_suffix(api_server)
    if not suffix:
        return ""
    return f"https://kubearchive-api-server-product-kubearchive.apps.{suffix}"


def derive_konflux_ui_base(api_server: str) -> str:
    """Best-effort Konflux UI base URL (``https://konflux-ui.apps.<suffix>``) for the same pattern."""
    suffix = hosted_openshift_apps_cluster_suffix(api_server)
    if not suffix:
        return ""
    return f"https://konflux-ui.apps.{suffix}"
