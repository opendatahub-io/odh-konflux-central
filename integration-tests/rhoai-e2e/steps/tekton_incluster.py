"""Shared in-cluster Tekton/Kubernetes API helpers for rhoai-e2e pipeline steps."""

from __future__ import annotations

import ipaddress
import json
import os
import socket
import ssl
import subprocess
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

_LABEL_SNAPSHOT = "appstudio.openshift.io/snapshot"
_OC_SUBPROCESS_TIMEOUT_SEC = 60


def pipeline_run_name_from_env(*, required: bool = False) -> str:
    from steps.tekton_util import resolved_tekton_env_value

    for key in ("PIPELINE_RUN_NAME", "PIPELINERUN", "PIPELINE_RUN"):
        v = resolved_tekton_env_value(os.environ.get(key, ""))
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
    from steps.tekton_util import resolved_tekton_env_value

    p = Path("/var/run/secrets/kubernetes.io/serviceaccount/namespace")
    if p.is_file():
        file_value = p.read_text(encoding="utf-8").strip()
        if file_value:
            return file_value
    v = resolved_tekton_env_value(os.environ.get("NAMESPACE", ""))
    if v:
        return v
    v = resolved_tekton_env_value(os.environ.get("PIPELINE_NAMESPACE", ""))
    if v:
        return v
    if required:
        raise SystemExit("cannot determine namespace (no serviceAccount namespace file)")
    return ""


def _host_for_url(host: str) -> str:
    try:
        ip = ipaddress.ip_address(host.strip("[]"))
    except ValueError:
        return host
    return f"[{ip}]" if ip.version == 6 else str(ip)


def _resolve_host_ip(host: str) -> ipaddress.IPv4Address | ipaddress.IPv6Address:
    return ipaddress.ip_address(socket.getaddrinfo(host, None)[0][4][0])


def _is_allowed_kubernetes_api_host(host: str) -> bool:
    """True when *host* looks like the in-cluster API (private IP or cluster DNS name)."""
    h = host.strip().lower()
    if not h:
        return False
    try:
        ip = ipaddress.ip_address(h.strip("[]"))
        return ip.is_private or ip.is_loopback
    except ValueError:
        pass
    if h in ("kubernetes.default.svc", "kubernetes.default.svc.cluster.local"):
        return True
    if h.endswith(".svc") or h.endswith(".svc.cluster.local"):
        try:
            ip = _resolve_host_ip(host)
        except OSError:
            return False
        return ip.is_private or ip.is_loopback
    try:
        ip = _resolve_host_ip(host)
    except OSError:
        return False
    return ip.is_private or ip.is_loopback


def kubernetes_api_base_url() -> str | None:
    """``https://host:port`` for the in-cluster API, or ``None`` when env host is untrusted."""
    host = os.environ.get("KUBERNETES_SERVICE_HOST", "").strip()
    port = os.environ.get("KUBERNETES_SERVICE_PORT", "443").strip() or "443"
    if not host or not _is_allowed_kubernetes_api_host(host):
        return None
    return f"https://{_host_for_url(host)}:{port}"


def validate_kubernetes_api_url(url: str) -> None:
    parsed = urlparse(url)
    if parsed.scheme != "https":
        raise ValueError(f"refusing Kubernetes API request with non-HTTPS scheme: {parsed.scheme!r}")
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
    status, reason, _message = task_succeeded_detail(tr)
    if status == "True":
        return reason or "Succeeded"
    return reason


def task_succeeded_detail(tr: dict[str, Any]) -> tuple[str, str, str]:
    """``Succeeded`` condition: status (True/False/Unknown), reason, message."""
    for cond in (tr.get("status") or {}).get("conditions") or []:
        if not isinstance(cond, dict):
            continue
        if cond.get("type") == "Succeeded":
            return (
                str(cond.get("status") or "").strip(),
                str(cond.get("reason") or "").strip(),
                str(cond.get("message") or "").strip(),
            )
    return "", "", ""


def result_map(tr: dict[str, Any]) -> dict[str, str]:
    out: dict[str, str] = {}
    for r in (tr.get("status") or {}).get("results") or []:
        if not isinstance(r, dict):
            continue
        name, val = r.get("name"), r.get("value")
        if isinstance(name, str) and isinstance(val, str):
            out[name] = val
    return out


