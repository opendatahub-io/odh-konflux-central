"""Detect unreachable cluster API (EPHC lease DNS death, etc.) for fail-fast infra attribution."""

from __future__ import annotations

import os
import socket
import time
from urllib.parse import urlparse

from install.dsc_install import _discover_operator_admission_webhook_service, oc_run
from suite.its_trigger_params import CLUSTER_SOURCE_EPHC, is_ephemeral_hosted_cluster_source

_INFRA_PREFIX = "cluster API unreachable"
_CONSOLE_INFRA_PREFIX = "openshift console route unreachable"

# OpenShift CI provision-ephemeral-cluster guests (konflux-ocp-ci.dev). Console/apps
# hostnames often do not resolve from Konflux Tekton pods while the API server is healthy.
_OPENSHIFT_CI_EPHEMERAL_API_MARKERS: tuple[str, ...] = (
    "konflux-ocp-ci.dev",
    ".prod.konflux-ocp-ci.",
)

_UNREACHABLE_MARKERS: tuple[str, ...] = (
    "no such host",
    "name or service not known",
    "unable to connect to the server",
    "connection refused",
    "connection reset",
    "i/o timeout",
    "getaddrinfo",
    "enotfound",
    "nameresolutionerror",
    "dial tcp: lookup",
    "failed to resolve",
    "max retries exceeded",
    "no endpoints available for service",
)


def cluster_api_unreachable_text(*, stderr: str = "", stdout: str = "") -> str:
    """Return a one-line infra reason when *stderr*/*stdout* indicate API DNS/connect failure."""
    blob = f"{stderr}\n{stdout}".strip()
    if not blob:
        return ""
    lower = blob.lower()
    for marker in _UNREACHABLE_MARKERS:
        if marker in lower:
            for line in blob.splitlines():
                if marker in line.lower():
                    snippet = line.strip()[:240]
                    return f"{_INFRA_PREFIX}: {snippet}"
            return f"{_INFRA_PREFIX}: {marker}"
    return ""


def _oci_guest_apps_hostname(hostname: str) -> bool:
    """True for OpenShift CI guest routes (*.apps.*.konflux-ocp-ci.dev)."""
    host = (hostname or "").strip().lower().rstrip(".")
    if not host:
        return False
    return any(marker in host for marker in _OPENSHIFT_CI_EPHEMERAL_API_MARKERS)


_API_PROBE_ATTEMPTS = 3
_API_PROBE_INTERVAL_SEC = 10.0
_WEBHOOK_PROBE_ATTEMPTS = 3
_WEBHOOK_PROBE_INTERVAL_SEC = 10.0


def _probe_cluster_api_unreachable_once() -> str:
    """Single oc probe; empty when the API responds or the error is not infra death."""
    try:
        result = oc_run(
            ["get", "datasciencecluster", "default-dsc"],
            check=False,
            capture_output=True,
            timeout=15,
        )
    except Exception as exc:
        return cluster_api_unreachable_text(stderr=str(exc)) or f"{_INFRA_PREFIX}: {exc}"
    return cluster_api_unreachable_text(
        stderr=result.stderr or "",
        stdout=result.stdout or "",
    )


def cluster_api_unreachable_reason(*, probe: bool = True) -> str:
    """Return non-empty infra reason when the cluster API cannot be reached."""
    if not probe:
        return ""
    last = ""
    for attempt in range(_API_PROBE_ATTEMPTS):
        last = _probe_cluster_api_unreachable_once()
        if not last:
            return ""
        if attempt < _API_PROBE_ATTEMPTS - 1:
            time.sleep(_API_PROBE_INTERVAL_SEC)
    return last


def _oc_infra_reason_from_exception(exc: Exception) -> str:
    return cluster_api_unreachable_text(stderr=str(exc)) or f"{_INFRA_PREFIX}: {exc}"


def _oc_infra_reason_from_output(*, stderr: str = "", stdout: str = "") -> str:
    return cluster_api_unreachable_text(stderr=stderr, stdout=stdout)


