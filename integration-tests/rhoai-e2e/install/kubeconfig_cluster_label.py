#!/usr/bin/env python3
"""Derive a short cluster label from a kubeconfig (API URL, context, or cluster name)."""

from __future__ import annotations

import os
import re
import subprocess
import sys
from pathlib import Path
from urllib.parse import urlparse

# Jenkins common.getClusterNameFromUrl parity (vars/common.groovy).
_OPENSHIFT_CLUSTER_URL_RE = re.compile(r"https://(?:[^.]+)?(?:\.apps|api)\.(.*?)\.")


def cluster_name_from_url(cluster_url: str = "") -> str:
    """Extract cluster id from an OpenShift console or API HTTPS URL."""
    url = (cluster_url or "").strip()
    if not url:
        return ""
    match = _OPENSHIFT_CLUSTER_URL_RE.search(url)
    if match:
        return match.group(1)[:63]
    return ""


def normalize_api_server_host(api_server: str) -> str:
    """Lowercase API hostname for single-flight cluster matching."""
    parsed = urlparse((api_server or "").strip())
    host = (parsed.hostname or "").strip().lower()
    if host:
        return host[:253]
    netloc = (parsed.netloc or "").split("@")[-1].strip().lower()
    if netloc.startswith("[") and "]" in netloc:
        return netloc[1 : netloc.index("]")][:253]
    return netloc.split(":", 1)[0][:253] if netloc else ""


def _resolve_kubeconfig_file(kubeconfig: Path | str) -> Path | None:
    path = Path(kubeconfig).expanduser().resolve()
    return path if path.is_file() else None


def _load_kubeconfig_doc(path: Path) -> dict | None:
    try:
        import yaml
    except ImportError:
        return None
    try:
        doc = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError):
        return None
    return doc if isinstance(doc, dict) else None


def _kubeconfig_clusters_by_name(doc: dict) -> dict[str, dict]:
    clusters = doc.get("clusters") if isinstance(doc.get("clusters"), list) else []
    by_name: dict[str, dict] = {}
    for entry in clusters:
        if isinstance(entry, dict):
            name = str(entry.get("name") or "").strip()
            if name:
                by_name[name] = entry
    return by_name


def _server_from_cluster_entry(entry: dict) -> str:
    cluster_obj = entry.get("cluster")
    if isinstance(cluster_obj, dict):
        return str(cluster_obj.get("server") or "").strip()
    return ""


def _kubeconfig_current_cluster_entry(doc: dict, clusters_by_name: dict[str, dict]) -> dict | None:
    current = str(doc.get("current-context") or "").strip()
    contexts = doc.get("contexts") if isinstance(doc.get("contexts"), list) else []
    if current:
        for ctx in contexts:
            if not isinstance(ctx, dict) or str(ctx.get("name") or "").strip() != current:
                continue
            ctx_obj = ctx.get("context")
            if isinstance(ctx_obj, dict):
                cluster_ref = str(ctx_obj.get("cluster") or "").strip()
                entry = clusters_by_name.get(cluster_ref)
                if isinstance(entry, dict):
                    return entry
            break
    clusters = doc.get("clusters") if isinstance(doc.get("clusters"), list) else []
    if clusters and isinstance(clusters[0], dict):
        return clusters[0]
    return None


def _kubeconfig_current_cluster_server(path: Path) -> str:
    """API server URL from kubeconfig current context (YAML parse, no oc)."""
    doc = _load_kubeconfig_doc(path)
    if doc is None:
        return ""
    entry = _kubeconfig_current_cluster_entry(doc, _kubeconfig_clusters_by_name(doc))
    return _server_from_cluster_entry(entry) if entry is not None else ""


def _oc_kubeconfig_jsonpath(path: Path, jsonpath: str) -> str:
    env = {**os.environ, "KUBECONFIG": str(path)}
    proc = subprocess.run(
        ["oc", "config", "view", "--minify", "-o", f"jsonpath={jsonpath}"],
        capture_output=True,
        text=True,
        env=env,
        check=False,
    )
    return (proc.stdout or "").strip() if proc.returncode == 0 else ""