def list_child_pipeline_runs(
    pipeline_run: str,
    namespace: str,
    *,
    error_out: list[str] | None = None,
) -> list[str]:
    """Return names of nested PipelineRuns spawned by *pipeline_run* (Tekton pipelines-in-pipelines)."""
    token_path = Path("/var/run/secrets/kubernetes.io/serviceaccount/token")
    ca_path = Path("/var/run/secrets/kubernetes.io/serviceaccount/ca.crt")
    base = kubernetes_api_base_url()
    if not (pipeline_run and namespace and token_path.is_file() and ca_path.is_file() and base):
        return []
    token = token_path.read_text(encoding="utf-8")
    sel = urllib.parse.quote(f"tekton.dev/parentPipelineRun={pipeline_run}")
    url = (
        f"{base}/apis/tekton.dev/v1/namespaces/{urllib.parse.quote(namespace)}"
        f"/pipelineruns?labelSelector={sel}"
    )
    try:
        doc = in_cluster_get(url, token, ca_path)
    except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, OSError, ValueError) as exc:
        if error_out is not None:
            error_out.append(f"ERROR: list child PipelineRuns: {exc}")
        return []
    items = doc.get("items")
    if not isinstance(items, list):
        return []
    names: list[str] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        meta = item.get("metadata")
        if not isinstance(meta, dict):
            continue
        name = str(meta.get("name") or "").strip()
        if name:
            names.append(name)
    return names


def pipeline_run_creation_timestamp(
    pipeline_run: str | None = None,
    namespace: str | None = None,
) -> str:
    """Return PipelineRun ``metadata.creationTimestamp`` (RFC3339) from the in-cluster Tekton API."""
    pr_name = (pipeline_run or pipeline_run_name_from_env()).strip()
    ns = (namespace or namespace_from_env()).strip()
    token_path = Path("/var/run/secrets/kubernetes.io/serviceaccount/token")
    ca_path = Path("/var/run/secrets/kubernetes.io/serviceaccount/ca.crt")
    base = kubernetes_api_base_url()
    if not (pr_name and ns and token_path.is_file() and ca_path.is_file() and base):
        return ""
    token = token_path.read_text(encoding="utf-8")
    url = (
        f"{base}/apis/tekton.dev/v1/namespaces/{urllib.parse.quote(ns)}"
        f"/pipelineruns/{urllib.parse.quote(pr_name)}"
    )
    try:
        doc = in_cluster_get(url, token, ca_path)
    except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, OSError, ValueError):
        return ""
    meta = doc.get("metadata")
    if not isinstance(meta, dict):
        return ""
    return str(meta.get("creationTimestamp") or "").strip()


def _list_taskruns_for_pipeline_run(
    pipeline_run: str,
    namespace: str,
    token: str,
    ca_path: Path,
    base: str,
    *,
    error_out: list[str] | None = None,
) -> list[dict[str, Any]]:
    sel = urllib.parse.quote(f"tekton.dev/pipelineRun={pipeline_run}")
    url = (
        f"{base}/apis/tekton.dev/v1/namespaces/{urllib.parse.quote(namespace)}"
        f"/taskruns?labelSelector={sel}"
    )
    try:
        doc = in_cluster_get(url, token, ca_path)
    except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, OSError, ValueError) as exc:
        if error_out is not None:
            error_out.append(f"ERROR: list TaskRuns for {pipeline_run}: {exc}")
        return []
    items = doc.get("items")
    if not isinstance(items, list):
        if error_out is not None:
            error_out.append(f"ERROR: could not list TaskRuns for PipelineRun {pipeline_run}")
        return []
    return [x for x in items if isinstance(x, dict)]


def fetch_pipelinerun_in_cluster(
    pipeline_run: str,
    namespace: str,
    *,
    error_out: list[str] | None = None,
) -> dict[str, Any] | None:
    """Fetch a PipelineRun document from the in-cluster Tekton API."""
    pr_name = (pipeline_run or "").strip()
    ns = (namespace or "").strip()
    if not pr_name or not ns:
        return None
    creds = _pipelinerun_list_credentials()
    if creds is None:
        if error_out is not None:
            error_out.append("ERROR: in-cluster credentials unavailable for PipelineRun get")
        return None
    token, ca_path, base = creds
    url = (
        f"{base}/apis/tekton.dev/v1/namespaces/{urllib.parse.quote(ns)}"
        f"/pipelineruns/{urllib.parse.quote(pr_name)}"
    )
    try:
        doc = in_cluster_get(url, token, ca_path)
    except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, OSError, ValueError) as exc:
        if error_out is not None:
            error_out.append(f"ERROR: get PipelineRun {pr_name}: {exc}")
        return None
    return doc if isinstance(doc, dict) else None


