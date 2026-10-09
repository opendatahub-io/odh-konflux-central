#!/usr/bin/env python3
"""Emit skipped JUnit XML for test-only BVT (no workload cluster / no ODH APIs on worker)."""

from __future__ import annotations

import sys
import xml.etree.ElementTree as ET
from pathlib import Path

_SKIP = (
    "test-only PRODUCT: full opendatahub-tests BVT needs a cluster with Open Data Hub APIs "
    "(e.g. DataScienceCluster). Use --external-kubeconfig or PRODUCT rhoai/odh for EPHC-backed BVT."
)

_ARTIFACTS_ROOT = Path("/artifacts").resolve()
_EXTRA_ROOTS = (
    Path("/workspace/tests-shared/tests-payload").resolve(),
    Path("/workspace/tests-shared/tests-payload/results").resolve(),
    Path("/workspace/tests-shared/artifacts").resolve(),
    Path("/workspace/tests-shared/artifacts/bvt").resolve(),
)


def _safe_artifact_out(raw: Path) -> Path:
    out = raw.resolve() if raw.is_absolute() else (_ARTIFACTS_ROOT / raw).resolve()
    allowed = (_ARTIFACTS_ROOT, *_EXTRA_ROOTS)
    if not any(out == root or root in out.parents for root in allowed):
        raise ValueError(f"output directory must be under allowed artifact roots, got {raw}")
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
        {"name": "bvt_skipped_no_odh_cluster", "classname": "rhoai-e2e"},
    )
    ET.SubElement(testcase, "skipped", {"message": msg})
    if hasattr(ET, "indent"):
        ET.indent(testsuite, space="  ")
    body = ET.tostring(testsuite, encoding="unicode", xml_declaration=False)
    return '<?xml version="1.0" encoding="utf-8"?>\n' + body


def write_placeholder_junit(out: Path) -> int:
    """Write skipped cluster/operator health JUnit XML under *out*."""
    out.mkdir(parents=True, exist_ok=True)
    for prefix in ("cluster-health", "operator-health"):
        (out / f"{prefix}.xml").write_text(
            _testsuite_xml(name=prefix, msg=_SKIP),
            encoding="utf-8",
        )
    return 0


def main() -> int:
    try:
        out = _safe_artifact_out(Path(sys.argv[1] if len(sys.argv) > 1 else "/artifacts"))
    except ValueError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    return write_placeholder_junit(out)


if __name__ == "__main__":
    raise SystemExit(main())
