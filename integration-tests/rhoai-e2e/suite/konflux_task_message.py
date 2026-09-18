"""Format TASK_MESSAGE for Konflux Results multi-line display."""

from __future__ import annotations

import re

_STATUS_HINT_RE = re.compile(
    r"^(?P<head>.+?: Succeeded)\s+-\s+(?P<tail>.+)$",
)


def _ensure_sentence_end(line: str) -> str:
    text = " ".join((line or "").split())
    if not text:
        return ""
    if text.endswith("…") or text.endswith("..."):
        return text if text.endswith((".", ";")) else f"{text}."
    if text[-1] in ".;":
        return text
    return f"{text}."


def _split_physical_line(line: str) -> list[str]:
    stripped = line.strip()
    if not stripped:
        return []
    match = _STATUS_HINT_RE.match(stripped)
    if match:
        return [
            _ensure_sentence_end(match.group("head")),
            _ensure_sentence_end(match.group("tail")),
        ]
    return [_ensure_sentence_end(stripped)]


def format_konflux_task_message(text: str) -> str:
    """Split TASK_MESSAGE into Konflux-friendly lines (each ending with ``.`` or ``;``)."""
    raw = (text or "").strip()
    if not raw:
        return ""

    lines_out: list[str] = []
    for physical_line in raw.splitlines():
        for clause in physical_line.split(";"):
            for segment in _split_physical_line(clause):
                if segment:
                    lines_out.append(segment)
    return "\n".join(lines_out)
