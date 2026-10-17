"""Resolve OLM UPDATE_CHANNEL for RHOAI Konflux triggers."""

from __future__ import annotations

import re


def _stable_channel_version(version: str) -> str:
    """Normalize version strings to major.minor for OLM stable-* channels."""
    text = (version or "").strip()
    match = re.match(r"^(\d+)[.-](\d+)", text)
    if match:
        return f"{match.group(1)}.{match.group(2)}"
    return text


def resolve_rhoai_update_channel(*, version: str = "", resolved_app: str = "") -> str | None:
    """Map CLI channel defaults for RHOAI Konflux triggers.

    When ``--rhoai-version`` is set:
      - EA builds (``3.5-ea.2``, ``3.5.0-ea.2``, …) → ``beta``
      - major < 3 (RHOAI 2.x) → ``stable``
      - major >= 3 → ``stable-<major>.<minor>`` (pinned minor line, e.g. ``stable-3.3``)
    When version is omitted (ITS default / resolved app only), use ``beta``.

    Use ``--rhoai-channel stable-3.x`` explicitly for the rolling 3.x GA channel.
    """
    _ = resolved_app
    ver = (version or "").strip()
    if not ver:
        return "beta"
    if re.search(r"-ea[.-]", ver) or re.search(r"-ea-\d", ver):
        return "beta"
    major_match = re.match(r"^(\d+)", ver)
    if not major_match:
        return "beta"
    if int(major_match.group(1)) < 3:
        return "stable"
    minor = _stable_channel_version(ver)
    return f"stable-{minor}" if minor else "beta"
