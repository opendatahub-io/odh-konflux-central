"""GitHub URL parsing for rhoai-e2e Konflux metadata."""

from __future__ import annotations

import re

_GITHUB_REPO_RE = re.compile(
    r"^https?://(?:www\.)?github\.com/([^/]+)/([^/#?]+?)(?:\.git)?(?:[/?#]|$)",
    re.IGNORECASE,
)


def normalize_https_git_url(url: str) -> str:
    """Return ``https://github.com/org/repo`` without trailing ``.git``."""
    text = (url or "").strip()
    if not text:
        return ""
    if text.endswith(".git"):
        text = text[:-4]
    return text.rstrip("/")


def parse_github_org_repo(git_url: str) -> tuple[str, str]:
    m = _GITHUB_REPO_RE.match((git_url or "").strip())
    if not m:
        return "", ""
    return m.group(1), m.group(2)
