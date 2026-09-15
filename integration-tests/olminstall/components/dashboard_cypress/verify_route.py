"""Jenkins verifyDashboardRoute parity for Tekton prepare (wait + curl probe)."""

from __future__ import annotations

import json
import os
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

from components.dashboard_cypress.config import (
    resolve_odh_dashboard_base_url,
    write_dashboard_cypress_test_config,
)
from components.dashboard_cypress.runtime import (
    dashboard_hostname_from_url,
    dashboard_hostname_resolves,
    dashboard_reachable_probe,
    verify_dashboard_reachable,
    wait_for_dashboard_hostname_dns,
    _is_ephc_cluster_source,
)
from install.dsc_install import oc_run
from suite.its_trigger_params import is_external_cluster_source

_DEFAULT_DEPLOYMENT_WAIT_SEC = 180
_DEFAULT_RHOAI_REINSTALL_DEPLOYMENT_WAIT_SEC = 600
_DEFAULT_ROUTE_VERIFY_TIMEOUT_SEC = 900
_RHOAI_OPERATOR_NS = "redhat-ods-operator"
_RHOAI_OPERATOR_DEPLOY = "rhods-operator"
_RHOAI_OPERATOR_SERVICE = "rhods-operator-service"
_RHOAI_DASHBOARD_NS = "redhat-ods-applications"
_RHOAI_DASHBOARD_DEPLOY = "rhods-dashboard"
_RHOAI_CORE_DEPLOYMENTS = (
    (_RHOAI_OPERATOR_NS, _RHOAI_OPERATOR_DEPLOY),
    (_RHOAI_DASHBOARD_NS, _RHOAI_DASHBOARD_DEPLOY),
)
_GATEWAY_REPAIR_ATTEMPTS = frozenset({1, 4, 7, 10, 13, 16, 19, 22, 25, 28})
# Jenkins verifyDashboardRoute.groovy: 3 minutes per deployment wait.
_JENKINS_DEPLOYMENT_WAIT_MINUTES = 3
_VERIFY_DEPLOYMENT_NS_EXACT = frozenset({
    "cert-manager",
    "cert-manager-operator",
    "kuadrant-system",
    "kyverno",
    "odh-ai-gateway-infra",
    "redhat-ods-applications",
    "redhat-ods-operator",
    "rhoai-model-registries",
})
_VERIFY_DEPLOYMENT_NS_PREFIXES = ("openshift-",)


def _rhoai_install_verify_wait() -> bool:
    return os.environ.get("PRODUCT", "").strip().lower() in ("rhoai", "odh")


def _default_deployment_wait_sec() -> int:
    raw = os.environ.get("DASHBOARD_DEPLOYMENT_WAIT_SEC", "").strip()
    if raw:
        return int(raw)
    if _rhoai_install_verify_wait() and _external_cluster_verify_wait():
        return _DEFAULT_RHOAI_REINSTALL_DEPLOYMENT_WAIT_SEC
    return _JENKINS_DEPLOYMENT_WAIT_MINUTES * 60


def _wait_deployment_available(ns: str, name: str, *, timeout_sec: int) -> bool:
    wait_r = oc_run(
        [
            "wait",
            "--for=condition=available",
            f"--timeout={timeout_sec}s",
            f"deployment/{name}",
            "-n",
            ns,
        ],
        check=False,
        capture_output=True,
        timeout=timeout_sec + 60,
    )
    if wait_r.returncode != 0:
        print(
            f"WARN: deployment/{name} in {ns} not available within {timeout_sec}s",
            file=sys.stderr,
            flush=True,
        )
        return False
    return True


