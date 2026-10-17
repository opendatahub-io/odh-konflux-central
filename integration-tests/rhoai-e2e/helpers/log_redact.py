"""Redact secrets from command / env strings before logging to Tekton stdout."""

from __future__ import annotations

import re

# KEY=VALUE and KEY="VALUE" / KEY='VALUE' (export shell + Cypress --env CSV).
_SENSITIVE_KEY = r"[A-Za-z_][A-Za-z0-9_]*(?:PASSWORD|TOKEN|SECRET|API_KEY)"
_SENSITIVE_VALUE = (
    r'"(?:\\.|[^"\\])*"|'
    r"'(?:\\.|[^'\\])*'|"
    r"[^\s,\"']+"
)
_SENSITIVE_ASSIGN_RE = re.compile(
    rf"(?i)\b({_SENSITIVE_KEY})\b\s*=\s*({_SENSITIVE_VALUE})"
)

# Compact JWTs accidentally embedded outside KEY=VALUE forms.
_JWT_RE = re.compile(
    r"\beyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\b"
)


def _redact_assignment(match: re.Match[str]) -> str:
    key, value = match.group(1), match.group(2)
    if value[:1] in {'"', "'"}:
        q = value[0]
        return f"{key}={q}***{q}"
    return f"{key}=***"


def redact_command_for_log(command: str) -> str:
    """Return *command* safe for stdout; does not alter the execution string."""
    if not command:
        return command
    return _JWT_RE.sub("***", _SENSITIVE_ASSIGN_RE.sub(_redact_assignment, command))
