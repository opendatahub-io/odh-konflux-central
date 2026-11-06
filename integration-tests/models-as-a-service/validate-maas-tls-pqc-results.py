#!/usr/bin/env python3
"""Validate tls-scanner results.json for MaaS Konflux group-test PQC gate.

Upstream tls-scanner can report PQC COMPLIANCE CHECK: PASSED when some filtered
components were unreachable (NetworkPolicy) or unscanned. Require each in-scope
component to have at least one successful TLS 1.3 + ML-KEM probe on an allowed port.

Strict mode (TLS_STRICT_COMPONENTS): components listed must not be SKIPped (no pods
matched). Default strict list is maas-api,maas-controller so the gate cannot pass
vacuously if those workloads are missing from the scan.
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path


def _parse_csv(raw: str) -> list[str]:
    """Split a comma-separated list into non-empty stripped tokens."""
    return [part.strip() for part in raw.split(",") if part.strip()]


def _parse_ports(raw: str) -> set[int]:
    """Parse comma-separated port numbers into a set of ints."""
    out: set[int] = set()
    for part in _parse_csv(raw):
        out.add(int(part))
    return out


def _component_matches(name: str, required: str) -> bool:
    """Return whether tls-scanner openshift_component name satisfies the filter entry."""
    if name == required:
        return True
    # tls-scanner often derives component from image repo (e.g. odh-maas-discovery).
    aliases = {
        "maas-discovery": {"maas-discovery", "odh-maas-discovery"},
    }
    if required in aliases and name in aliases[required]:
        return True
    # CronJob / hook pods share the maas-api image but are not the serving API.
    if required == "maas-api" and name.startswith("maas-api") and name != "maas-api":
        return False
    return False


def _port_is_pqc_ready(port_result: dict) -> bool:
    """True if port_result represents TLS 1.3 + ML-KEM readiness per MaaS gate."""
    if port_result.get("status") != "OK":
        return False
    if port_result.get("tls13_supported") is not True:
        return False
    if port_result.get("mlkem_supported") is not True:
        return False
    readiness = port_result.get("tls_readiness") or {}
    if readiness.get("pqc_capable") is False:
        return False
    return True


def main() -> int:
    """Load results.json and enforce PQC probes for filtered (and strict) components."""
    artifact_dir = Path(os.environ.get("ARTIFACT_DIR", "/workspace/artifacts-dir"))
    results_path = artifact_dir / "tls-scanner" / "results.json"
    if not results_path.is_file():
        print(f"ERROR: missing {results_path}", file=sys.stderr)
        return 1

    component_filter_raw = os.environ.get("TLS_COMPONENT_FILTER", "")
    if not component_filter_raw.strip():
        print(
            "ERROR: TLS_COMPONENT_FILTER must be set (use the same list as the pipeline param)",
            file=sys.stderr,
        )
        return 1
    components = _parse_csv(component_filter_raw)

    strict_raw = os.environ.get("TLS_STRICT_COMPONENTS", "maas-api,maas-controller")
    strict_set = set(_parse_csv(strict_raw)) if strict_raw.strip() else set()
    missing_strict = strict_set - set(components)
    if missing_strict:
        print(
            "ERROR: strict components missing from TLS_COMPONENT_FILTER: "
            + ", ".join(sorted(missing_strict)),
            file=sys.stderr,
        )
        return 1

    required_ports = _parse_ports(os.environ.get("TLS_PQC_REQUIRED_PORTS", "8443,9443"))

    with results_path.open(encoding="utf-8") as handle:
        payload = json.load(handle)

    ip_results = payload.get("ip_results") or []
    failures: list[str] = []
    ok_count = 0

    for component in components:
        entries = [
            entry
            for entry in ip_results
            if _component_matches(
                (entry.get("openshift_component") or {}).get("component") or "",
                component,
            )
        ]
        if not entries:
            line = f"SKIP {component}: no pods matched in scan"
            print(line)
            if component in strict_set:
                failures.append(f"{component}: strict required component had no pods in scan (vacuous SKIP)")
            continue

        matched_ports: list[str] = []
        for entry in entries:
            pod = entry.get("pod") or {}
            pod_ref = f"{pod.get('Namespace', '?')}/{pod.get('Name', '?')}"
            for port_result in entry.get("port_results") or []:
                port = port_result.get("port")
                if port not in required_ports:
                    continue
                if _port_is_pqc_ready(port_result):
                    matched_ports.append(f"{pod_ref}:{port}")
        if not matched_ports:
            failures.append(
                f"{component}: no port in {sorted(required_ports)} with status=OK, "
                f"tls13_supported=true, mlkem_supported=true"
            )
        else:
            print(f"OK {component}: {', '.join(matched_ports)}")
            ok_count += 1

    if ok_count == 0 and not failures:
        failures.append(
            "no component produced an OK PQC probe — check COMPONENT_FILTER, scan reachability, or results.json"
        )

    if failures:
        print("MaaS TLS/PQC validation failed:", file=sys.stderr)
        for line in failures:
            print(f"  - {line}", file=sys.stderr)
        return 1

    print("MaaS TLS/PQC validation passed for all required components")
    return 0


if __name__ == "__main__":
    sys.exit(main())