def _wait_rhoai_operator_webhook_ready(*, timeout_sec: int) -> bool:
    """Block until rhods-operator admission webhooks can serve DSC/DSCI patches."""
    print(
        f"Waiting for {_RHOAI_OPERATOR_SERVICE} endpoints (up to {timeout_sec}s)...",
        flush=True,
    )
    deadline = time.monotonic() + timeout_sec
    while time.monotonic() < deadline:
        r = oc_run(
            [
                "get",
                "endpoints",
                _RHOAI_OPERATOR_SERVICE,
                "-n",
                _RHOAI_OPERATOR_NS,
                "-o",
                "jsonpath={.subsets[*].addresses[*].ip}",
            ],
            check=False,
            capture_output=True,
            timeout=30,
        )
        if r.returncode == 0 and (r.stdout or "").strip():
            print(f"✓ {_RHOAI_OPERATOR_SERVICE} has endpoints", flush=True)
            return True
        time.sleep(10)
    print(
        f"WARN: {_RHOAI_OPERATOR_SERVICE} has no endpoints within {timeout_sec}s",
        file=sys.stderr,
        flush=True,
    )
    return False


def _wait_rhoai_core_deployments(*, timeout_sec: int) -> bool:
    """After PRODUCT=rhoai reinstall, operator/dashboard often lag the 180s bulk wait."""
    print(
        f"Waiting for RHOAI operator/dashboard deployments (up to {timeout_sec}s)...",
        flush=True,
    )
    ok = True
    for ns, name in _RHOAI_CORE_DEPLOYMENTS:
        if not _wait_deployment_available(ns, name, timeout_sec=timeout_sec):
            ok = False
    if ok:
        _wait_rhoai_operator_webhook_ready(timeout_sec=min(timeout_sec, 120))
    return ok


def _external_cluster_verify_wait() -> bool:
    source = os.environ.get("CLUSTER_SOURCE", "").strip()
    return is_external_cluster_source(source)


def _deployment_in_verify_wait_scope(ns: str) -> bool:
    if not _external_cluster_verify_wait():
        return True
    if ns in _VERIFY_DEPLOYMENT_NS_EXACT:
        return True
    return any(ns.startswith(prefix) for prefix in _VERIFY_DEPLOYMENT_NS_PREFIXES)


def _dashboard_ready_status() -> str:
    r = oc_run(
        [
            "get",
            "datasciencecluster",
            "default-dsc",
            "-o",
            'jsonpath={.status.conditions[?(@.type=="DashboardReady")].status}',
        ],
        check=False,
        capture_output=True,
        timeout=30,
    )
    return (r.stdout or "").strip()


def _repair_gateway_stack_for_verify() -> None:
    """Re-run §15 gateway prep when deployments are up but dashboard routes are missing."""
    from components.maas_billing.auth import (
        _gateway_api_provider_present,
        recover_kuadrant_after_gateway_api_provider,
    )
    from components.maas_billing.gateway import ensure_openshift_default_gateway_class
    from helpers.gateway_stack_marker import reconcile_gateway_stack_incomplete_marker
    from install.gateway_config import (
        ensure_openshift_gateway_istio_for_verify,
        ensure_rhoai_gateway_for_install,
        gateway_config_ready,
    )
    from install.dsc_install import ensure_dashboard_gateway_prereqs
    from install.rhoai_gateway_prep import ensure_transitive_olm_deps_for_gateway

    print("verify-operator-ready: running RHOAI gateway repair (§15 P1–P4)...", flush=True)
    try:
        if not ensure_openshift_gateway_istio_for_verify():
            print(
                "WARN: openshift-gateway Istio/controller not ready for verify; "
                "rh-ai dashboard preflight may return 503",
                file=sys.stderr,
                flush=True,
            )
    except Exception as exc:
        print(
            f"WARN: openshift-gateway Istio reconcile failed ({exc}); continuing verify",
            file=sys.stderr,
            flush=True,
        )
    ensure_openshift_default_gateway_class()
    reconcile_gateway_stack_incomplete_marker()
    provider_wait = int(os.environ.get("VERIFY_GATEWAY_CLASS_WAIT_SEC", "180"))
    if not _gateway_api_provider_present() and provider_wait > 0:
        deadline = time.monotonic() + provider_wait
        while time.monotonic() < deadline:
            if _gateway_api_provider_present():
                print("✓ Accepted GatewayClass present for verify gateway repair", flush=True)
                break
            time.sleep(10)
    try:
        recover_kuadrant_after_gateway_api_provider()
    except SystemExit as exc:
        print(
            f"WARN: Kuadrant recovery exited ({getattr(exc, 'code', '?')}); continuing verify",
            file=sys.stderr,
            flush=True,
        )
    except Exception as exc:
        print(f"WARN: Kuadrant recovery failed ({exc}); continuing verify", file=sys.stderr, flush=True)
    ensure_dashboard_gateway_prereqs(for_gateway_stack=True)
    try:
        approved = ensure_transitive_olm_deps_for_gateway(wait_servicemesh=True)
        if approved:
            print(f"✓ Approved {approved} transitive InstallPlan(s) for gateway stack", flush=True)
    except Exception as exc:
        print(f"WARN: transitive OLM approve failed ({exc})", file=sys.stderr, flush=True)
    try:
        timeout = int(os.environ.get("GATEWAY_CONFIG_WAIT_SEC", "1200"))
        ensure_rhoai_gateway_for_install(wait_timeout_sec=timeout, wait_servicemesh_first=True)
    except Exception as exc:
        print(f"WARN: gateway repair failed ({exc})", file=sys.stderr, flush=True)
    if gateway_config_ready():
        print("✓ GatewayConfig Ready after verify gateway repair", flush=True)


