"""Human-readable rhoai-e2e PipelineRun trigger context (logs + Konflux Results)."""

from __future__ import annotations

import json
import os
import re
from typing import Any

from suite.constants import (
    ANNOTATION_CLUSTER,
    ANNOTATION_FBCF_IMAGE,
    ANNOTATION_PRODUCT,
    ANNOTATION_TESTS,
    ANNOTATION_TRIGGER_COMMAND,
    ANNOTATION_TRIGGER_TYPE,
    EVENT_TYPE_INCOMING,
    EVENT_TYPE_PUSH,
    LABEL_TRIGGER_EVENT_TYPE,
    TRIGGER_TYPE_MANUAL,
    TRIGGER_TYPE_RH_NIGHTLY_AUTO,
)
from suite.its_trigger_params import (
    CLUSTER_SOURCE_EPHC,
    external_kubeconfig_secret_name,
    is_ephemeral_hosted_cluster_source,
)

_SHA256_RE = re.compile(r"@sha256:([0-9a-f]{12,64})", re.IGNORECASE)

_TRIGGER_TYPE_LABELS: dict[str, str] = {
    TRIGGER_TYPE_MANUAL: "CLI direct (manual trigger)",
    TRIGGER_TYPE_RH_NIGHTLY_AUTO: "rh-nightly catalog sync (ITS Snapshot)",
}

_EVENT_TYPE_LABELS: dict[str, str] = {
    EVENT_TYPE_PUSH: "Push — Integration Service (Snapshot / ITS)",
    EVENT_TYPE_INCOMING: "Incoming — CLI direct PipelineRun",
}

# Separate Tekton results on parse-pipeline-tests (Konflux Results ignores newlines in one value).
TRIGGER_CONTEXT_RESULT_NAMES: tuple[str, ...] = (
    "TRIGGER",
    "KONFLUX_EVENT",
    "SNAPSHOT",
    "FBC",
    "CLUSTER",
    "RUN",
    "TRIGGER_CMD",
)

TRIGGER_CONTEXT_PATH_ENV: dict[str, str] = {
    name: f"{name}_PATH" for name in TRIGGER_CONTEXT_RESULT_NAMES
}


def short_digest(image: str) -> str:
    text = (image or "").strip()
    match = _SHA256_RE.search(text)
    if match:
        digest = match.group(1).lower()
        return digest[:12] + "…" if len(digest) > 12 else digest
    return ""


def fbc_image_from_snapshot(snapshot_raw: str, component_name: str) -> str:
    """Return ``containerImage`` for *component_name* inside Konflux SNAPSHOT JSON."""
    raw = (snapshot_raw or "").strip()
    name = (component_name or "").strip()
    if not raw or not name:
        return ""
    try:
        snap = json.loads(raw)
    except json.JSONDecodeError:
        return ""
    if not isinstance(snap, dict):
        return ""
    components = snap.get("components")
    if not isinstance(components, list):
        return ""
    for comp in components:
        if isinstance(comp, dict) and comp.get("name") == name:
            img = comp.get("containerImage")
            return img.strip() if isinstance(img, str) else ""
    return ""


def snapshot_name_from_json(snapshot_raw: str) -> str:
    raw = (snapshot_raw or "").strip()
    if not raw:
        return ""
    try:
        snap = json.loads(raw)
    except json.JSONDecodeError:
        return ""
    if not isinstance(snap, dict):
        return ""
    meta = snap.get("metadata")
    if isinstance(meta, dict):
        name = meta.get("name")
        if isinstance(name, str) and name.strip():
            return name.strip()
    return ""


def _cluster_display(cluster_source: str, cluster_annotation: str = "") -> str:
    label = (cluster_annotation or "").strip()
    source = (cluster_source or "").strip()
    if label and source and label != source:
        return f"{label} ({source})"
    if label:
        return label
    if is_ephemeral_hosted_cluster_source(source):
        return "EPHC (provision in-pipeline)"
    secret = external_kubeconfig_secret_name(source)
    if secret.startswith("rhoai-e2e-kubeconfig-"):
        return secret.removeprefix("rhoai-e2e-kubeconfig-")
    return secret or source


def describe_trigger_type(trigger_type: str, *, konflux_event: str = "") -> str:
    key = (trigger_type or "").strip().lower()
    if key:
        return _TRIGGER_TYPE_LABELS.get(key, key)
    event = (konflux_event or "").strip().lower()
    if event == EVENT_TYPE_PUSH:
        return "Integration Service (upstream FBC build or playpen Snapshot)"
    if event == EVENT_TYPE_INCOMING:
        return "CLI direct (no Integration Service)"
    return "Integration Service or manual Snapshot"


