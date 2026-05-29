"""Shared in-cluster Tekton/Kubernetes API helpers for olminstall pipeline steps."""

from __future__ import annotations

import ipaddress
import json
import os
import socket
import ssl
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any
from urllib.parse import urlparse


def pipeline_run_name_from_env(*, required: bool = False) -> str:
    for key in ("PIPELINE_RUN_NAME", "PIPELINERUN", "PIPELINE_RUN"):
        v = os.environ.get(key, "").strip()
        if v:
            return v
    p = Path("/etc/tekton/pipelineRunName")
    if p.is_file():
        file_value = p.read_text(encoding="utf-8").strip()
        if file_value:
            return file_value
    if required:
        raise SystemExit("PIPELINE_RUN_NAME missing (and no /etc/tekton/pipelineRunName)")
    return ""


def namespace_from_env(*, required: bool = False) -> str:
    p = Path("/var/run/secrets/kubernetes.io/serviceaccount/namespace")
    if p.is_file():
        file_value = p.read_text(encoding="utf-8").strip()
        if file_value:
            return file_value
    v = os.environ.get("NAMESPACE", "").strip()
    if v:
        return v
    if required:
        raise SystemExit("cannot determine namespace (no serviceAccount namespace file)")
    return ""


def _is_allowed_kubernetes_api_host(host: str) -> bool:
    """True when *host* looks like the in-cluster API (private IP or cluster DNS name)."""
    h = host.strip().lower()
    if not h:
        return False
    if h in ("kubernetes.default.svc", "kubernetes.default.svc.cluster.local"):
        return True
    if h.endswith(".svc") or h.endswith(".svc.cluster.local"):
        try:
            ip = ipaddress.ip_address(socket.gethostbyname(host))
        except OSError:
            return False
        return ip.is_private or ip.is_loopback
    try:
        ip = ipaddress.ip_address(socket.gethostbyname(host))
    except OSError:
        return False
    return ip.is_private or ip.is_loopback


def kubernetes_api_base_url() -> str | None:
    """``https://host:port`` for the in-cluster API, or ``None`` when env host is untrusted."""
    host = os.environ.get("KUBERNETES_SERVICE_HOST", "").strip()
    port = os.environ.get("KUBERNETES_SERVICE_PORT", "443").strip() or "443"
    if not host or not _is_allowed_kubernetes_api_host(host):
        return None
    return f"https://{host}:{port}"


def validate_kubernetes_api_url(url: str) -> None:
    parsed = urlparse(url)
    hostname = (parsed.hostname or "").strip()
    if not hostname or not _is_allowed_kubernetes_api_host(hostname):
        raise ValueError(f"refusing Kubernetes API request to untrusted host: {hostname!r}")


def in_cluster_get(url: str, token: str, ca_path: Path) -> dict[str, Any]:
    validate_kubernetes_api_url(url)
    ctx = ssl.create_default_context(cafile=str(ca_path))
    req = urllib.request.Request(url, headers={"Authorization": f"Bearer {token}"})
    with urllib.request.urlopen(req, context=ctx, timeout=60) as resp:
        data = json.loads(resp.read().decode("utf-8"))
    if not isinstance(data, dict):
        raise ValueError("expected JSON object from API")
    return data


def task_name(tr: dict[str, Any]) -> str:
    labels = (tr.get("metadata") or {}).get("labels") or {}
    if not isinstance(labels, dict):
        return ""
    return str(labels.get("tekton.dev/pipelineTask", "") or "")


def task_reason(tr: dict[str, Any]) -> str:
    for cond in (tr.get("status") or {}).get("conditions") or []:
        if not isinstance(cond, dict):
            continue
        if cond.get("type") == "Succeeded":
            return str(cond.get("reason") or "").strip()
    return ""


def result_map(tr: dict[str, Any]) -> dict[str, str]:
    out: dict[str, str] = {}
    for r in (tr.get("status") or {}).get("results") or []:
        if not isinstance(r, dict):
            continue
        name, val = r.get("name"), r.get("value")
        if isinstance(name, str) and isinstance(val, str):
            out[name] = val
    return out


def list_taskruns_in_cluster(
    pipeline_run: str,
    namespace: str,
    *,
    error_out: list[str] | None = None,
) -> list[dict[str, Any]]:
    """List TaskRuns for a PipelineRun. On API failure, return [] and append to *error_out* when set."""
    token_path = Path("/var/run/secrets/kubernetes.io/serviceaccount/token")
    ca_path = Path("/var/run/secrets/kubernetes.io/serviceaccount/ca.crt")
    base = kubernetes_api_base_url()
    if not (pipeline_run and namespace and token_path.is_file() and ca_path.is_file() and base):
        if error_out is not None and base is None and os.environ.get("KUBERNETES_SERVICE_HOST", "").strip():
            error_out.append("ERROR: KUBERNETES_SERVICE_HOST is missing or not an allowed in-cluster API host")
        return []
    token = token_path.read_text(encoding="utf-8")
    sel = urllib.parse.quote(f"tekton.dev/pipelineRun={pipeline_run}")
    url = (
        f"{base}/apis/tekton.dev/v1/namespaces/{urllib.parse.quote(namespace)}"
        f"/taskruns?labelSelector={sel}"
    )
    try:
        doc = in_cluster_get(url, token, ca_path)
    except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, OSError, ValueError) as exc:
        if error_out is not None:
            error_out.append(f"ERROR: list TaskRuns: {exc}")
        return []
    items = doc.get("items")
    if not isinstance(items, list):
        if error_out is not None:
            error_out.append("ERROR: could not list TaskRuns for this PipelineRun")
        return []
    return [x for x in items if isinstance(x, dict)]
