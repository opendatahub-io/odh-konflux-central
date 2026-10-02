"""Map OpenShift minor versions to RHOAI FBC fragment Konflux component ids."""

from __future__ import annotations

import re

_OCP_MINOR_RE = re.compile(r"^(\d+)\.(\d+)$")
_RHOAI_FBC_OCP_COMPONENT_RE = re.compile(r"^rhoai-fbc-fragment-ocp-(\d)(\d{2})$", re.IGNORECASE)


def normalize_ocp_minor(text: str) -> str:
    """Return ``MAJOR.MINOR`` (e.g. ``4.21``) or raise ``ValueError``."""
    raw = (text or "").strip()
    match = _OCP_MINOR_RE.fullmatch(raw)
    if not match:
        raise ValueError(f"OCP version must be MAJOR.MINOR (e.g. 4.21), got {text!r}")
    return f"{match.group(1)}.{match.group(2)}"


def rhoai_fbc_name_from_ocp_minor(ocp_minor: str) -> str:
    """``4.21`` → ``rhoai-fbc-fragment-ocp-421``. Empty for OCP 5.x (no fragment yet)."""
    minor = normalize_ocp_minor(ocp_minor)
    major_s, minor_s = minor.split(".", 1)
    if major_s != "4":
        return ""
    return f"rhoai-fbc-fragment-ocp-{major_s}{int(minor_s):02d}"


def rhoai_fbc_name_from_rhoai_version(version: str) -> str:
    """``3.5`` → ``rhoai-fbc-fragment-v3-5`` (Konflux version-stream component id)."""
    raw = (version or "").strip()
    if not raw:
        return ""
    parts = raw.split(".")
    if not parts or not all(part.isdigit() for part in parts):
        return ""
    return "rhoai-fbc-fragment-v" + "-".join(parts)


def ocp_minor_from_rhoai_fbc_name(component_name: str) -> str:
    """``rhoai-fbc-fragment-ocp-421`` → ``4.21``."""
    match = _RHOAI_FBC_OCP_COMPONENT_RE.match((component_name or "").strip())
    if not match:
        return ""
    return f"{match.group(1)}.{int(match.group(2))}"