def describe_konflux_event(event_type: str) -> str:
    key = (event_type or "").strip().lower()
    return _EVENT_TYPE_LABELS.get(key, key or "n/a")


def _truncate_line(text: str, max_len: int) -> str:
    line = (text or "").strip()
    if len(line) <= max_len:
        return line
    return line[: max(0, max_len - 1)] + "…"


def build_pipeline_run_context_lines(
    *,
    pipelinerun_name: str = "",
    trigger_type: str = "",
    trigger_command: str = "",
    konflux_event: str = "",
    snapshot_name: str = "",
    fbc_component: str = "",
    fbc_image: str = "",
    cluster_source: str = "",
    cluster_annotation: str = "",
    product: str = "",
    test_gates: str = "",
) -> list[str]:
    """Multiline log block (full detail)."""
    img = (fbc_image or "").strip()
    digest = short_digest(img)
    lines = ["=== rhoai-e2e run context ==="]
    if (pipelinerun_name or "").strip():
        lines.append(f"PipelineRun: {pipelinerun_name.strip()}")
    lines.append(f"Trigger: {describe_trigger_type(trigger_type, konflux_event=konflux_event)}")
    if (konflux_event or "").strip():
        lines.append(f"Konflux event: {describe_konflux_event(konflux_event)}")
    if (snapshot_name or "").strip():
        lines.append(f"Snapshot: {snapshot_name.strip()}")
    if (fbc_component or "").strip():
        comp_line = f"FBC component: {fbc_component.strip()}"
        if digest:
            comp_line += f" @ sha256:{digest}"
        lines.append(comp_line)
    if img:
        lines.append(f"Catalog image: {img}")
    cluster = _cluster_display(cluster_source, cluster_annotation)
    if cluster:
        lines.append(f"Cluster: {cluster}")
    prod = (product or "").strip()
    tests = (test_gates or "").strip()
    if prod or tests:
        bits = []
        if prod:
            bits.append(f"product={prod}")
        if tests:
            bits.append(f"tests={tests}")
        lines.append("Run params: " + ", ".join(bits))
    cmd = (trigger_command or "").strip()
    if cmd:
        lines.append(f"Trigger command: {cmd}")
    elif (trigger_type or "").strip().lower() == TRIGGER_TYPE_RH_NIGHTLY_AUTO:
        lines.append(
            "Trigger command: (rh-nightly catalog sync via --enable-its — ITS started this PipelineRun)"
        )
    elif (konflux_event or "").strip().lower() == EVENT_TYPE_PUSH and not trigger_type:
        lines.append(
            "Trigger command: (none — upstream component build or manual Snapshot; Integration Service only)"
        )
    lines.append("=== end run context ===")
    return lines


def _fbc_result_value(*, fbc_component: str, fbc_image: str) -> str:
    img = (fbc_image or "").strip()
    digest = short_digest(img)
    component = (fbc_component or "").strip()
    if component:
        line = component
        if digest:
            line += f" @ sha256:{digest}"
        return line
    if img:
        return _truncate_line(img, 120)
    return "n/a"


def _trigger_cmd_result_value(
    *,
    trigger_type: str,
    trigger_command: str,
    konflux_event: str,
    max_len: int = 480,
) -> str:
    cmd = (trigger_command or "").strip()
    if cmd:
        return _truncate_line(cmd, max_len)
    if (trigger_type or "").strip().lower() == TRIGGER_TYPE_RH_NIGHTLY_AUTO:
        return (
            "(rh-nightly catalog sync via --enable-its — ITS started this PipelineRun)"
        )
    if (konflux_event or "").strip().lower() == EVENT_TYPE_PUSH and not (trigger_type or "").strip():
        return (
            "(none — upstream component build or manual Snapshot; Integration Service only)"
        )
    return "n/a"


