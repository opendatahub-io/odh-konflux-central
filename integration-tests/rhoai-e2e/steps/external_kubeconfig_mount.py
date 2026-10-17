"""Copy external-cluster kubeconfig from a Tekton secret volume mount."""

from __future__ import annotations

import os
from pathlib import Path

_DEFAULT_MOUNT = Path("/var/rhoai-e2e/external-kubeconfig/kubeconfig")


def external_kubeconfig_mount_path() -> Path:
    raw = os.environ.get("OLMINSTALL_EXTERNAL_KUBECONFIG_MOUNT", "").strip()
    return Path(raw) if raw else _DEFAULT_MOUNT


def copy_external_kubeconfig_mount(dest: Path) -> bool:
    """Stage kubeconfig from secret volume when mounted; return True on success."""
    src = external_kubeconfig_mount_path()
    if not src.is_file():
        return False
    data = src.read_bytes()
    dest.parent.mkdir(parents=True, exist_ok=True)
    with open(dest, "wb", opener=lambda path, flags: os.open(path, flags, 0o600)) as handle:
        os.fchmod(handle.fileno(), 0o600)
        handle.write(data)
    print(f"External kubeconfig staged from secret mount at {dest}")
    return True