def list_taskruns_in_cluster(
    pipeline_run: str,
    namespace: str,
    *,
    include_child_pipeline_runs: bool = True,
    error_out: list[str] | None = None,
) -> list[dict[str, Any]]:
    """List TaskRuns for a PipelineRun (and nested child PipelineRuns when requested)."""
    token_path = Path("/var/run/secrets/kubernetes.io/serviceaccount/token")
    ca_path = Path("/var/run/secrets/kubernetes.io/serviceaccount/ca.crt")
    base = kubernetes_api_base_url()
    if not (pipeline_run and namespace and token_path.is_file() and ca_path.is_file() and base):
        if error_out is not None and base is None and os.environ.get("KUBERNETES_SERVICE_HOST", "").strip():
            error_out.append("ERROR: KUBERNETES_SERVICE_HOST is missing or not an allowed in-cluster API host")
        return []
    token = token_path.read_text(encoding="utf-8")
    run_names = [pipeline_run]
    if include_child_pipeline_runs:
        run_names.extend(
            list_child_pipeline_runs(pipeline_run, namespace, error_out=error_out)
        )
    out: list[dict[str, Any]] = []
    for pr_name in run_names:
        out.extend(
            _list_taskruns_for_pipeline_run(
                pr_name, namespace, token, ca_path, base, error_out=error_out
            )
        )
    return out


def _pipelinerun_list_credentials() -> tuple[str, Path, str] | None:
    token_path = Path("/var/run/secrets/kubernetes.io/serviceaccount/token")
    ca_path = Path("/var/run/secrets/kubernetes.io/serviceaccount/ca.crt")
    base = kubernetes_api_base_url()
    if not (token_path.is_file() and ca_path.is_file() and base):
        return None
    return token_path.read_text(encoding="utf-8"), ca_path, base


def _list_pipelineruns_for_snapshot_oc(
    snapshot_name: str,
    namespace: str,
    *,
    error_out: list[str] | None = None,
) -> list[dict[str, Any]]:
    snap = (snapshot_name or "").strip()
    ns = (namespace or namespace_from_env()).strip()
    if not snap or not ns:
        return []
    try:
        proc = subprocess.run(
            ["oc", "get", "pipelinerun", "-n", ns, "-l", f"{_LABEL_SNAPSHOT}={snap}", "-o", "json"],
            capture_output=True,
            text=True,
            check=False,
            timeout=_OC_SUBPROCESS_TIMEOUT_SEC,
        )
    except (subprocess.TimeoutExpired, OSError) as exc:
        if error_out is not None:
            error_out.append(f"ERROR: oc list PipelineRuns for snapshot {snap}: {exc}")
        return []
    if proc.returncode != 0:
        if error_out is not None:
            err = (proc.stderr or proc.stdout or "").strip()
            error_out.append(f"ERROR: oc list PipelineRuns for snapshot {snap}: {err}")
        return []
    try:
        doc = json.loads(proc.stdout or "{}")
    except json.JSONDecodeError:
        if error_out is not None:
            error_out.append(f"ERROR: oc list PipelineRuns for snapshot {snap}: invalid JSON")
        return []
    items = doc.get("items")
    if not isinstance(items, list):
        return []
    return [x for x in items if isinstance(x, dict)]


def _pipeline_run_snapshot_label_oc(
    pipeline_run: str | None = None,
    namespace: str | None = None,
) -> str:
    pr_name = (pipeline_run or pipeline_run_name_from_env()).strip()
    ns = (namespace or namespace_from_env()).strip()
    if not pr_name or not ns:
        return ""
    try:
        proc = subprocess.run(
            [
                "oc",
                "get",
                "pipelinerun",
                pr_name,
                "-n",
                ns,
                "-o",
                f"jsonpath={{.metadata.labels['{_LABEL_SNAPSHOT}']}}",
            ],
            capture_output=True,
            text=True,
            check=False,
            timeout=_OC_SUBPROCESS_TIMEOUT_SEC,
        )
    except (subprocess.TimeoutExpired, OSError):
        return ""
    if proc.returncode != 0:
        return ""
    return (proc.stdout or "").strip()