def cluster_lock_key_from_kubeconfig(kubeconfig: Path | str) -> str:
    """Canonical lock key: normalized API server hostname from kubeconfig."""
    path = _resolve_kubeconfig_file(kubeconfig)
    if path is None:
        return ""
    server = _kubeconfig_current_cluster_server(path) or _oc_kubeconfig_jsonpath(
        path, "{.clusters[0].cluster.server}"
    )
    return normalize_api_server_host(server) if server else ""


def cluster_name_from_dashboard_url(dashboard_url: str = "") -> str:
    """Extract cluster id from dashboard/gateway routes (incl. ROSA HCP ``apps.rosa.<id>``)."""
    url = (dashboard_url or "").strip()
    if not url:
        return ""
    host = url.split("//", 1)[-1].split("/", 1)[0]
    if ".apps.rosa." in host:
        cluster_id = host.split(".apps.rosa.", 1)[1].split(".")[0].strip()
        if cluster_id:
            return cluster_id[:63]
    return cluster_name_from_url(url)


def _label_from_cluster_entry(cluster_entry: dict) -> str:
    cluster_obj = cluster_entry.get("cluster")
    if isinstance(cluster_obj, dict):
        server = str(cluster_obj.get("server") or "").strip()
        if server:
            label = cluster_name_from_url(server)
            if label:
                return label
            label = _sanitize_cluster_label(server)
            if label:
                return label
    name = str(cluster_entry.get("name") or "").strip()
    if name:
        return _sanitize_cluster_label(name)
    return ""


def _cluster_label_from_kubeconfig_yaml(path: Path) -> str:
    """Parse kubeconfig YAML without ``oc`` (Tekton Cypress image may lack CLI)."""
    doc = _load_kubeconfig_doc(path)
    if doc is None:
        return ""
    clusters_by_name = _kubeconfig_clusters_by_name(doc)
    current = str(doc.get("current-context") or "").strip()
    contexts = doc.get("contexts") if isinstance(doc.get("contexts"), list) else []
    if current:
        for ctx in contexts:
            if not isinstance(ctx, dict) or str(ctx.get("name") or "").strip() != current:
                continue
            ctx_obj = ctx.get("context")
            if isinstance(ctx_obj, dict):
                cluster_ref = str(ctx_obj.get("cluster") or "").strip()
                if cluster_ref and cluster_ref in clusters_by_name:
                    label = _label_from_cluster_entry(clusters_by_name[cluster_ref])
                    if label:
                        return label
            label = _sanitize_cluster_label(current)
            if label:
                return label
            break

    entry = _kubeconfig_current_cluster_entry(doc, clusters_by_name)
    if entry is not None:
        label = _label_from_cluster_entry(entry)
        if label:
            return label

    for ctx in contexts:
        if not isinstance(ctx, dict):
            continue
        if current and str(ctx.get("name") or "").strip() != current:
            continue
        label = _sanitize_cluster_label(str(ctx.get("name") or "").strip())
        if label:
            return label
    if contexts and isinstance(contexts[0], dict):
        label = _sanitize_cluster_label(str(contexts[0].get("name") or "").strip())
        if label:
            return label
    return ""


def cluster_label_from_kubeconfig(kubeconfig: Path | str) -> str:
    """Return a short cluster label from kubeconfig; empty string if unavailable."""
    path = _resolve_kubeconfig_file(kubeconfig)
    if path is None:
        return ""
    for jsonpath in (
        "{.clusters[0].cluster.server}",
        "{.clusters[0].name}",
        "{.contexts[0].name}",
    ):
        raw = _oc_kubeconfig_jsonpath(path, jsonpath)
        if not raw:
            continue
        if jsonpath.endswith(".server}"):
            label = cluster_name_from_url(raw)
        else:
            label = _sanitize_cluster_label(raw)
        if label:
            return label
    return _cluster_label_from_kubeconfig_yaml(path)


