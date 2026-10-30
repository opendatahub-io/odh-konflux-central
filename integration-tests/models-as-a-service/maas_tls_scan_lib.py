"""Shared helpers for MaaS Konflux TLS/PQC group-test scanning."""
from __future__ import annotations

import json
import os
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Callable


@dataclass(frozen=True)
class ServiceTarget:
    component: str
    namespace: str
    service: str
    port: int

    def fqdn(self, cluster_local: bool = True) -> str:
        host = f"{self.service}.{self.namespace}.svc"
        if cluster_local:
            host += ".cluster.local"
        return host

    def host_port(self, cluster_local: bool = True) -> str:
        return f"{self.fqdn(cluster_local)}:{self.port}"

    @staticmethod
    def parse_entry(entry: str) -> ServiceTarget:
        """Parse component:namespace/service:port."""
        entry = entry.strip()
        if not entry:
            raise ValueError("empty service target entry")
        comp, _, rest = entry.partition(":")
        if not comp or not rest or "/" not in rest:
            raise ValueError(
                f"invalid target {entry!r}; want component:namespace/service:port"
            )
        ns_svc, _, port_s = rest.rpartition(":")
        if not port_s.isdigit():
            raise ValueError(f"invalid port in target {entry!r}")
        ns, _, svc = ns_svc.partition("/")
        if not ns or not svc:
            raise ValueError(f"invalid namespace/service in target {entry!r}")
        return ServiceTarget(
            component=comp.strip(),
            namespace=ns.strip(),
            service=svc.strip(),
            port=int(port_s),
        )


DEFAULT_SERVICE_TARGETS: tuple[ServiceTarget, ...] = (
    ServiceTarget("maas-api", "odh-ai-gateway-infra", "maas-api", 8443),
    ServiceTarget("maas-controller", "opendatahub", "maas-controller-metrics", 8443),
)


def parse_service_targets(raw: str | None) -> list[ServiceTarget]:
    if not raw or not raw.strip():
        return list(DEFAULT_SERVICE_TARGETS)
    out: list[ServiceTarget] = []
    for part in raw.split(","):
        part = part.strip()
        if part:
            out.append(ServiceTarget.parse_entry(part))
    if not out:
        raise ValueError("TLS_SCAN_SERVICE_TARGETS parsed to empty list")
    return out


def build_targets_meta(targets: list[ServiceTarget]) -> dict:
    host_to_component: dict[str, str] = {}
    for t in targets:
        host_to_component[t.fqdn(True)] = t.component
        host_to_component[t.fqdn(False)] = t.component
        host_to_component[t.service] = t.component
    return {
        "mode": "service",
        "targets": [
            {
                "component": t.component,
                "namespace": t.namespace,
                "service": t.service,
                "port": t.port,
                "host_port": t.host_port(True),
            }
            for t in targets
        ],
        "host_to_component": host_to_component,
    }


def targets_cli_argument(targets: list[ServiceTarget]) -> str:
    return ",".join(t.host_port(True) for t in targets)


def filter_targets_by_allowlist(
    targets: list[ServiceTarget], allow_ports: set[int]
) -> list[ServiceTarget]:
    return [t for t in targets if t.port in allow_ports]


OcGetService = Callable[[str, str], bool]


def discover_service_targets(
    targets: list[ServiceTarget],
    service_exists: OcGetService | None = None,
) -> tuple[list[ServiceTarget], list[str]]:
    """Drop targets whose Service is missing; return (kept, warnings)."""
    if service_exists is None:
        return targets, []

    kept: list[ServiceTarget] = []
    warnings: list[str] = []
    for t in targets:
        if service_exists(t.namespace, t.service):
            kept.append(t)
        else:
            warnings.append(f"Service {t.namespace}/{t.service} not found (skip)")
    return kept, warnings


def oc_service_exists(namespace: str, name: str) -> bool:
    try:
        subprocess.run(
            ["oc", "get", "svc", name, "-n", namespace],
            check=True,
            capture_output=True,
            timeout=60,
        )
        return True
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired, FileNotFoundError):
        return False