def _service_has_ready_endpoints(*, service: str, namespace: str) -> tuple[bool, str]:
    try:
        result = oc_run(
            [
                "get",
                "endpoints",
                service,
                "-n",
                namespace,
                "-o",
                "jsonpath={.subsets[*].addresses[*].ip}",
            ],
            check=False,
            capture_output=True,
            text=True,
            timeout=15,
        )
    except Exception as exc:
        return False, _oc_infra_reason_from_exception(exc)
    if result.returncode == 0 and (result.stdout or "").strip():
        return True, ""
    api_msg = _oc_infra_reason_from_output(
        stderr=result.stderr or "",
        stdout=result.stdout or "",
    )
    if api_msg:
        return False, api_msg
    return False, f"{_INFRA_PREFIX}: {service} webhook has no endpoints in {namespace}"


def operator_admission_webhook_unavailable_reason(*, probe: bool = True) -> str:
    """Return infra reason when the operator admission webhook Service has no endpoints."""
    if not probe:
        return ""
    ns = (os.environ.get("OPERATOR_NAMESPACE") or "redhat-ods-operator").strip()
    svc = _discover_operator_admission_webhook_service(ns)
    last = ""
    for attempt in range(_WEBHOOK_PROBE_ATTEMPTS):
        ready, reason = _service_has_ready_endpoints(service=svc, namespace=ns)
        if ready:
            return ""
        last = reason
        if attempt < _WEBHOOK_PROBE_ATTEMPTS - 1:
            time.sleep(_WEBHOOK_PROBE_INTERVAL_SEC)
    return last


def _console_hostname_unreachable_reason(hostname: str) -> str:
    """Return infra reason when the console route hostname does not resolve."""
    host = (hostname or "").strip().rstrip(".")
    if not host:
        return f"{_CONSOLE_INFRA_PREFIX}: empty hostname"
    try:
        socket.getaddrinfo(host, 443, type=socket.SOCK_STREAM)
    except OSError as exc:
        return f"{_CONSOLE_INFRA_PREFIX}: {exc}"
    return ""


def openshift_console_route_unavailable_reason(*, probe: bool = True) -> str:
    """Return infra reason when the cluster console route hostname is unreachable (EPHC lease DNS)."""
    if not probe:
        return ""
    try:
        result = oc_run(
            ["whoami", "--show-console"],
            check=False,
            capture_output=True,
            timeout=15,
        )
    except Exception as exc:
        msg = cluster_api_unreachable_text(stderr=str(exc))
        if msg:
            return f"{_CONSOLE_INFRA_PREFIX}: {exc}"
        return f"{_CONSOLE_INFRA_PREFIX}: {exc}"
    msg = cluster_api_unreachable_text(
        stderr=result.stderr or "",
        stdout=result.stdout or "",
    )
    if msg:
        snippet = (result.stderr or result.stdout or "").strip()[:240]
        return f"{_CONSOLE_INFRA_PREFIX}: {snippet}" if snippet else msg
    if result.returncode != 0:
        snippet = (result.stderr or result.stdout or "").strip()[:240]
        if snippet:
            return f"{_CONSOLE_INFRA_PREFIX}: {snippet}"
        return ""
    console_url = (result.stdout or "").strip()
    if not console_url:
        return ""
    host = urlparse(console_url).hostname or ""
    if _oci_guest_apps_hostname(host):
        # Tekton pods often cannot resolve guest apps routes; verify uses in-cluster curl.
        return ""
    return _console_hostname_unreachable_reason(host)


def _rh_ai_route_hostname() -> str:
    apps_ns = (os.environ.get("APPLICATIONS_NAMESPACE") or "redhat-ods-applications").strip()
    try:
        result = oc_run(
            [
                "get",
                "route",
                "rh-ai",
                "-n",
                apps_ns,
                "-o",
                "jsonpath={.spec.host}",
            ],
            check=False,
            capture_output=True,
            timeout=15,
        )
    except Exception:
        return ""
    if result.returncode != 0:
        return ""
    return (result.stdout or "").strip()


