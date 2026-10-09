"""Resolve or clone the rhoai-e2e repo for dependency install scripts."""

from __future__ import annotations

import os
import shutil
from pathlib import Path

from steps.tekton_util import git_clone
from suite.constants import DEFAULT_OLMINSTALL_REPO_REVISION, DEFAULT_OLMINSTALL_REPO_URL

_OLMINSTALL_DEST = Path("/tmp/rhoai-e2e-maas-prereqs")
_POST_INSTALL_SCRIPT = "resources/post-install-rhcl-operator.sh"
_CLEANUP_SCRIPT = "cleanup.sh"


def resolve_olminstall_dir(*, marker_script: str = _POST_INSTALL_SCRIPT, require_marker: bool = True) -> Path:
    """Return upstream olminstall checkout (``OLMINSTALL_DIR`` env or clone under /tmp)."""
    existing = os.environ.get("OLMINSTALL_DIR", "").strip()
    if existing:
        path = Path(existing)
        script = path / marker_script
        if require_marker and not script.is_file():
            raise FileNotFoundError(f"OLMINSTALL_DIR={existing!r} has no {marker_script}")
        return path

    dest = _OLMINSTALL_DEST
    script = dest / marker_script
    url = os.environ.get("OLMINSTALL_REPO_URL", "").strip() or DEFAULT_OLMINSTALL_REPO_URL
    rev = os.environ.get("OLMINSTALL_REPO_REVISION", "").strip() or DEFAULT_OLMINSTALL_REPO_REVISION
    marker = dest / ".checkout_ref"
    stale = marker.is_file() and marker.read_text().strip() != f"{url}@{rev}"
    if not script.is_file() or stale:
        if dest.exists():
            shutil.rmtree(dest)
        print(f"Cloning rhoai-e2e ({url} @ {rev})...", flush=True)
        git_clone(url, rev, dest, tls_workaround=True)
        marker.write_text(f"{url}@{rev}")
        if not script.is_file():
            raise FileNotFoundError(f"Missing {script} after rhoai-e2e clone")
    return dest
