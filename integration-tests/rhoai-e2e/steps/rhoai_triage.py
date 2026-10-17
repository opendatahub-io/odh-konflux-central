#!/usr/bin/env python3
"""ODH/RHOAI failure triage (Python port of ods-install collect-odh-rhoai-logs.sh).

Writes structured reports under ``<diag_dir>/triage/``: status, events, operator
highlights, pod logs since PipelineRun start, and a grep-based issues summary.

Env (optional):
    PIPELINE_RUN_START_TIME -- RFC3339 PipelineRun creationTimestamp for ``oc logs --since-time``
    DIAG_POD_LOG_MAX_BYTES -- max bytes per namespace for workload logs (default 524288)
    DIAG_ISSUES_SUMMARY_MAX_LINES -- default 500
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from steps.tekton_util import run

_OC = shutil.which("oc") or "oc"
_DEFAULT_MAX_BYTES = 524288
_DEFAULT_ISSUES_LINES = 500
_LOGS_SINCE_TIME_RE = re.compile(
    r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:\d{2})$"
)

ODH_RHOAI_NS_RE = re.compile(
    r"^(redhat-ods-applications|redhat-ods-operator|redhat-ods-monitoring|"
    r"rhoai-model-registries|rhods-notebooks|opendatahub|opendatahub-operators)$"
)
ODH_RHOAI_WORKLOAD_NS_RE = re.compile(
    r"^(redhat-ods-applications|redhat-ods-monitoring|rhoai-model-registries|"
    r"rhods-notebooks|opendatahub|opendatahub-operators)$"
)
INSTALL_DEPENDENCY_NS_RE = re.compile(
    r"^(kuadrant-system|rh-connectivity-link|openshift-keda|cert-manager|"
    r"openshift-marketplace|openshift-operators|redhat-ods-operator)$"
)
POD_DEPLOYMENT_PREFIX_RE = re.compile(r"^(?P<prefix>.+)-[a-z0-9]+$")

ISSUE_GREP_RE = re.compile(
    r"error|fail|retry|webhook|no endpoints|Reconciler error|provisioning failed|"
    r"CrashLoop|NotFound|InstallPlanFailed|CatalogSourceUnhealthy|WARNING|Warning|"
    r"phase=Failed|ready=[0-9]+/[1-9]",
    re.IGNORECASE,
)
ISSUE_GREP_EXCLUDE_RE = re.compile(
    r"Registering webhook|Conversion webhook enabled|Starting webhook server|"
    r"Serving webhook server|^--- |^==========",
    re.IGNORECASE,
)
OPERATOR_HIGHLIGHT_RE = re.compile(
    r"error|fail|retry|webhook|no endpoints|Reconciler error|provisioning failed|"
    r"CrashLoop|NotFound",
    re.IGNORECASE,
)


def _oc(args: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
    return run([_OC, *args], check=False, capture=True, **kwargs)  # type: ignore[arg-type]


def _oc_json(args: list[str]) -> dict[str, Any]:
    proc = _oc([*args, "-o", "json"])
    if proc.returncode != 0 or not (proc.stdout or "").strip():
        return {}
    try:
        return json.loads(proc.stdout)
    except json.JSONDecodeError:
        return {}


def _safe_filename(name: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]+", "_", name.strip()) or "unknown"


def _is_valid_logs_since_time(val: str) -> bool:
    return bool(val) and _LOGS_SINCE_TIME_RE.match(val) and "$(" not in val


def resolve_logs_since_time(raw: str | None = None) -> str:
    """Return RFC3339 timestamp for ``oc logs --since-time`` (PipelineRun start)."""
    val = (raw or os.environ.get("PIPELINE_RUN_START_TIME", "")).strip()
    if _is_valid_logs_since_time(val):
        return val

    from steps.tekton_incluster import pipeline_run_creation_timestamp

    fetched = pipeline_run_creation_timestamp().strip()
    if _is_valid_logs_since_time(fetched):
        print(
            f"Using PipelineRun creationTimestamp from in-cluster API: {fetched}",
            flush=True,
        )
        return fetched

    if val and "$(" in val:
        fallback = (
            datetime.now(timezone.utc) - timedelta(hours=3)
        ).strftime("%Y-%m-%dT%H:%M:%SZ")
        print(
            f"WARN: PIPELINE_RUN_START_TIME={val!r} unavailable; "
            f"using logs since-time fallback {fallback}",
            flush=True,
        )
        return fallback

    if val:
        raise ValueError(
            f"invalid PIPELINE_RUN_START_TIME {val!r}; expected RFC3339 UTC timestamp"
        )
    raise ValueError(
        "PIPELINE_RUN_START_TIME is required (PipelineRun creationTimestamp, RFC3339) "
        "and could not be loaded from the in-cluster Tekton API"
    )


def odh_rhoai_namespaces() -> list[str]:
    data = _oc_json(["get", "ns"])
    names = [
        item["metadata"]["name"]
        for item in data.get("items", [])
        if ODH_RHOAI_NS_RE.match(item.get("metadata", {}).get("name", ""))
    ]
    return sorted(names)


def install_dependency_namespaces() -> list[str]:
    """Namespaces used by install-dep-operators (Kuadrant/Authorino/RHCL/KEDA/cert-manager/OLM)."""
    data = _oc_json(["get", "ns"])
    names = [
        item["metadata"]["name"]
        for item in data.get("items", [])
        if INSTALL_DEPENDENCY_NS_RE.match(item.get("metadata", {}).get("name", ""))
    ]
    return sorted(names)


def needs_dependency_install_diagnostics(operator_ns: str) -> bool:
    """True when RHOAI is not installed yet or dependency install failed."""
    if os.environ.get("INSTALL_DEP_OPERATORS_STATUS", "").strip() == "Failed":
        return True
    if not odh_rhoai_namespaces():
        return True
    sub_json = _oc_json(["get", "subscription", "-n", operator_ns])
    return not sub_json.get("items")


def load_cluster_pods_snapshot() -> dict[str, Any]:
    return _oc_json(["get", "pods", "-A"])


def _fetch_pod_logs(since_time: str, namespace: str, pod: str) -> tuple[str, bool]:
    proc = _oc(
        [
            "logs",
            f"--since-time={since_time}",
            "--all-containers",
            pod,
            "-n",
            namespace,
        ]
    )
    if proc.returncode != 0:
        err = (proc.stderr or proc.stdout or "").strip().replace("\n", " ")
        return (
            f"ERROR: cannot export logs for pod {namespace}/{pod}: {err}\n",
            False,
        )
    return proc.stdout or "", True


def _conditions_lines(conditions: list[dict[str, Any]] | None) -> list[str]:
    if not conditions:
        return ["(no conditions)"]
    return [
        f"{c.get('type')}: status={c.get('status')} "
        f"reason={c.get('reason') or ''} message={c.get('message') or ''}"
        for c in conditions
    ]


def write_status_report(
    dest: Path,
    *,
    operator_ns: str,
    pods_json: dict[str, Any],
) -> None:
    lines: list[str] = []
    lines.append(
        f"========== ODH/RHOAI STATUS REPORT ({datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')}) =========="
    )
    lines.append("")

    lines.append("--- OpenShift / OCP version ---")
    ver = _oc(["version"])
    lines.append((ver.stdout or ver.stderr or "(oc version unavailable)").strip())

    lines.append("")
    lines.append("--- Namespaces ---")
    ns_list = odh_rhoai_namespaces()
    if ns_list:
        lines.extend(f"namespace/{ns}" for ns in ns_list)
    else:
        lines.append("(no ODH/RHOAI namespaces found)")

    lines.append("")
    lines.append(f"--- Subscriptions ({operator_ns}) ---")
    sub = _oc(["get", "subscription", "-n", operator_ns, "-o", "wide"])
    lines.append((sub.stdout or sub.stderr or "(unable to list subscriptions)").strip())
    lines.append("")
    lines.append("Subscription conditions (unhealthy):")
    sub_json = _oc_json(["get", "subscription", "-n", operator_ns])
    unhealthy: list[str] = []
    for item in sub_json.get("items", []):
        for cond in item.get("status", {}).get("conditions") or []:
            ctype = cond.get("type", "")
            if cond.get("status") == "False" or re.search(
                r"Failed|Error|Unhealthy", ctype, re.IGNORECASE
            ):
                unhealthy.append(
                    f"{ctype}: status={cond.get('status')} "
                    f"reason={cond.get('reason') or ''} message={cond.get('message') or ''}"
                )
    lines.extend(unhealthy or ["(no unhealthy subscription conditions)"])

    lines.append("")
    lines.append(f"--- ClusterServiceVersions ({operator_ns}) ---")
    csv_wide = _oc(["get", "csv", "-n", operator_ns, "-o", "wide"])
    lines.append((csv_wide.stdout or csv_wide.stderr or "(unable to list CSVs)").strip())

    lines.append("")
    lines.append("--- CSVs not in Succeeded phase ---")
    csv_json = _oc_json(["get", "csv", "-n", operator_ns])
    bad_csv: list[str] = []
    for item in csv_json.get("items", []):
        phase = (item.get("status") or {}).get("phase") or ""
        if phase != "Succeeded":
            status = item.get("status") or {}
            bad_csv.append(
                f"{item.get('metadata', {}).get('name')}: phase={phase or 'unknown'} "
                f"reason={status.get('reason') or ''} message={status.get('message') or ''}"
            )
    lines.extend(bad_csv or ["(all CSVs report phase Succeeded)"])

    lines.append("")
    lines.append(f"--- InstallPlans ({operator_ns}, not Complete) ---")
    ip_json = _oc_json(["get", "installplan", "-n", operator_ns])
    bad_ip: list[str] = []
    for item in ip_json.get("items", []):
        phase = (item.get("status") or {}).get("phase") or ""
        if phase != "Complete":
            bad_ip.append(
                f"{item.get('metadata', {}).get('name')}: phase={phase or 'unknown'}"
            )
    lines.extend(bad_ip or ["(no non-Complete InstallPlans)"])

    lines.append("")
    lines.append("--- DataScienceCluster ---")
    dsc_wide = _oc(["get", "datasciencecluster", "-A", "-o", "wide"])
    lines.append((dsc_wide.stdout or "(no DataScienceCluster)").strip())
    dsc = _oc_json(["get", "datasciencecluster", "default-dsc"])
    if dsc.get("metadata"):
        lines.append("")
        lines.append("DSC Ready summary:")
        ready = _oc(
            [
                "get",
                "datasciencecluster",
                "default-dsc",
                "-o",
                "jsonpath={.status.conditions[?(@.type==\"Ready\")].status}{\" reason=\"}"
                "{.status.conditions[?(@.type==\"Ready\")].reason}{\"\\n\"}",
            ]
        )
        lines.append((ready.stdout or "").strip() or "(Ready condition missing)")
        lines.append("")
        lines.append("DSC component states:")
        components = (dsc.get("status") or {}).get("components") or {}
        if components:
            for key, val in sorted(components.items()):
                mgmt = (val or {}).get("managementState", "n/a")
                lines.append(f"{key}: managementState={mgmt}")
        else:
            lines.append("(no component states)")
        lines.append("")
        lines.append("DSC conditions:")
        lines.extend(_conditions_lines((dsc.get("status") or {}).get("conditions")))

    lines.append("")
    lines.append("--- DSCInitialization ---")
    dsci_wide = _oc(["get", "dscinitialization", "-A", "-o", "wide"])
    lines.append((dsci_wide.stdout or "(no DSCInitialization)").strip())
    dsci = _oc_json(["get", "dscinitialization", "default-dsci"])
    if dsci.get("metadata"):
        lines.append("")
        lines.append("DSCInitialization conditions:")
        lines.extend(_conditions_lines((dsci.get("status") or {}).get("conditions")))

    lines.append("")
    lines.append("--- Component CRs (Ready) ---")
    component_names = sorted(
        {
            line.split(".")[0].strip()
            for line in (_oc(["api-resources", "-o", "name"]).stdout or "").splitlines()
            if ".components." in line and line.strip()
        }
    )
    if component_names:
        get_proc = _oc(["get", ",".join(component_names), "-A"])
        lines.append((get_proc.stdout or get_proc.stderr or "(unable to list component CRs)").strip())
    else:
        lines.append("(no component CR API resources found under components.* API groups)")

    lines.append("")
    lines.append("--- Deployments not fully ready (ODH/RHOAI namespaces) ---")
    deploy_json = _oc_json(["get", "deploy", "-A"])
    bad_deploy: list[str] = []
    for item in deploy_json.get("items", []):
        ns = item.get("metadata", {}).get("namespace", "")
        if not ODH_RHOAI_NS_RE.match(ns):
            continue
        spec_rep = item.get("spec", {}).get("replicas", 0)
        status = item.get("status") or {}
        ready = status.get("readyReplicas") or 0
        if ready != spec_rep:
            bad_deploy.append(
                f"{ns}/{item.get('metadata', {}).get('name')}: "
                f"ready={ready}/{spec_rep} available={status.get('availableReplicas') or 0}"
            )
    lines.extend(bad_deploy or ["(all deployments report ready replicas == desired)"])

    lines.append("")
    lines.append("--- Pods not Running/Succeeded (ODH/RHOAI namespaces) ---")
    bad_pods: list[str] = []
    for item in pods_json.get("items", []):
        ns = item.get("metadata", {}).get("namespace", "")
        if not ODH_RHOAI_NS_RE.match(ns):
            continue
        phase = item.get("status", {}).get("phase", "")
        if phase not in ("Running", "Succeeded"):
            bad_pods.append(
                f"{ns}/{item.get('metadata', {}).get('name')}: "
                f"phase={phase} reason={item.get('status', {}).get('reason') or ''}"
            )
    lines.extend(bad_pods or ["(all pods Running or Succeeded)"])

    lines.append("")
    lines.append("---------------------------------------------------------------------------------------------------")
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text("\n".join(lines) + "\n", encoding="utf-8")


def write_dependency_install_status(
    dest: Path,
    *,
    operator_ns: str,
    pods_json: dict[str, Any],
) -> None:
    """Status for dependency-operator install (before RHOAI namespaces exist)."""
    lines: list[str] = []
    lines.append(
        f"========== DEPENDENCY INSTALL STATUS ({datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')}) =========="
    )
    lines.append("")
    dep_ns = install_dependency_namespaces()
    lines.append("--- Dependency install namespaces ---")
    if dep_ns:
        lines.extend(f"namespace/{ns}" for ns in dep_ns)
    else:
        lines.append("(no dependency install namespaces found on cluster)")

    for ns in dep_ns:
        lines.append("")
        lines.append(f"--- Subscriptions ({ns}) ---")
        sub = _oc(["get", "subscription", "-n", ns, "-o", "wide"])
        lines.append((sub.stdout or sub.stderr or "(unable to list subscriptions)").strip())
        lines.append("")
        lines.append(f"--- ClusterServiceVersions ({ns}) ---")
        csv = _oc(["get", "csv", "-n", ns, "-o", "wide"])
        lines.append((csv.stdout or csv.stderr or "(unable to list CSVs)").strip())
        csv_json = _oc_json(["get", "csv", "-n", ns])
        bad_csv: list[str] = []
        for item in csv_json.get("items", []):
            phase = (item.get("status") or {}).get("phase") or ""
            if phase != "Succeeded":
                status = item.get("status") or {}
                bad_csv.append(
                    f"{item.get('metadata', {}).get('name')}: phase={phase or 'unknown'} "
                    f"reason={status.get('reason') or ''} message={status.get('message') or ''}"
                )
        if bad_csv:
            lines.append("")
            lines.append(f"CSVs not Succeeded in {ns}:")
            lines.extend(bad_csv)

    lines.append("")
    lines.append("--- Authorino / Kuadrant CRs ---")
    for ns in ("kuadrant-system", "rh-connectivity-link"):
        for kind in ("authorino", "kuadrant"):
            proc = _oc(["get", kind, "-n", ns])
            body = (proc.stdout or proc.stderr or "").strip()
            if body and "No resources found" not in body:
                lines.append(f"{kind} in {ns}:")
                lines.append(body)

    lines.append("")
    lines.append(f"--- RHOAI operator namespace ({operator_ns}) ---")
    sub = _oc(["get", "subscription", "-n", operator_ns, "-o", "wide"])
    lines.append((sub.stdout or sub.stderr or "(unable to list subscriptions)").strip())

    lines.append("")
    lines.append("--- Deployments not fully ready (dependency namespaces) ---")
    deploy_json = _oc_json(["get", "deploy", "-A"])
    bad_deploy: list[str] = []
    for item in deploy_json.get("items", []):
        ns = item.get("metadata", {}).get("namespace", "")
        if not INSTALL_DEPENDENCY_NS_RE.match(ns):
            continue
        spec_rep = item.get("spec", {}).get("replicas", 0)
        status = item.get("status") or {}
        ready = status.get("readyReplicas") or 0
        if ready != spec_rep:
            bad_deploy.append(
                f"{ns}/{item.get('metadata', {}).get('name')}: "
                f"ready={ready}/{spec_rep} available={status.get('availableReplicas') or 0}"
            )
    lines.extend(bad_deploy or ["(all deployments report ready replicas == desired)"])

    lines.append("")
    lines.append("--- Pods not Running/Succeeded (dependency namespaces) ---")
    bad_pods: list[str] = []
    for item in pods_json.get("items", []):
        ns = item.get("metadata", {}).get("namespace", "")
        if not INSTALL_DEPENDENCY_NS_RE.match(ns):
            continue
        phase = item.get("status", {}).get("phase", "")
        if phase not in ("Running", "Succeeded"):
            reason = item.get("status", {}).get("reason") or ""
            message = ""
            for cs in item.get("status", {}).get("containerStatuses") or []:
                waiting = cs.get("state", {}).get("waiting") or {}
                if waiting:
                    message = waiting.get("message") or waiting.get("reason") or message
            bad_pods.append(
                f"{ns}/{item.get('metadata', {}).get('name')}: "
                f"phase={phase} reason={reason} {message}".strip()
            )
    lines.extend(bad_pods or ["(all pods Running or Succeeded)"])

    lines.append("")
    lines.append("---------------------------------------------------------------------------------------------------")
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text("\n".join(lines) + "\n", encoding="utf-8")


def write_dependency_events(dest: Path, namespaces: list[str]) -> None:
    lines = [
        "",
        "========== DEPENDENCY INSTALL EVENTS (Warning / Error / Failed) ==========",
        "",
    ]
    for ns in namespaces:
        lines.append(f"--- namespace/{ns} ---")
        proc = _oc(["get", "events", "-n", ns, "--sort-by=.lastTimestamp"])
        if proc.returncode != 0:
            lines.append("(unable to list events)")
        else:
            event_lines = (proc.stdout or "").splitlines()
            if not event_lines:
                lines.append("(no events)")
            else:
                for row in event_lines:
                    if row == event_lines[0] or re.search(
                        r"Warning|Error|Failed", row
                    ):
                        lines.append(row)
        lines.append("")
    lines.append("---------------------------------------------------------------------------------------------------")
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _dependency_pods_sorted(
    pods_json: dict[str, Any],
    namespaces: set[str],
) -> list[tuple[str, str, str]]:
    pods: list[tuple[str, str, str, int, str]] = []
    for item in pods_json.get("items", []):
        ns = item.get("metadata", {}).get("namespace", "")
        if ns not in namespaces:
            continue
        name = item.get("metadata", {}).get("name", "")
        phase = item.get("status", {}).get("phase", "")
        bad = 1 if phase in ("Running", "Succeeded") else 0
        ts = item.get("metadata", {}).get("creationTimestamp") or ""
        pods.append((bad, ts, ns, name, phase))
    pods.sort(key=lambda row: (row[0], row[1]))
    return [(ns, name, phase) for _, _, ns, name, phase in pods]


def collect_dependency_install_logs(
    triage_dir: Path,
    *,
    since_time: str,
    pods_json: dict[str, Any],
    max_bytes_per_ns: int,
) -> Path:
    """Collect pod logs from dependency install namespaces; return highlights path."""
    dep_ns = set(install_dependency_namespaces())
    operator_ns = os.environ.get("OPERATOR_NAMESPACE", "").strip()
    if operator_ns:
        dep_ns.add(operator_ns)

    logs_dir = triage_dir / "dependency-logs"
    logs_dir.mkdir(parents=True, exist_ok=True)
    highlights_path = triage_dir / "dependency-operator-highlights.txt"
    highlight_lines = [
        "",
        "========== DEPENDENCY OPERATOR LOG HIGHLIGHTS ==========",
        "",
        f"Source: pods in {', '.join(sorted(dep_ns))} (since-time={since_time})",
        "",
    ]
    summary_lines = [
        f"since-time={since_time} max_bytes_per_ns={max_bytes_per_ns}",
        "",
        "========== DEPENDENCY INSTALL POD LOGS ==========",
        "",
    ]
    ns_bytes: dict[str, int] = {}
    found = False

    for namespace, pod, phase in _dependency_pods_sorted(pods_json, dep_ns):
        used = ns_bytes.get(namespace, 0)
        if used >= max_bytes_per_ns:
            summary_lines.append(
                f"... truncated {namespace} after {max_bytes_per_ns} bytes"
            )
            continue
        body, ok = _fetch_pod_logs(since_time, namespace, pod)
        rel_name = f"{_safe_filename(namespace)}/{_safe_filename(pod)}.log"
        dest = logs_dir / rel_name
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text(body, encoding="utf-8")
        size = len(body.encode("utf-8", errors="replace"))
        ns_bytes[namespace] = used + size
        summary_lines.append(
            f"  {namespace}/{pod} phase={phase} -> {rel_name} ({size} bytes)"
        )
        if not ok:
            found = True
            continue
        matches = [ln for ln in body.splitlines() if OPERATOR_HIGHLIGHT_RE.search(ln)]
        if matches:
            found = True
            highlight_lines.append(f"--- {namespace}/{pod} (phase={phase}) ---")
            highlight_lines.extend(matches)
            highlight_lines.append("")

    if not found:
        highlight_lines.append(
            "(no matching error/webhook lines in dependency operator logs for this time window)"
        )
    highlight_lines.append(
        "---------------------------------------------------------------------------------------------------"
    )
    highlights_path.write_text("\n".join(highlight_lines) + "\n", encoding="utf-8")
    (logs_dir / "collection-summary.txt").write_text(
        "\n".join(summary_lines) + "\n", encoding="utf-8"
    )
    return highlights_path


def write_events(dest: Path, namespaces: list[str]) -> None:
    lines = [
        "",
        "========== ODH/RHOAI EVENTS (Warning / Error / Failed) ==========",
        "",
    ]
    for ns in namespaces:
        lines.append(f"--- namespace/{ns} ---")
        proc = _oc(["get", "events", "-n", ns, "--sort-by=.lastTimestamp"])
        if proc.returncode != 0:
            lines.append("(unable to list events)")
        else:
            event_lines = (proc.stdout or "").splitlines()
            if not event_lines:
                lines.append("(no events)")
            else:
                for row in event_lines:
                    if row == event_lines[0] or re.search(
                        r"Warning|Error|Failed", row
                    ):
                        lines.append(row)
        lines.append("")
    lines.append("---------------------------------------------------------------------------------------------------")
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _operator_pod_names(operator_ns: str) -> list[str]:
    proc = _oc(
        [
            "get",
            "pods",
            "-n",
            operator_ns,
            "-l",
            "name=rhods-operator",
            "-o",
            "jsonpath={range .items[*]}{.metadata.name}{\"\\n\"}{end}",
        ]
    )
    return [ln.strip() for ln in (proc.stdout or "").splitlines() if ln.strip()]


def collect_operator_logs_and_highlights(
    triage_dir: Path,
    *,
    operator_ns: str,
    since_time: str,
) -> tuple[Path, Path]:
    logs_dir = triage_dir / "operator-logs"
    logs_dir.mkdir(parents=True, exist_ok=True)
    highlights_path = triage_dir / "operator-highlights.txt"

    highlight_lines = [
        "",
        "========== ODH/RHOAI LOG HIGHLIGHTS (errors, webhooks, reconcile failures) ==========",
        "",
        f"Source: rhods-operator pods in {operator_ns} (since-time={since_time})",
        "",
    ]
    found = False
    for pod in _operator_pod_names(operator_ns):
        body, ok = _fetch_pod_logs(since_time, operator_ns, pod)
        log_path = logs_dir / f"{_safe_filename(pod)}.log"
        log_path.write_text(body, encoding="utf-8")
        if not ok:
            found = True
            continue
        matches = [
            ln
            for ln in body.splitlines()
            if OPERATOR_HIGHLIGHT_RE.search(ln)
        ]
        if matches:
            found = True
            highlight_lines.append(f"--- {operator_ns}/{pod} ---")
            highlight_lines.extend(matches)
            highlight_lines.append("")

    if not found:
        highlight_lines.append(
            "(no matching error/webhook lines in operator logs for this time window)"
        )
    highlight_lines.append(
        "---------------------------------------------------------------------------------------------------"
    )
    highlights_path.write_text("\n".join(highlight_lines) + "\n", encoding="utf-8")
    return logs_dir, highlights_path


def _latest_pod_per_prefix(
    pods_json: dict[str, Any],
    ns_re: re.Pattern[str],
) -> list[tuple[str, str]]:
    groups: dict[str, list[dict[str, Any]]] = {}
    for item in pods_json.get("items", []):
        ns = item.get("metadata", {}).get("namespace", "")
        name = item.get("metadata", {}).get("name", "")
        if not ns_re.match(ns):
            continue
        match = POD_DEPLOYMENT_PREFIX_RE.match(name)
        if not match:
            continue
        key = f"{ns} {match.group('prefix')}"
        groups.setdefault(key, []).append(item)

    selected: list[tuple[str, str]] = []
    for group in groups.values():
        latest = max(
            group,
            key=lambda p: p.get("metadata", {}).get("creationTimestamp") or "",
        )
        selected.append(
            (latest["metadata"]["namespace"], latest["metadata"]["name"])
        )
    return sorted(selected)


def write_skipped_workload_pods(
    dest: Path,
    pods_json: dict[str, Any],
    selected: set[tuple[str, str]],
) -> None:
    lines = [
        "",
        "--- Workload pods not selected for logs (name pattern or duplicate prefix group) ---",
    ]
    skipped: list[str] = []
    for item in pods_json.get("items", []):
        ns = item.get("metadata", {}).get("namespace", "")
        name = item.get("metadata", {}).get("name", "")
        if not ODH_RHOAI_WORKLOAD_NS_RE.match(ns):
            continue
        pod_id = (ns, name)
        if pod_id in selected:
            continue
        if not POD_DEPLOYMENT_PREFIX_RE.match(name):
            skipped.append(
                f"{ns}/{name}: skipped (pod name does not match deployment-prefix-hash pattern)"
            )
        else:
            skipped.append(
                f"{ns}/{name}: skipped (older replica for same deployment prefix)"
            )
    lines.extend(skipped or ["(all pods in scope are either selected or listed above)"])
    dest.write_text("\n".join(lines) + "\n", encoding="utf-8")


def collect_workload_pod_logs(
    triage_dir: Path,
    *,
    since_time: str,
    pods_json: dict[str, Any],
    max_bytes_per_ns: int,
) -> Path:
    out_root = triage_dir / "workload-logs"
    out_root.mkdir(parents=True, exist_ok=True)
    selected_pairs = _latest_pod_per_prefix(pods_json, ODH_RHOAI_WORKLOAD_NS_RE)
    selected_set = set(selected_pairs)

    summary_lines = [
        f"since-time={since_time} max_bytes_per_ns={max_bytes_per_ns}",
        "",
        "========== ODH/RHOAI POD LOGS (workloads) ==========",
        "",
    ]
    ns_bytes: dict[str, int] = {}

    for namespace, pod in selected_pairs:
        ns_dir = out_root / _safe_filename(namespace)
        ns_dir.mkdir(parents=True, exist_ok=True)
        used = ns_bytes.get(namespace, 0)
        if used >= max_bytes_per_ns:
            summary_lines.append(
                f"... truncated {namespace} after {max_bytes_per_ns} bytes"
            )
            continue
        body, ok = _fetch_pod_logs(since_time, namespace, pod)
        dest = ns_dir / f"{_safe_filename(pod)}.log"
        dest.write_text(body, encoding="utf-8")
        ns_bytes[namespace] = used + len(body.encode("utf-8", errors="replace"))
        summary_lines.append(
            f"  {namespace}/{pod} -> {dest.relative_to(out_root)} ({len(body)} chars)"
        )
        if not ok:
            summary_lines.append("    (log fetch failed; see file)")

    write_skipped_workload_pods(triage_dir / "skipped-pods.txt", pods_json, selected_set)
    summary_path = out_root / "collection-summary.txt"
    summary_path.write_text("\n".join(summary_lines) + "\n", encoding="utf-8")
    return out_root


def build_issues_summary(
    dest: Path,
    *,
    status_report: Path,
    events: Path,
    operator_highlights: Path,
    max_lines: int,
    extra_reports: tuple[Path, ...] = (),
) -> str:
    chunks: list[str] = []
    for path in (status_report, events, operator_highlights, *extra_reports):
        if path.is_file():
            chunks.append(path.read_text(encoding="utf-8", errors="replace"))

    matched: list[str] = []
    for line in "\n".join(chunks).splitlines():
        if ISSUE_GREP_RE.search(line) and not ISSUE_GREP_EXCLUDE_RE.search(line):
            matched.append(line)
        if len(matched) >= max_lines:
            break

    body_lines = [
        "",
        "========== ODH/RHOAI ISSUES SUMMARY (status, events, highlights only) ==========",
        "",
    ]
    body_lines.extend(matched or ["(no issue patterns matched in status/events/highlights)"])
    body_lines.append("")
    body_lines.append(
        "---------------------------------------------------------------------------------------------------"
    )
    text = "\n".join(body_lines) + "\n"
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text(text, encoding="utf-8")
    return text


def run_rhoai_triage(
    diag_dir: Path,
    *,
    operator_ns: str,
    logs_since_time: str | None = None,
    max_bytes_per_ns: int | None = None,
    issues_max_lines: int | None = None,
) -> Path:
    """Collect triage artifacts; return path to issues-summary.txt."""
    since_time = resolve_logs_since_time(logs_since_time)
    max_bytes = max_bytes_per_ns
    if max_bytes is None:
        raw = os.environ.get("DIAG_POD_LOG_MAX_BYTES", str(_DEFAULT_MAX_BYTES)).strip()
        max_bytes = int(raw or str(_DEFAULT_MAX_BYTES))
    issues_lines = issues_max_lines
    if issues_lines is None:
        raw_issues = os.environ.get(
            "DIAG_ISSUES_SUMMARY_MAX_LINES", str(_DEFAULT_ISSUES_LINES)
        ).strip()
        issues_lines = int(raw_issues or str(_DEFAULT_ISSUES_LINES))

    triage_dir = diag_dir / "triage"
    triage_dir.mkdir(parents=True, exist_ok=True)

    pods_json = load_cluster_pods_snapshot()
    status_path = triage_dir / "status-report.txt"
    write_status_report(status_path, operator_ns=operator_ns, pods_json=pods_json)

    namespaces = odh_rhoai_namespaces()
    events_path = triage_dir / "events.txt"
    write_events(events_path, namespaces)

    _, highlights_path = collect_operator_logs_and_highlights(
        triage_dir, operator_ns=operator_ns, since_time=since_time
    )
    collect_workload_pod_logs(
        triage_dir,
        since_time=since_time,
        pods_json=pods_json,
        max_bytes_per_ns=max_bytes,
    )

    extra_reports: tuple[Path, ...] = ()
    if needs_dependency_install_diagnostics(operator_ns):
        dep_status_path = triage_dir / "dependency-status-report.txt"
        write_dependency_install_status(
            dep_status_path, operator_ns=operator_ns, pods_json=pods_json
        )
        dep_events_path = triage_dir / "dependency-events.txt"
        write_dependency_events(dep_events_path, install_dependency_namespaces())
        dep_highlights_path = collect_dependency_install_logs(
            triage_dir,
            since_time=since_time,
            pods_json=pods_json,
            max_bytes_per_ns=max_bytes,
        )
        extra_reports = (dep_status_path, dep_events_path, dep_highlights_path)

    issues_path = triage_dir / "issues-summary.txt"
    build_issues_summary(
        issues_path,
        status_report=status_path,
        events=events_path,
        operator_highlights=highlights_path,
        max_lines=issues_lines,
        extra_reports=extra_reports,
    )
    return issues_path
