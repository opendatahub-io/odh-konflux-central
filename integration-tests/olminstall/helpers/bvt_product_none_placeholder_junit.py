#!/usr/bin/env python3
"""Emit skipped JUnit XML for PRODUCT=none BVT (no workload cluster / no ODH APIs on worker)."""

from __future__ import annotations

import sys
import xml.etree.ElementTree as ET
from pathlib import Path

_SKIP = (
    "PRODUCT=none: full opendatahub-tests BVT needs a cluster with Open Data Hub APIs "
    "(e.g. DataScienceCluster). Use PRODUCT rhoai or odh for EaaS-backed BVT."
)

_ARTIFACTS_ROOT = Path("/artifacts").resolve()


def _safe_artifact_out(raw: Path) -> Path:
    out = raw.resolve() if raw.is_absolute() else (_ARTIFACTS_ROOT / raw).resolve()
    if out != _ARTIFACTS_ROOT and _ARTIFACTS_ROOT not in out.parents:
        raise ValueError(f"output directory must be under {_ARTIFACTS_ROOT}, got {raw}")
    return out


def _testsuite_xml(name: str, msg: str) -> str:
    testsuite = ET.Element(
        "testsuite",
        {
            "name": name,
            "tests": "1",
            "skipped": "1",
            "failures": "0",
            "errors": "0",
            "time": "0.0",
        },
    )
    testcase = ET.SubElement(
        testsuite,
        "testcase",
        {"name": "bvt_skipped_no_odh_cluster", "classname": "olminstall"},
    )
    ET.SubElement(testcase, "skipped", {"message": msg})
    if hasattr(ET, "indent"):
        ET.indent(testsuite, space="  ")
    body = ET.tostring(testsuite, encoding="unicode", xml_declaration=False)
    return '<?xml version="1.0" encoding="utf-8"?>\n' + body


def main() -> int:
    try:
        out = _safe_artifact_out(Path(sys.argv[1] if len(sys.argv) > 1 else "/artifacts"))
    except ValueError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    out.mkdir(parents=True, exist_ok=True)
    for prefix in ("cluster-health", "operator-health"):
        (out / f"{prefix}.xml").write_text(
            _testsuite_xml(name=prefix, msg=_SKIP),
            encoding="utf-8",
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
