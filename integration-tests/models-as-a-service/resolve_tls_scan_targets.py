#!/usr/bin/env python3
"""Resolve MaaS TLS scan --targets and write scan-targets.meta.json for validation."""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

from maas_tls_scan_lib import (
    build_targets_meta,
    discover_service_targets,
    filter_targets_by_allowlist,
    oc_service_exists,
    parse_service_targets,
    targets_cli_argument,
    validation_config_from_env,
)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--write-meta",
        required=True,
        type=Path,
        help="Write scan-targets.meta.json (under ARTIFACT_DIR)",
    )
    parser.add_argument(
        "--skip-oc-discovery",
        action="store_true",
        help="Do not drop targets when oc get svc fails (for offline tests)",
    )
    args = parser.parse_args()

    allow_ports, _, _ = validation_config_from_env()
    targets = parse_service_targets(os.environ.get("TLS_SCAN_SERVICE_TARGETS"))
    targets = filter_targets_by_allowlist(targets, allow_ports)

    warnings: list[str] = []
    if not args.skip_oc_discovery:
        targets, warnings = discover_service_targets(targets, oc_service_exists)

    if not targets:
        print("No TLS scan service targets remain after filtering/discovery", file=sys.stderr)
        for w in warnings:
            print(w, file=sys.stderr)
        return 1

    meta = build_targets_meta(targets)
    args.write_meta.parent.mkdir(parents=True, exist_ok=True)
    args.write_meta.write_text(json.dumps(meta, indent=2) + "\n", encoding="utf-8")

    for w in warnings:
        print(w, file=sys.stderr)

    print(targets_cli_argument(targets))
    return 0


if __name__ == "__main__":
    sys.exit(main())