def _parse_int_set(raw: str) -> set[int]:
    out: set[int] = set()
    for part in raw.split(","):
        part = part.strip()
        if part:
            out.add(int(part))
    return out


def _component_name(ip_result: dict, host_map: dict[str, str]) -> str:
    ip = (ip_result.get("ip") or "").strip()
    if ip in host_map:
        return host_map[ip]
    oc = ip_result.get("openshift_component") or {}
    name = oc.get("component") or oc.get("Component") or ""
    if name:
        return name
    for host, comp in host_map.items():
        if host in ip or ip in host:
            return comp
    pod = ip_result.get("pod") or {}
    if pod.get("component"):
        return str(pod["component"])
    return ""


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


def validate_scan_artifacts(
    artifact_dir: Path,
    *,
    allow_ports: set[int],
    required_components: list[str],
    require_pqc: bool,
    host_map: dict[str, str] | None = None,
) -> tuple[bool, list[str], dict[str, list[tuple[str, int]]]]:
    """Return (ok, error_messages, ok_by_component)."""
    host_map = host_map or {}
    results_path = artifact_dir / "results.json"
    output_log = artifact_dir / "output.log"

    if not results_path.is_file():
        return False, [f"Missing results.json under {artifact_dir}"], {}

    try:
        data = json.loads(results_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        return False, [f"Invalid JSON in {results_path}: {exc}"], {}

    ip_results = data.get("ip_results") or []
    scan_errors = data.get("scan_errors") or []

    ok_by_component: dict[str, list[tuple[str, int]]] = {c: [] for c in required_components}
    allowed_attempts = 0

    for ip_result in ip_results:
        comp = _component_name(ip_result, host_map)
        if comp not in ok_by_component:
            continue
        ip = ip_result.get("ip") or "?"
        for pr in ip_result.get("port_results") or []:
            port = pr.get("port")
            if port not in allow_ports:
                continue
            allowed_attempts += 1
            if _port_probe_ok(pr, require_pqc=require_pqc):
                ok_by_component[comp].append((str(ip), int(port)))

    errors: list[str] = []
    for comp in required_components:
        if not ok_by_component[comp]:
            errors.append(
                f"component {comp}: no successful TLS"
                f"{' + PQC' if require_pqc else ''} probe on ports {sorted(allow_ports)}"
            )

    if allowed_attempts == 0:
        errors.append(
            f"no scan jobs targeted allowlisted ports {sorted(allow_ports)} "
            "(check targets / component filter and port discovery)"
        )

    if output_log.is_file():
        text = output_log.read_text(encoding="utf-8", errors="replace")
        if re.search(r"PQC COMPLIANCE CHECK:\s*PASSED", text, re.I) and errors:
            errors.append(
                "scanner printed PQC COMPLIANCE CHECK: PASSED but post-validation failed "
                "(likely connection failures marked SkipUnscannable upstream)"
            )
        connect_fails = len(re.findall(r"Fatal error: Can't connect", text))
        if connect_fails and errors:
            errors.append(
                f"output.log: {connect_fails} testssl connection failure(s) on scanned targets"
            )

    if scan_errors:
        allow_err = [e for e in scan_errors if e.get("port") in allow_ports]
        if allow_err and errors:
            errors.append(
                f"results.json: {len(allow_err)} scan_error entries on allowlisted ports"
            )

    return (not errors, errors, ok_by_component)


def load_host_map_from_meta(artifact_dir: Path) -> dict[str, str]:
    meta_path = artifact_dir / "scan-targets.meta.json"
    if not meta_path.is_file():
        return {}
    try:
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {}
    return dict(meta.get("host_to_component") or {})


def validation_config_from_env() -> tuple[set[int], list[str], bool]:
    allow_ports = _parse_int_set(os.environ.get("TLS_SCAN_PORT_ALLOWLIST", "443,8443"))
    required = [
        c.strip()
        for c in os.environ.get("TLS_REQUIRED_COMPONENTS", "maas-api,maas-controller").split(",")
        if c.strip()
    ]
    require_pqc = os.environ.get("TLS_REQUIRE_PQC", "true").lower() in ("1", "true", "yes")
    return allow_ports, required, require_pqc
