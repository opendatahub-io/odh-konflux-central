#!/usr/bin/env python3
"""Post-process tls-scanner artifacts for MaaS group-test."""
from __future__ import annotations

import os
import sys
from pathlib import Path

from maas_tls_scan_lib import (
    load_host_map_from_meta,
    validate_scan_artifacts,
    validation_config_from_env,
)


def main() -> int:
    artifact_dir = Path(os.environ.get("ARTIFACT_DIR", ".")).resolve()
    allow_ports, required, require_pqc = validation_config_from_env()
    host_map = load_host_map_from_meta(artifact_dir)

    ok, errors, ok_by_component = validate_scan_artifacts(
        artifact_dir,
        allow_ports=allow_ports,
        required_components=required,
        require_pqc=require_pqc,
        host_map=host_map,
    )

    if not ok:
        print("TLS/PQC post-validation failed:", file=sys.stderr)
        for msg in errors:
            print(f"  - {msg}", file=sys.stderr)
        return 1

    print("TLS/PQC post-validation passed:")
    for comp in required:
        for ip, port in ok_by_component.get(comp, []):
            print(f"  - {comp}: {ip}:{port}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