def openshift_guest_rh_ai_route_tekton_unreachable_reason(*, probe: bool = True) -> str:
    """When the cluster is an OCI guest, rh-ai.apps routes often fail DNS from Tekton (Playwright, MLflow)."""
    if not probe or not _extended_ephc_infra_probes_enabled():
        return ""
    try:
        result = oc_run(
            ["whoami", "--show-console"],
            check=False,
            capture_output=True,
            timeout=15,
        )
    except Exception:
        return ""
    if result.returncode != 0:
        return ""
    console_host = urlparse((result.stdout or "").strip()).hostname or ""
    if not _oci_guest_apps_hostname(console_host):
        return ""
    rh_ai_host = _rh_ai_route_hostname()
    if not rh_ai_host or not _oci_guest_apps_hostname(rh_ai_host):
        return ""
    return _console_hostname_unreachable_reason(rh_ai_host)


def _extended_ephc_infra_probes_enabled() -> bool:
    """Webhook/console probes run on EPHC Tekton steps (not local unit tests)."""
    source = os.environ.get("CLUSTER_SOURCE", "").strip()
    if not is_ephemeral_hosted_cluster_source(source):
        return False
    in_pipeline = bool(
        os.environ.get("PIPELINE_RUN_NAME", "").strip()
        or os.environ.get("ARTIFACTS_DIR", "").strip()
    )
    return in_pipeline and bool(os.environ.get("KUBECONFIG", "").strip())


def _prior_cluster_api_unreachable_reason() -> str:
    from steps.cluster_prep_state import cluster_api_unreachable_marker_reason

    return cluster_api_unreachable_marker_reason()


def _persist_cluster_api_unreachable(reason: str) -> None:
    if not reason.lower().startswith(_INFRA_PREFIX.lower()):
        return
    from steps.cluster_prep_state import mark_cluster_api_unreachable

    mark_cluster_api_unreachable(reason)


def _clear_cluster_api_unreachable_marker() -> None:
    from steps.cluster_prep_state import clear_cluster_api_unreachable_marker

    clear_cluster_api_unreachable_marker()


def cluster_smoke_infra_blocked_reason(*, probe: bool = True) -> str:
    """Combined infra probe: API, operator webhook, and console route (EPHC lease death)."""
    prior = _prior_cluster_api_unreachable_reason()
    if prior:
        if not probe:
            return prior
        reason = cluster_api_unreachable_reason(probe=True)
        if reason:
            return reason
        _clear_cluster_api_unreachable_marker()
    else:
        reason = cluster_api_unreachable_reason(probe=probe)
        if reason:
            _persist_cluster_api_unreachable(reason)
            return reason
    if not probe or not _extended_ephc_infra_probes_enabled():
        return ""
    reason = operator_admission_webhook_unavailable_reason(probe=probe)
    if reason:
        return reason
    reason = openshift_console_route_unavailable_reason(probe=probe)
    if reason:
        return reason
    return ""


def is_definitive_infra_error(message: str) -> bool:
    """True when *message* is a hard infra block (API death, MaaS Removed, gateway never ready)."""
    if not message:
        return False
    lower = message.lower()
    if lower.startswith(_INFRA_PREFIX.lower()):
        return True
    markers = (
        "modelsasservice.managementstate=removed",
        "models as service.managementstate=removed",
        "maas gateway https service not ready",
        "namespace \"oidc\" not found",
        'namespaces "oidc" not found',
        "byoidc-credentials",
        "kuadrant/authorino dependency operators are missing",
        "webhook has no endpoints",
        "openshift console unavailable",
        "openshift console route unreachable",
        "broken hypershift admission webhook",
        "xxx-invalid-service-xxx",
        "block-resources.hypershift.openshift.io",
    )
    return any(m in lower for m in markers)