def list_pipelineruns_for_snapshot(
    snapshot_name: str,
    namespace: str,
    *,
    error_out: list[str] | None = None,
) -> list[dict[str, Any]]:
    """List PipelineRuns labeled with ``appstudio.openshift.io/snapshot``."""
    snap = (snapshot_name or "").strip()
    ns = (namespace or namespace_from_env()).strip()
    if not snap or not ns:
        return []
    creds = _pipelinerun_list_credentials()
    if creds is None:
        return _list_pipelineruns_for_snapshot_oc(snap, ns, error_out=error_out)
    token, ca_path, base = creds
    sel = urllib.parse.quote(f"{_LABEL_SNAPSHOT}={snap}")
    url = (
        f"{base}/apis/tekton.dev/v1/namespaces/{urllib.parse.quote(ns)}"
        f"/pipelineruns?labelSelector={sel}"
    )
    try:
        doc = in_cluster_get(url, token, ca_path)
    except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, OSError, ValueError) as exc:
        oc_items = _list_pipelineruns_for_snapshot_oc(snap, ns, error_out=error_out)
        if oc_items:
            return oc_items
        if error_out is not None:
            error_out.append(f"ERROR: list PipelineRuns for snapshot {snap}: {exc}")
        return []
    items = doc.get("items")
    if not isinstance(items, list):
        oc_items = _list_pipelineruns_for_snapshot_oc(snap, ns, error_out=error_out)
        if oc_items:
            return oc_items
        return []
    return [x for x in items if isinstance(x, dict)]


def pipeline_run_snapshot_label(
    pipeline_run: str | None = None,
    namespace: str | None = None,
) -> str:
    """Read ``appstudio.openshift.io/snapshot`` from the current PipelineRun."""
    pr_name = (pipeline_run or pipeline_run_name_from_env()).strip()
    ns = (namespace or namespace_from_env()).strip()
    if not pr_name or not ns:
        return ""
    creds = _pipelinerun_list_credentials()
    if creds is not None:
        token, ca_path, base = creds
        url = (
            f"{base}/apis/tekton.dev/v1/namespaces/{urllib.parse.quote(ns)}"
            f"/pipelineruns/{urllib.parse.quote(pr_name)}"
        )
        try:
            doc = in_cluster_get(url, token, ca_path)
        except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, OSError, ValueError):
            doc = None
        else:
            meta = doc.get("metadata")
            if isinstance(meta, dict):
                labels = meta.get("labels")
                if isinstance(labels, dict):
                    label = str(labels.get(_LABEL_SNAPSHOT) or "").strip()
                    if label:
                        return label
    return _pipeline_run_snapshot_label_oc(pr_name, ns)


def fetch_snapshot_metadata(
    snapshot_name: str,
    namespace: str | None = None,
) -> tuple[dict[str, str], dict[str, str]]:
    """Return Snapshot ``metadata.labels`` and ``metadata.annotations`` as string dicts."""
    snap = (snapshot_name or "").strip()
    ns = (namespace or namespace_from_env()).strip()
    if not snap or not ns:
        return {}, {}

    doc: dict[str, Any] | None = None
    try:
        proc = subprocess.run(
            ["oc", "get", "snapshot", snap, "-n", ns, "-o", "json"],
            capture_output=True,
            text=True,
            check=False,
            timeout=_OC_SUBPROCESS_TIMEOUT_SEC,
        )
    except (subprocess.TimeoutExpired, OSError):
        proc = None
    if proc is not None and proc.returncode == 0:
        try:
            parsed = json.loads(proc.stdout or "{}")
        except json.JSONDecodeError:
            parsed = None
        if isinstance(parsed, dict):
            doc = parsed

    if doc is None:
        creds = _pipelinerun_list_credentials()
        if creds is None:
            return {}, {}
        token, ca_path, base = creds
        url = (
            f"{base}/apis/appstudio.redhat.com/v1alpha1/namespaces/{urllib.parse.quote(ns)}"
            f"/snapshots/{urllib.parse.quote(snap)}"
        )
        try:
            fetched = in_cluster_get(url, token, ca_path)
        except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, OSError, ValueError):
            return {}, {}
        doc = fetched if isinstance(fetched, dict) else None

    if doc is None:
        return {}, {}
    return _metadata_string_maps(doc.get("metadata"))


def _string_metadata_map(value: object) -> dict[str, str]:
    if not isinstance(value, dict):
        return {}
    return {
        key: val
        for key, val in value.items()
        if isinstance(key, str) and isinstance(val, str)
    }


def _metadata_string_maps(meta: object) -> tuple[dict[str, str], dict[str, str]]:
    if not isinstance(meta, dict):
        return {}, {}
    return _string_metadata_map(meta.get("labels")), _string_metadata_map(meta.get("annotations"))