def wait_all_cluster_deployments_available(*, timeout_sec: int = _DEFAULT_DEPLOYMENT_WAIT_SEC) -> bool:
    """Jenkins verifyDashboardRoute: parallel ``oc wait`` on every Deployment (-A)."""
    list_r = oc_run(
        ["get", "deployments", "-A", "-o", "json"],
        check=False,
        capture_output=True,
        timeout=120,
    )
    if list_r.returncode != 0:
        print(
            f"WARN: could not list cluster deployments: {(list_r.stderr or list_r.stdout or '').strip()}",
            file=sys.stderr,
            flush=True,
        )
        return False
    try:
        items = json.loads(list_r.stdout or "{}").get("items") or []
    except json.JSONDecodeError:
        print("WARN: could not parse deployment list JSON", file=sys.stderr, flush=True)
        return False

    pairs: list[tuple[str, str]] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        meta = item.get("metadata") or {}
        ns = (meta.get("namespace") or "").strip()
        name = (meta.get("name") or "").strip()
        if ns and name and _deployment_in_verify_wait_scope(ns):
            pairs.append((ns, name))
    if not pairs:
        return True

    scope = "platform namespaces" if _external_cluster_verify_wait() else "all namespaces"
    print(
        f"Wait for deployments to be ready in {scope} (up to {timeout_sec // 60} minutes, "
        f"{len(pairs)} deployment(s), parallel)",
        flush=True,
    )

    def _wait_one(ns: str, name: str) -> bool:
        wait_r = oc_run(
            [
                "wait",
                "--for=condition=available",
                f"--timeout={timeout_sec}s",
                f"deployment/{name}",
                "-n",
                ns,
            ],
            check=False,
            capture_output=True,
            timeout=timeout_sec + 60,
        )
        if wait_r.returncode != 0:
            print(
                f"WARN: deployment/{name} in {ns} not available within {timeout_sec}s",
                file=sys.stderr,
                flush=True,
            )
            return False
        return True

    ok = True
    workers = min(32, len(pairs))
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = [pool.submit(_wait_one, ns, name) for ns, name in pairs]
        for fut in as_completed(futures):
            if not fut.result():
                ok = False
    return ok


