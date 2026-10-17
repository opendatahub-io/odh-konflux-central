"""Human-readable PipelineRun configuration for rhoai_e2e watch/trigger summaries."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any, Callable

from suite.its_trigger_params import CLUSTER_SOURCE_EPHC, external_kubeconfig_secret_name, is_external_cluster_source

# Tekton param name → summary label (order preserved; Konflux → cluster → product → tests).
_PARAM_LABELS: tuple[tuple[str, str], ...] = (
    ("SCRIPTS_REPO_URL", "Scripts repo URL"),
    ("SCRIPTS_REPO_REVISION", "Scripts branch/revision"),
    ("CLUSTER_SOURCE", "Cluster source"),
    ("OCP_VERSION", "OCP version"),
    ("OCP_VERSION_PREFIX", "OCP version prefix (install)"),
    ("OCP_RELEASE_CHANNEL", "OCP release channel (OpenShift CI)"),
    ("CLEANUP", "Cleanup before install"),
    ("HYPERSHIFT_INSTANCE_TYPE", "HyperShift instance type"),
    ("PRODUCT", "Product"),
    ("RHOAI_VERSION", "RHOAI version"),
    ("UPDATE_CHANNEL", "Update channel"),
    ("OPERATOR_NAME", "Operator"),
    ("OPERATOR_NAMESPACE", "Operator namespace"),
    ("RHOAI_FBC_NAME", "RHOAI FBC component"),
    ("RHOAI_FBC_IMAGE", "RHOAI FBC catalog image"),
    ("COMPONENTS", "Smoke components"),
    ("COMPONENT_TEST_TIMEOUT", "Per-component smoke timeout"),
    ("TEST_TAGS", "Test tags"),
    ("OLMINSTALL_TESTS_VERSION_OVERRIDE", "Tests version override"),
    ("FAIL_FAST_DISABLED_COMPONENT", "Fail fast disabled components"),
    ("TEST_GATES", "Test gates"),
    ("TESTS", "Tests"),
    ("SLACK_CHANNEL_ID", "Slack channel"),
)

_SNAPSHOT_LABEL = "appstudio.openshift.io/snapshot"


def pipelinerun_param_map(prj: dict[str, Any]) -> dict[str, str]:
    """Return non-empty PipelineRun ``spec.params`` values keyed by name."""
    out: dict[str, str] = {}
    for p in prj.get("spec", {}).get("params", []) or []:
        if not isinstance(p, dict):
            continue
        name = p.get("name")
        if not isinstance(name, str) or not name:
            continue
        val = p.get("value")
        if isinstance(val, str) and val.strip():
            out[name] = val.strip()
    return out


def _short_container_image(image: str, *, max_len: int = 72) -> str:
    text = (image or "").strip()
    if not text:
        return ""
    if len(text) <= max_len:
        return text
    if "@sha256:" in text:
        repo, digest = text.split("@sha256:", 1)
        digest = digest[:12]
        short = f"{repo}@sha256:{digest}…"
        return short if len(short) <= max_len else short[: max_len - 1] + "…"
    return text[: max_len - 1] + "…"


def _snapshot_lines(prj: dict[str, Any], params: dict[str, str]) -> list[str]:
    lines: list[str] = []
    labels = (prj.get("metadata") or {}).get("labels") or {}
    snap_name = labels.get(_SNAPSHOT_LABEL) if isinstance(labels, dict) else None
    if isinstance(snap_name, str) and snap_name.strip():
        lines.append(f"  Snapshot: {snap_name.strip()}")

    raw = params.get("SNAPSHOT", "")
    if not raw or raw == snap_name:
        return lines
    if not raw.startswith("{"):
        lines.append(f"  Snapshot param: {raw}")
        return lines
    try:
        doc = json.loads(raw)
    except json.JSONDecodeError:
        lines.append("  Snapshot param: (invalid JSON)")
        return lines
    app = (doc.get("application") or "").strip()
    if app:
        lines.append(f"  Snapshot application: {app}")
    components = doc.get("components")
    if isinstance(components, list):
        for item in components[:3]:
            if not isinstance(item, dict):
                continue
            name = (item.get("name") or "").strip()
            image = _short_container_image(str(item.get("containerImage") or ""))
            if name and image:
                lines.append(f"  Snapshot image: {name} → {image}")
            elif name:
                lines.append(f"  Snapshot component: {name}")
        if len(components) > 3:
            lines.append(f"  Snapshot components: (+{len(components) - 3} more)")
    return lines


def _components_value(params: dict[str, str]) -> str:
    raw = (params.get("COMPONENTS") or "").strip()
    if raw:
        parts = [p.strip() for p in raw.split(",") if p.strip()]
        if len(parts) > 6:
            return f"{', '.join(parts[:6])}, … ({len(parts)} total)"
        return raw
    return "(not set on PipelineRun — ITS / catalog default: all components)"


def _test_gates_value(params: dict[str, str]) -> str:
    gates = (params.get("TEST_GATES") or "").strip()
    if gates:
        return gates
    return (params.get("TESTS") or "").strip()


def _format_timestamp_utc(ts: str) -> str:
    text = (ts or "").strip()
    if not text:
        return ""
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(text)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        else:
            dt = dt.astimezone(timezone.utc)
        return dt.strftime("%Y-%m-%d %H:%M:%S UTC")
    except ValueError:
        return (ts or "").strip()


def _pipelinerun_timing_lines(prj: dict[str, Any]) -> list[str]:
    meta = prj.get("metadata") if isinstance(prj.get("metadata"), dict) else {}
    status = prj.get("status") if isinstance(prj.get("status"), dict) else {}
    created_raw = (meta.get("creationTimestamp") or "").strip()
    completed_raw = (status.get("completionTime") or "").strip()
    lines: list[str] = []
    if created_raw:
        lines.append(f"  Started: {_format_timestamp_utc(created_raw)}")
    if completed_raw:
        lines.append(f"  Finished: {_format_timestamp_utc(completed_raw)}")
    else:
        lines.append("  Finished: (not completed)")
    if created_raw and completed_raw:
        try:
            start = datetime.fromisoformat(created_raw.replace("Z", "+00:00"))
            end = datetime.fromisoformat(completed_raw.replace("Z", "+00:00"))
            secs = int((end - start).total_seconds())
            if secs >= 0:
                mins, rem = divmod(secs, 60)
                lines.append(f"  Duration: {mins}m {rem}s")
        except ValueError:
            pass
    return lines


def pipelinerun_outcome_line(prj: dict[str, Any]) -> str | None:
    status = prj.get("status") if isinstance(prj.get("status"), dict) else {}
    conds = status.get("conditions")
    if not isinstance(conds, list):
        return None
    succeeded = next((c for c in conds if isinstance(c, dict) and c.get("type") == "Succeeded"), None)
    if not isinstance(succeeded, dict):
        return None
    stat = (succeeded.get("status") or "").strip()
    reason = (succeeded.get("reason") or "").strip()
    message = (succeeded.get("message") or "").strip()
    if stat == "True":
        return "  Outcome: succeeded"
    if stat == "False":
        detail = reason or "Failed"
        if message and message != detail:
            return f"  Outcome: {detail} — {message}"
        return f"  Outcome: {detail}"
    if reason or message:
        detail = reason or "Unknown"
        if message and message != detail:
            return f"  Outcome: {detail} — {message}"
        return f"  Outcome: {detail}"
    return None


def _target_cluster_line(
    params: dict[str, str],
    *,
    cluster_label_hint: str = "",
    resolve_external_cluster: Callable[[str], str] | None,
) -> str | None:
    source = (params.get("CLUSTER_SOURCE") or "").strip()
    if is_external_cluster_source(source):
        secret_name = external_kubeconfig_secret_name(source) or source
        label = (cluster_label_hint or "").strip()
        if not label and resolve_external_cluster is not None:
            try:
                label = (resolve_external_cluster(secret_name) or "").strip()
            except OSError:
                label = ""
        if label and label != secret_name:
            return f"  Target cluster: {label}"
        return (
            f"  Target cluster: (unresolved — secret {secret_name} missing, deleted, or has no cluster label; "
            "re-upload with --external-kubeconfig to label the Secret)"
        )
    if source == CLUSTER_SOURCE_EPHC:
        ocp = (params.get("OCP_VERSION_PREFIX") or "").strip()
        if ocp:
            return f"  Target cluster: ephemeral (OCP {ocp})"
        return "  Target cluster: EPHC provisioned"
    ocp = (params.get("OCP_VERSION_PREFIX") or "").strip()
    if ocp:
        return f"  Target cluster: ephemeral (OCP {ocp})"
    product = (params.get("PRODUCT") or "").strip().lower()
    if product == "rhoai":
        return "  Target cluster: EPHC provisioned (default)"
    return None


def pipelinerun_timing_lines(prj: dict[str, Any]) -> list[str]:
    """Started/finished/duration lines for the run summary header."""
    if not prj:
        return []
    return _pipelinerun_timing_lines(prj)


def format_pipelinerun_config_lines(
    prj: dict[str, Any],
    *,
    cluster_label_hint: str = "",
    resolve_external_cluster: Callable[[str], str] | None = None,
) -> list[str]:
    """Lines for ``print_run_summary`` (each line already indented with two spaces)."""
    if not prj:
        return ["  (PipelineRun details unavailable)"]

    params = pipelinerun_param_map(prj)
    lines: list[str] = []
    lines.extend(_snapshot_lines(prj, params))

    repo = (params.get("SCRIPTS_REPO_URL") or "").strip()
    rev = (params.get("SCRIPTS_REPO_REVISION") or "").strip()
    if repo and rev:
        lines.append(f"  Scripts repo: {repo} @ {rev}")
    elif repo:
        lines.append(f"  Scripts repo URL: {repo}")
    elif rev:
        lines.append(f"  Scripts branch/revision: {rev}")

    cluster = _target_cluster_line(
        params,
        cluster_label_hint=cluster_label_hint,
        resolve_external_cluster=resolve_external_cluster,
    )
    if cluster:
        lines.append(cluster)

    source = (params.get("CLUSTER_SOURCE") or "").strip()
    if source:
        lines.append(f"  Cluster source: {source}")

    ocp_ver = (params.get("OCP_VERSION") or "").strip()
    if ocp_ver:
        lines.append(f"  OCP version: {ocp_ver}")

    ocp_prefix = (params.get("OCP_VERSION_PREFIX") or "").strip()
    if ocp_prefix:
        lines.append(f"  OCP version prefix: {ocp_prefix}")

    cleanup = (params.get("CLEANUP") or "").strip()
    if cleanup and cleanup.lower() == "true":
        lines.append("  Cleanup: true")

    product = (params.get("PRODUCT") or "").strip()
    if product:
        lines.append(f"  Product: {product}")

    rhoai_ver = (params.get("RHOAI_VERSION") or "").strip()
    if rhoai_ver:
        lines.append(f"  RHOAI version: {rhoai_ver}")

    channel = (params.get("UPDATE_CHANNEL") or "").strip()
    if channel:
        lines.append(f"  Update channel: {channel}")

    fbc_name = (params.get("RHOAI_FBC_NAME") or params.get("FBCF_COMPONENT_NAME") or "").strip()
    if fbc_name:
        lines.append(f"  RHOAI FBC component: {fbc_name}")

    fbc_image = (params.get("RHOAI_FBC_IMAGE") or params.get("FBCF_IMAGE_DISPLAY") or "").strip()
    if fbc_image:
        lines.append(f"  RHOAI FBC catalog image: {fbc_image}")

    lines.append(f"  Smoke components: {_components_value(params)}")

    timeout = (params.get("COMPONENT_TEST_TIMEOUT") or "").strip()
    if timeout:
        lines.append(f"  Per-component timeout: {timeout}")

    tag_ids = (params.get("TEST_TAGS") or "").strip()
    if tag_ids:
        lines.append(f"  Test tags: {tag_ids}")

    tests_ver = (params.get("OLMINSTALL_TESTS_VERSION_OVERRIDE") or "").strip()
    if tests_ver:
        lines.append(f"  Tests version override: {tests_ver}")

    gates = _test_gates_value(params)
    if gates:
        lines.append(f"  Test gates: {gates}")

    shown = {
        "SNAPSHOT",
        "SCRIPTS_REPO_URL",
        "SCRIPTS_REPO_REVISION",
        "CLUSTER_SOURCE",
        "OCP_VERSION",
        "OCP_VERSION_PREFIX",
        "CLEANUP",
        "PRODUCT",
        "RHOAI_VERSION",
        "UPDATE_CHANNEL",
        "RHOAI_FBC_NAME",
        "RHOAI_FBC_IMAGE",
        "FBCF_COMPONENT_NAME",
        "FBCF_IMAGE_DISPLAY",
        "UPDATE_CHANNEL_DISPLAY",
        "COMPONENTS",
        "COMPONENT_TEST_TIMEOUT",
        "TEST_TAGS",
        "OLMINSTALL_TESTS_VERSION_OVERRIDE",
        "TEST_GATES",
        "TESTS",
    }
    for name, label in _PARAM_LABELS:
        if name in shown:
            continue
        val = (params.get(name) or "").strip()
        if val:
            lines.append(f"  {label}: {val}")

    if not lines:
        return ["  (no configuration params on PipelineRun)"]
    return lines