def build_pipeline_run_context_results(
    *,
    pipelinerun_name: str = "",
    trigger_type: str = "",
    trigger_command: str = "",
    konflux_event: str = "",
    snapshot_name: str = "",
    fbc_component: str = "",
    fbc_image: str = "",
    cluster_source: str = "",
    cluster_annotation: str = "",
    product: str = "",
    test_gates: str = "",
) -> dict[str, str]:
    """One Tekton result per trigger-context field (Konflux Results table rows)."""
    _ = pipelinerun_name
    event = (konflux_event or "").strip()
    cluster = _cluster_display(cluster_source, cluster_annotation)
    prod = (product or "").strip()
    tests = (test_gates or "").strip()
    return {
        "TRIGGER": describe_trigger_type(trigger_type, konflux_event=konflux_event),
        "KONFLUX_EVENT": describe_konflux_event(event) if event else "n/a",
        "SNAPSHOT": (snapshot_name or "").strip() or "n/a",
        "FBC": _fbc_result_value(fbc_component=fbc_component, fbc_image=fbc_image),
        "CLUSTER": cluster or "n/a",
        "RUN": f"product={prod or 'n/a'}, tests={tests or 'n/a'}",
        "TRIGGER_CMD": _trigger_cmd_result_value(
            trigger_type=trigger_type,
            trigger_command=trigger_command,
            konflux_event=konflux_event,
        ),
    }


def build_pipeline_run_context_message(
    *,
    pipelinerun_name: str = "",
    trigger_type: str = "",
    trigger_command: str = "",
    konflux_event: str = "",
    snapshot_name: str = "",
    fbc_component: str = "",
    fbc_image: str = "",
    cluster_source: str = "",
    cluster_annotation: str = "",
    product: str = "",
    test_gates: str = "",
    max_bytes: int = 720,
) -> str:
    """Legacy single-string summary (logs / CLI); prefer ``build_pipeline_run_context_results``."""
    results = build_pipeline_run_context_results(
        pipelinerun_name=pipelinerun_name,
        trigger_type=trigger_type,
        trigger_command=trigger_command,
        konflux_event=konflux_event,
        snapshot_name=snapshot_name,
        fbc_component=fbc_component,
        fbc_image=fbc_image,
        cluster_source=cluster_source,
        cluster_annotation=cluster_annotation,
        product=product,
        test_gates=test_gates,
    )
    lines = [f"{key}: {results[key]}" for key in TRIGGER_CONTEXT_RESULT_NAMES if results.get(key)]
    msg = "\n".join(lines)
    raw = msg.encode("utf-8")
    if len(raw) <= max_bytes:
        return msg
    ellipsis = "…"
    budget = max(0, max_bytes - len(ellipsis.encode("utf-8")))
    return raw[:budget].decode("utf-8", errors="ignore") + ellipsis


def trigger_context_paths_from_environ(environ: dict[str, str] | None = None) -> dict[str, str]:
    """Map trigger-context result names to Tekton ``$(results.*.path)`` files from env."""
    env = os.environ if environ is None else environ
    paths: dict[str, str] = {}
    for name, env_key in TRIGGER_CONTEXT_PATH_ENV.items():
        raw = env.get(env_key, "").strip()
        if raw and "$(" not in raw:
            paths[name] = raw
    return paths


def context_from_pipelinerun_json(
    prj: dict[str, Any],
    *,
    snapshot_raw: str = "",
    fbc_component: str = "",
    cluster_source: str = "",
    product: str = "",
    test_gates: str = "",
) -> dict[str, str]:
    """Build context fields from a PipelineRun object + pipeline params."""
    meta = prj.get("metadata") if isinstance(prj.get("metadata"), dict) else {}
    ann = meta.get("annotations") if isinstance(meta.get("annotations"), dict) else {}
    labels = meta.get("labels") if isinstance(meta.get("labels"), dict) else {}
    name = str(meta.get("name") or "").strip()

    fbc_image = str(ann.get(ANNOTATION_FBCF_IMAGE) or "").strip()
    if not fbc_image:
        fbc_image = fbc_image_from_snapshot(snapshot_raw, fbc_component)

    snap = (
        str(labels.get("appstudio.openshift.io/snapshot") or "").strip()
        or snapshot_name_from_json(snapshot_raw)
    )

    return {
        "pipelinerun_name": name,
        "trigger_type": str(ann.get(ANNOTATION_TRIGGER_TYPE) or "").strip(),
        "trigger_command": str(ann.get(ANNOTATION_TRIGGER_COMMAND) or "").strip(),
        "konflux_event": str(labels.get(LABEL_TRIGGER_EVENT_TYPE) or "").strip(),
        "snapshot_name": snap,
        "fbc_component": (fbc_component or "").strip(),
        "fbc_image": fbc_image,
        "cluster_source": (cluster_source or "").strip(),
        "cluster_annotation": str(ann.get(ANNOTATION_CLUSTER) or "").strip(),
        "product": (product or str(ann.get(ANNOTATION_PRODUCT) or "")).strip(),
        "test_gates": (test_gates or str(ann.get(ANNOTATION_TESTS) or "")).strip(),
    }
