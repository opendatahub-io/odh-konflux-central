#!/usr/bin/env python3
"""Probe live cluster OCP minor and write Tekton result ``ocpMinor``."""

from __future__ import annotations

import os
import sys
from pathlib import Path

from k8s.cluster_ocp_version import cluster_ocp_minor_from_kubeconfig


def write_cluster_ocp_minor_result(
    kubeconfig: str | Path,
    *,
    out_path: str | Path = "",
    openshift_version_path: str | Path = "",
) -> str:
    """Return detected ``MAJOR.MINOR``; write Tekton result files when paths are set."""
    path = Path(kubeconfig).expanduser()
    minor = cluster_ocp_minor_from_kubeconfig(path)
    if minor:
        print(f"Cluster OCP minor: {minor}")
    else:
        print("WARN: could not detect cluster OCP minor from kubeconfig", file=sys.stderr)
    for dest_raw in (out_path, openshift_version_path):
        dest = str(dest_raw or "").strip()
        if dest and minor:
            Path(dest).write_text(minor, encoding="ascii")
    return minor


def main() -> int:
    kubeconfig = os.environ.get("KUBECONFIG", "").strip()
    if not kubeconfig:
        print("KUBECONFIG is required", file=sys.stderr)
        return 1
    write_cluster_ocp_minor_result(
        kubeconfig,
        out_path=os.environ.get("OCP_MINOR_PATH", "").strip(),
        openshift_version_path=os.environ.get("OPENSHIFT_VERSION_PATH", "").strip(),
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
