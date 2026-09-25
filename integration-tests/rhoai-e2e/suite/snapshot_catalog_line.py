"""Detect RHOAI catalog line (2.25, 3.5-ea.2, …) from Konflux Snapshot PAC metadata."""

from __future__ import annotations

import json
import re
from typing import Any

from suite.component_version_gate import normalize_version_for_enablement, rhoai_version_at_least
from suite.its_trigger_params import (
    DEFAULT_SUFFIX,
    rhoai_catalog_line_from_konflux_app,
)
from suite.pipeline_run_context import fbc_image_from_snapshot

_PRNAME_VERSION_RE = re.compile(
    r"rhoai-fbc-fragment-rhoai-(\d+)(?:-ea(\d+))?-ocp-",
    re.IGNORECASE,
)
_RHOAI_LINE_RE = re.compile(
    r"rhoai-(\d+(?:\.\d+)*(?:-(?:ea|rc)\.\d+)?)",
    re.IGNORECASE,
)
_CEL_CATALOG_PATH_RE = re.compile(
    r"catalog/rhoai-([^/\"']+)",
    re.IGNORECASE,
)

_PAC_PRNAME = "pac.test.appstudio.openshift.io/original-prname"
_PAC_TITLE = "pac.test.appstudio.openshift.io/sha-title"
_PAC_CEL = "pac.test.appstudio.openshift.io/on-cel-expression"
_RESULT_IMAGE = "test.appstudio.openshift.io/result-image-url"


def _compact_digits_to_version(digits: str) -> str:
    """``225`` → ``2.25``, ``33`` → ``3.3``, ``35`` → ``3.5``."""
    d = (digits or "").strip()
    if not d.isdigit():
        return d
    if len(d) == 3:
        return f"{d[0]}.{d[1:]}"
    if len(d) == 2:
        return f"{d[0]}.{d[1]}"
    if len(d) == 1:
        return d
    if len(d) > 3 and d[1:].isdigit():
        return f"{d[0]}.{d[1:]}"
    return d


def catalog_line_from_prname(prname: str) -> str:
    match = _PRNAME_VERSION_RE.search((prname or "").strip())
    if not match:
        return ""
    base = _compact_digits_to_version(match.group(1))
    ea = match.group(2)
    if ea is not None:
        return f"{base}-ea.{ea}"
    return base


def _catalog_line_from_text(text: str) -> str:
    match = _RHOAI_LINE_RE.search((text or "").strip())
    if not match:
        return ""
    return match.group(1).strip()


def catalog_line_from_free_text(text: str) -> str:
    return _catalog_line_from_text(text)


def catalog_line_from_image_tag(url: str) -> str:
    text = (url or "").strip()
    if not text:
        return ""
    return _catalog_line_from_text(text.rsplit(":", 1)[-1])


def catalog_line_from_cel_expression(cel: str) -> str:
    match = _CEL_CATALOG_PATH_RE.search((cel or "").strip())
    if not match:
        return ""
    raw = match.group(1).strip()
    return catalog_line_from_free_text(f"rhoai-{raw}")


def catalog_line_from_snapshot_metadata(
    labels: dict[str, str] | None,
    annotations: dict[str, str] | None,
) -> str:
    """Best-effort catalog line from Integration Service Snapshot labels/annotations."""
    lab = labels if isinstance(labels, dict) else {}
    ann = annotations if isinstance(annotations, dict) else {}
    sources = (
        lab.get(_PAC_PRNAME, ""),
        ann.get(_PAC_TITLE, ""),
        ann.get(_RESULT_IMAGE, ""),
        ann.get(_PAC_CEL, ""),
    )
    extractors = (
        catalog_line_from_prname,
        catalog_line_from_free_text,
        catalog_line_from_image_tag,
        catalog_line_from_cel_expression,
    )
    for extractor, value in zip(extractors, sources):
        if line := extractor(value):
            return line
    return ""


def catalog_line_from_snapshot_json(
    snapshot_raw: str,
    component_name: str = "",
) -> str:
    """Best-effort catalog line from inline SNAPSHOT JSON (metadata and/or component image)."""
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
        line = catalog_line_from_snapshot_metadata(
            meta.get("labels") if isinstance(meta.get("labels"), dict) else None,
            meta.get("annotations") if isinstance(meta.get("annotations"), dict) else None,
        )
        if line:
            return line

    comp = (component_name or "").strip()
    if comp:
        line = catalog_line_from_image_tag(fbc_image_from_snapshot(raw, comp))
        if line:
            return line

    components = snap.get("components")
    if isinstance(components, list):
        for comp_obj in components:
            if not isinstance(comp_obj, dict):
                continue
            img = comp_obj.get("containerImage")
            if isinstance(img, str) and (line := catalog_line_from_image_tag(img)):
                return line
    return ""


def rhoai_catalog_version_from_fbc_source(
    *,
    fbc_image: str = "",
    fbc_snapshot_meta: dict[str, Any] | None = None,
    snapshot_json: str = "",
    fbc_component_name: str = "",
    resolved_app: str = "",
) -> str:
    """Resolve the RHOAI catalog version from Konflux FBC snapshot metadata or image pullspec."""
    meta = fbc_snapshot_meta if isinstance(fbc_snapshot_meta, dict) else {}
    line = catalog_line_from_snapshot_metadata(
        meta.get("labels") if isinstance(meta.get("labels"), dict) else None,
        meta.get("annotations") if isinstance(meta.get("annotations"), dict) else None,
    )
    if line:
        return line
    line = catalog_line_from_snapshot_json(snapshot_json, fbc_component_name)
    if line:
        return line
    line = catalog_line_from_image_tag(fbc_image)
    if line:
        return line
    return rhoai_catalog_line_from_konflux_app(resolved_app)


def resolve_catalog_version_for_naming(
    *,
    fbc_image: str = "",
    fbc_snapshot_meta: dict[str, Any] | None = None,
    snapshot_json: str = "",
    fbc_component_name: str = "",
    resolved_app: str = "",
    rhoai_version_param: str = "",
) -> str:
    """Resolve catalog line for PipelineRun ``generateName`` at trigger time."""
    from suite.pipelinerun_naming import version_placeholder

    catalog = rhoai_catalog_version_from_fbc_source(
        fbc_image=fbc_image,
        fbc_snapshot_meta=fbc_snapshot_meta,
        snapshot_json=snapshot_json,
        fbc_component_name=fbc_component_name,
        resolved_app=resolved_app,
    )
    if catalog:
        return catalog
    param = (rhoai_version_param or "").strip()
    if param.endswith(DEFAULT_SUFFIX):
        param = param[: -len(DEFAULT_SUFFIX)].strip()
    if not param or version_placeholder(param):
        return ""
    line = catalog_line_from_free_text(param) or rhoai_catalog_line_from_konflux_app(param) or param
    return line if line and not version_placeholder(line) else ""


def catalog_version_for_install(
    *,
    rhoai_version_param: str = "",
    fbcf_image: str = "",
) -> str:
    """Catalog line for install task results when CSV version is unavailable."""
    from suite.pipelinerun_naming import version_placeholder

    ver = (rhoai_version_param or "").strip()
    if ver and not version_placeholder(ver):
        return ver
    return catalog_line_from_image_tag(fbcf_image)


def catalog_line_meets_min_version(catalog_line: str, min_version: str) -> bool:
    line = (catalog_line or "").strip()
    minimum = (min_version or "3.5").strip() or "3.5"
    if not line:
        return True
    compare_line, is_numeric = normalize_version_for_enablement(line)
    if not is_numeric:
        return True
    return rhoai_version_at_least(compare_line, minimum)
