#!/usr/bin/env python3
"""Post-process tls-scanner artifacts for MaaS group-test.

The upstream scanner can print PQC COMPLIANCE CHECK: PASSED when every probe is
SkipUnscannable (e.g. connection failures classified as NO_TLS). This script
requires successful TLS probes on allowed ports for each required component.
"""
from __future__ import annotations

import json
import os
import re
import sys
from pathlib import Path


def _parse_int_set(raw: str) -> set[int]:
    out: set[int] = set()
    for part in raw.split(","):
        part = part.strip()
        if not part:
            continue
        out.add(int(part))
    return out


def _component_name(ip_result: dict) -> str:
    oc = ip_result.get("openshift_component") or {}
    name = oc.get("component") or oc.get("Component") or ""
    if name:
        return name
    pod = ip_result.get("pod") or {}
    return pod.get("component") or pod.get("name") or ""


def _port_probe_ok(port_result: dict, *, require_pqc: bool) -> bool:
    port = port_result.get("port")
    if port is None:
        return False
    versions = port_result.get("tls_versions") or []
    tls13 = bool(port_result.get("tls13_supported")) or "TLSv1.3" in versions
    if not tls13 and not versions:
        return False
    if not tls13:
        return False
    if require_pqc and not port_result.get("mlkem_supported"):
        return False
    return True


def main() -> int:
    artifact_dir = Path(os.environ.get("ARTIFACT_DIR", ".")).resolve()
    results_path = artifact_dir / "results.json"
    output_log = artifact_dir / "output.log"

    allow_ports = _parse_int_set(os.environ.get("TLS_SCAN_PORT_ALLOWLIST", "443,8443"))
    required = [
        c.strip()
        for c in os.environ.get("TLS_REQUIRED_COMPONENTS", "maas-api,maas-controller").split(",")
        if c.strip()
    ]
    require_pqc = os.environ.get("TLS_REQUIRE_PQC", "true").lower() in ("1", "true", "yes")

    if not results_path.is_file():
        print(f"Missing results.json under {artifact_dir}", file=sys.stderr)
        return 1

    try:
        data = json.loads(results_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        print(f"Invalid JSON in {results_path}: {exc}", file=sys.stderr)
        return 1

    ip_results = data.get("ip_results") or []
    scan_errors = data.get("scan_errors") or []

    # component -> successful probes on allowlisted ports
    ok_by_component: dict[str, list[tuple[str, int]]] = {c: [] for c in required}
    allowed_attempts = 0

    for ip_result in ip_results:
        comp = _component_name(ip_result)
        if comp not in ok_by_component:
            continue
        ip = ip_result.get("ip") or "?"
        for pr in ip_result.get("port_results") or []:
            port = pr.get("port")
            if port not in allow_ports:
                continue
            allowed_attempts += 1
            if _port_probe_ok(pr, require_pqc=require_pqc):
                ok_by_component[comp].append((ip, int(port)))

    errors: list[str] = []
    for comp in required:
        hits = ok_by_component[comp]
        if not hits:
            errors.append(
                f"component {comp}: no successful TLS"
                f"{' + PQC' if require_pqc else ''} probe on ports {sorted(allow_ports)}"
            )

    if allowed_attempts == 0:
        errors.append(
            f"no scan jobs targeted allowlisted ports {sorted(allow_ports)} "
            "(check component filter and port discovery)"
        )

    if output_log.is_file():
        text = output_log.read_text(encoding="utf-8", errors="replace")
        if re.search(r"PQC COMPLIANCE CHECK:\s*PASSED", text, re.I):
            if errors:
                errors.append(
                    "scanner printed PQC COMPLIANCE CHECK: PASSED but post-validation failed "
                    "(likely connection failures marked SkipUnscannable upstream)"
                )
        connect_fails = len(re.findall(r"Fatal error: Can't connect", text))
        if connect_fails and errors:
            errors.append(f"output.log: {connect_fails} testssl connection failure(s) on scanned targets")

    if scan_errors:
        allow_err = [e for e in scan_errors if e.get("port") in allow_ports]
        if allow_err and errors:
            errors.append(f"results.json: {len(allow_err)} scan_error entries on allowlisted ports")

    if errors:
        print("TLS/PQC post-validation failed:", file=sys.stderr)
        for msg in errors:
            print(f"  - {msg}", file=sys.stderr)
        return 1

    print("TLS/PQC post-validation passed:")
    for comp in required:
        for ip, port in ok_by_component[comp]:
            print(f"  - {comp}: {ip}:{port}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
