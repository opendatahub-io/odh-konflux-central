"""Symlink kubectl -> oc on PATH for scripts that invoke kubectl."""

from __future__ import annotations

import shutil
from pathlib import Path

_SHIM_ROOT = Path("/tmp/olminstall-kubectl-shim")


def kubectl_shim_dir() -> str:
    """Return a directory containing a kubectl symlink to oc (create if needed)."""
    _SHIM_ROOT.mkdir(exist_ok=True)
    kubectl = _SHIM_ROOT / "kubectl"
    if kubectl.is_symlink() or not kubectl.exists():
        oc_path = shutil.which("oc") or "oc"
        kubectl.unlink(missing_ok=True)
        try:
            kubectl.symlink_to(oc_path)
        except FileExistsError:
            pass  # created concurrently by another process
    return str(_SHIM_ROOT)
