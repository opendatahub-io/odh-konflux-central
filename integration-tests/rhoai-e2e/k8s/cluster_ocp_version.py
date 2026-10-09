"""Read OpenShift cluster minor version from a kubeconfig file."""

from __future__ import annotations

import json
import os
import re
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

from k8s.oc_util import run_cmd

_OCP_MINOR_FROM_FULL = re.compile(r"^(\d+\.\d+)")


def ocp_minor_from_version_string(version: str) -> str:
    """``4.21.10`` or ``4.21`` → ``4.21``."""
    text = (version or "").strip()
    if not text:
        return ""
    match = _OCP_MINOR_FROM_FULL.match(text)
    return match.group(1) if match else ""


@contextmanager
def _kubeconfig_env(path: Path) -> Iterator[None]:
    prev = os.environ.get("KUBECONFIG")
    os.environ["KUBECONFIG"] = str(path)
    try:
        yield
    finally:
        if prev is None:
            os.environ.pop("KUBECONFIG", None)
        else:
            os.environ["KUBECONFIG"] = prev


def cluster_ocp_minor_from_kubeconfig(kubeconfig: Path) -> str:
    """Return cluster OCP ``MAJOR.MINOR`` or empty when unavailable."""
    if not kubeconfig.is_file():
        return ""
    with _kubeconfig_env(kubeconfig):
        proc = run_cmd(
            [
                "oc",
                "get",
                "clusterversion",
                "version",
                "-o",
                "jsonpath={.status.desired.version}",
            ],
            capture=True,
            check=False,
            timeout=30,
        )
        if proc.returncode == 0:
            minor = ocp_minor_from_version_string((proc.stdout or "").strip())
            if minor:
                return minor

        proc = run_cmd(["oc", "version", "-o", "json"], capture=True, check=False, timeout=30)
        if proc.returncode != 0 or not (proc.stdout or "").strip():
            return ""
        try:
            data = json.loads(proc.stdout)
        except json.JSONDecodeError:
            return ""
        server = data.get("serverVersion") if isinstance(data, dict) else {}
        if isinstance(server, dict):
            major = str(server.get("major", "")).strip().lstrip("v")
            minor = str(server.get("minor", "")).strip().lstrip("v")
            if major and minor:
                return ocp_minor_from_version_string(f"{major}.{minor}")
            minor = ocp_minor_from_version_string(str(server.get("gitVersion", "")))
            if minor:
                return minor
    return ""