def _cluster_label_from_cluster_source(
    cluster_source: str,
    *,
    dashboard_url: str = "",
) -> str:
    """Derive cluster id from tenant kubeconfig Secret name (``CLUSTER_SOURCE``)."""
    secret = (cluster_source or "").strip()
    if not secret or secret == "EPHC":
        return ""
    for prefix in ("rhoai-e2e-kubeconfig-", "kubeconfig-"):
        if secret.startswith(prefix):
            body = secret[len(prefix) :].strip("-") or secret
            break
    else:
        body = secret
    url_label = cluster_name_from_dashboard_url(dashboard_url)
    if url_label and (body == url_label or body.startswith(f"{url_label}-")):
        return url_label
    return _sanitize_cluster_label(body)


def resolve_cypress_cluster_label(
    kubeconfig: Path | str,
    *,
    cluster_source: str = "",
    dashboard_url: str = "",
) -> str:
    """Best-effort cluster label for dashboard Cypress TEST_CLUSTERS merge."""
    label = cluster_label_from_kubeconfig(kubeconfig)
    if label:
        return label
    source_label = _cluster_label_from_cluster_source(
        cluster_source,
        dashboard_url=dashboard_url,
    )
    if source_label:
        return source_label
    return cluster_name_from_dashboard_url(dashboard_url)


def _extract_from_api_cluster_token(token: str) -> str:
    """``api-ods-qe-psi-09-osp-…`` or ``api.ods-qe-psi-09.…`` → ``ods-qe-psi-09``."""
    token = token.split(":")[0].strip()
    if token.startswith("api-"):
        rest = token[4:]
        for marker in (
            "-osp-",
            "-p1.",
            "-p2.",
            "-p3.",
            "-hjvn.",
            "-p1-openshiftapps-",
            "-p2-openshiftapps-",
            "-p3-openshiftapps-",
        ):
            if marker in rest:
                return rest.split(marker)[0][:63]
        if "-rh-ods-" in rest:
            return rest.split("-rh-ods-")[0][:63]
    if token.startswith("api."):
        host = token[4:]
        if host.startswith("ods-qe-") or ".osp." in host:
            # api.ods-qe-psi-09.osp.rh-ods.com
            return host.split(".")[0][:63]
    return ""


def _sanitize_cluster_label(raw: str) -> str:
    """Drop hostnames/URLs; keep a short human-readable cluster id."""
    name = raw.strip()
    if not name:
        return ""
    if "://" in name:
        from_url = cluster_name_from_url(name)
        if from_url:
            return from_url
        return ""
    # oc login context: default/api-CLUSTER:6443/user → use api-CLUSTER segment
    if "/" in name:
        parts = list(reversed(name.split("/")))
        for part in parts:
            extracted = _extract_from_api_cluster_token(part)
            if extracted:
                return extracted
        for part in parts:
            if part and part not in ("default",) and ":" not in part and len(part) <= 63:
                return part[:63]
    extracted = _extract_from_api_cluster_token(name)
    if extracted:
        return extracted
    if name.startswith("api.") and name.count(".") >= 2:
        return name.split(".")[1][:63]
    if re.match(r"^[\w.-]+\.[\w.-]+\.", name):
        return name.split(".")[0][:63]
    return name[:63]


def main() -> int:
    kubeconfig = os.environ.get("KUBECONFIG", "").strip()
    out_path = os.environ.get("CLUSTER_NAME_PATH", "").strip()
    if not kubeconfig:
        print("KUBECONFIG is required", file=sys.stderr)
        return 1
    label = cluster_label_from_kubeconfig(kubeconfig)
    if not label:
        print("WARN: could not resolve cluster label from kubeconfig", file=sys.stderr)
        return 0
    print(f"External cluster: {label}")
    if out_path:
        Path(out_path).write_text(label, encoding="ascii")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