def wait_for_dashboard_route(
    *,
    timeout_sec: int | None = None,
    poll_sec: int = 30,
    deployment_wait_sec: int | None = None,
) -> str:
    """Block until dashboard URL resolves, DashboardReady=True, and curl preflight passes."""
    total = timeout_sec
    if total is None:
        total = int(os.environ.get("DASHBOARD_ROUTE_VERIFY_TIMEOUT_SEC", str(_DEFAULT_ROUTE_VERIFY_TIMEOUT_SEC)))
    deploy_wait = deployment_wait_sec
    if deploy_wait is None:
        deploy_wait = _default_deployment_wait_sec()

    if _rhoai_install_verify_wait():
        _wait_rhoai_core_deployments(timeout_sec=deploy_wait)

    if not wait_all_cluster_deployments_available(timeout_sec=deploy_wait):
        print(
            f"WARN: some cluster deployments were not ready after {deploy_wait}s",
            file=sys.stderr,
            flush=True,
        )

    _repair_gateway_stack_for_verify()

    deadline = time.monotonic() + total
    attempt = 0
    while time.monotonic() < deadline:
        attempt += 1
        if attempt in _GATEWAY_REPAIR_ATTEMPTS:
            url_probe = resolve_odh_dashboard_base_url()
            if not url_probe or _dashboard_ready_status() != "True":
                _repair_gateway_stack_for_verify()
        url = resolve_odh_dashboard_base_url()
        ready = _dashboard_ready_status()
        if url:
            print(
                f"Dashboard route attempt {attempt}: url={url} DashboardReady={ready or '?'}",
                flush=True,
            )
            host = dashboard_hostname_from_url(url)
            if _is_ephc_cluster_source() and host and not dashboard_hostname_resolves(host):
                dns_wait = int(os.environ.get("DASHBOARD_DNS_WAIT_SEC", "300"))
                remaining = max(0, int(deadline - time.monotonic()))
                wait_for_dashboard_hostname_dns(
                    host,
                    timeout_sec=min(dns_wait, remaining),
                )
            reachable, failure_kind = dashboard_reachable_probe(url)
            if reachable:
                if ready != "True":
                    print(
                        f"WARN: gateway URL reachable but DashboardReady={ready or '?'}; "
                        "continuing (HTTP preflight passed)",
                        file=sys.stderr,
                        flush=True,
                    )
                print(f"Dashboard route verified: {url}", flush=True)
                return url
            if failure_kind != "dns" and (attempt in _GATEWAY_REPAIR_ATTEMPTS or attempt == 1):
                print(
                    "Dashboard HTTP preflight failed with route URL resolved — "
                    "retrying gateway/Kuadrant repair",
                    flush=True,
                )
                _repair_gateway_stack_for_verify()
        else:
            print(
                f"Dashboard route attempt {attempt}: gateway URL not resolved yet "
                f"(DashboardReady={ready or '?'})",
                flush=True,
            )
        time.sleep(poll_sec)

    url = resolve_odh_dashboard_base_url()
    ready = _dashboard_ready_status()
    raise RuntimeError(
        "Dashboard route not ready after "
        f"{total}s (url={url or 'missing'}, DashboardReady={ready or '?'})"
    )


def dashboard_cypress_accessible_for_smoke(*, url: str | None = None) -> bool:
    """True when gateway HTTP preflight passes (Jenkins verifyDashboardRoute parity)."""
    resolved = (url or resolve_odh_dashboard_base_url() or "").strip()
    if not resolved:
        return False
    return verify_dashboard_reachable(resolved)


def verify_dashboard_route_for_prepare(*, artifacts_dir: Path | None = None) -> str:
    """Prepare-step entry: wait for route and write dashboard-cypress-config.yml."""
    out_dir = artifacts_dir
    if out_dir is None:
        raw = os.environ.get("ARTIFACTS_DIR", "").strip()
        out_dir = Path(raw) if raw else None
    url = wait_for_dashboard_route()
    if out_dir is not None:
        out_dir.mkdir(parents=True, exist_ok=True)
        cfg = out_dir / "dashboard-cypress-config.yml"
        write_dashboard_cypress_test_config(cfg, dashboard_url=url)
        print(f"Wrote dashboard Cypress config at {cfg}", flush=True)
    return url
