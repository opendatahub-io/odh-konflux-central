"""MaaS and DSC readiness wait loops."""

from __future__ import annotations

import sys
import time

from install.dsc_install import oc_run

from components.maas_billing.common import (
    _dsc_condition,
    _dsc_condition_types,
    _GATEWAY_NAME,
    _GATEWAY_NS,
    _MAAS_APPS_NS,
    _maas_smoke_ready,
    maas_functional_smoke_ready,
    maas_smoke_acceptable_for_run,
    models_as_service_ready_condition_type,
)
from components.maas_billing.timeouts import maas_dsc_prereq_grace_sec


def _wait_for_maas_gateway_programmed(*, timeout_sec: int) -> None:
    """Wait until gateway is smoke-ready (Programmed=True or EPHC functional fallback)."""
    from components.maas_billing.common import (
        _maas_gateway_programmed,
        _maas_gateway_ready_for_smoke,
    )
    from install.gateway_config import cluster_source_is_ephc

    deadline = time.time() + timeout_sec
    while time.time() < deadline:
        ready, reason = _maas_gateway_ready_for_smoke()
        if ready:
            programmed, _ = _maas_gateway_programmed()
            if programmed:
                print(
                    f"✓ MaaS gateway {_GATEWAY_NS}/{_GATEWAY_NAME} Programmed=True",
                    flush=True,
                )
            else:
                print(f"✓ {reason[:200]}", flush=True)
            return
        if cluster_source_is_ephc() and "missing annotation" in reason:
            try:
                from components.maas_billing.gateway import ensure_maas_gateway

                ensure_maas_gateway()
            except Exception as exc:
                print(
                    f"WARN: MaaS gateway re-annotate during wait: {exc}",
                    file=sys.stderr,
                    flush=True,
                )
        if int(time.time()) % 30 < 12:
            print(f"Waiting for MaaS gateway Programmed: {reason[:120]}", flush=True)
        time.sleep(12)
    _, reason = _maas_gateway_ready_for_smoke()
    raise RuntimeError(
        f"MaaS gateway not Programmed after {timeout_sec}s — {reason[:300]}"
    )


_OPENSHIFT_GATEWAY_CONTROLLER_DEPLOYMENTS = (
    "istiod-openshift-gateway",
    "data-science-gateway-data-science-gateway-class",
)


def _ensure_openshift_gateway_controller_ready(*, timeout_sec: int = 180) -> None:
    """Wait for OpenShift Gateway API controller before MaaS gateway can expose HTTPS."""
    for name in _OPENSHIFT_GATEWAY_CONTROLLER_DEPLOYMENTS:
        r = oc_run(
            [
                "wait",
                "--for=condition=available",
                f"--timeout={timeout_sec}s",
                f"deployment/{name}",
                "-n",
                "openshift-ingress",
            ],
            check=False,
            capture_output=True,
            timeout=timeout_sec + 30,
        )
        if r.returncode != 0:
            print(
                f"WARN: deployment/{name} in openshift-ingress not available within {timeout_sec}s",
                file=sys.stderr,
                flush=True,
            )


def _nudge_maas_gateway_reconcile() -> None:
    """Re-apply gateway TLS/annotations so openshift-default listener can reconcile."""
    from components.maas_billing.auth import ensure_authorino_tls
    from components.maas_billing.gateway import (
        ensure_maas_gateway,
        ensure_maas_gateway_https_service_clusterip,
        ensure_maas_gateway_ingress_tls_secret,
        ensure_openshift_default_gateway_class,
    )

    ensure_openshift_default_gateway_class()
    ensure_maas_gateway_ingress_tls_secret()
    ensure_authorino_tls()
    ensure_maas_gateway()
    ensure_maas_gateway_https_service_clusterip()


def _dump_maas_gateway_https_diagnostics() -> None:
    """Best-effort cluster dump when HTTPS Service wait fails (EPHC Programmed gaps)."""
    cmds = (
        ["get", "gatewayclass", "-o", "wide"],
        ["get", "gateway", _GATEWAY_NAME, "-n", _GATEWAY_NS, "-o", "yaml"],
        [
            "get",
            "svc",
            "-n",
            _GATEWAY_NS,
            "-l",
            f"gateway.networking.k8s.io/gateway-name={_GATEWAY_NAME}",
            "-o",
            "wide",
        ],
        [
            "get",
            "pods",
            "-n",
            _GATEWAY_NS,
            "-l",
            f"gateway.networking.k8s.io/gateway-name={_GATEWAY_NAME}",
            "-o",
            "wide",
        ],
        [
            "get",
            "deploy",
            "-n",
            _GATEWAY_NS,
            "-o",
            "wide",
        ],
    )
    for args in cmds:
        r = oc_run(args, check=False, capture_output=True, timeout=45)
        out = (r.stdout or r.stderr or "").strip()
        print(
            f"DIAG maas-gateway [{' '.join(args)}]:\n{out[:4000]}",
            flush=True,
        )


def _maas_gateway_https_wait_detail(svc_detail: str) -> str:
    from components.maas_billing.common import _maas_gateway_programmed

    programmed, prog_detail = _maas_gateway_programmed()
    if programmed:
        return f"Programmed=True but {svc_detail}"
    return prog_detail or svc_detail


def _wait_maas_gateway_https_service(*, timeout_sec: int) -> None:
    """Wait until maas-api can resolve the gateway-owned HTTPS service (strict; no EPHC fallback)."""
    from components.maas_billing.common import _maas_gateway_https_service_ready

    deadline = time.time() + timeout_sec
    while time.time() < deadline:
        svc_ready, svc_detail = _maas_gateway_https_service_ready()
        if svc_ready:
            print(f"✓ MaaS gateway HTTPS service ready ({svc_detail})", flush=True)
            return
        detail = _maas_gateway_https_wait_detail(svc_detail)
        if int(time.time()) % 30 < 12:
            print(f"Waiting for MaaS gateway HTTPS service: {detail[:120]}", flush=True)
        time.sleep(12)
    _, svc_detail = _maas_gateway_https_service_ready()
    raise RuntimeError(
        f"MaaS gateway HTTPS service not ready after {timeout_sec}s — {svc_detail[:300]}"
    )


def _wait_maas_gateway_https_for_models_as_service(*, timeout_sec: int) -> None:
    """modelsAsService requires gateway-owned HTTPS; EPHC functional fallback is insufficient."""
    from components.maas_billing.common import _maas_gateway_https_service_ready
    from steps.cluster_prep_state import (
        maas_gateway_https_blocked_reason,
        mark_maas_gateway_https_failed,
    )

    blocked = maas_gateway_https_blocked_reason()
    if blocked:
        raise RuntimeError(blocked)

    _ensure_openshift_gateway_controller_ready()
    deadline = time.time() + timeout_sec
    last_nudge = 0.0
    while time.time() < deadline:
        svc_ready, svc_detail = _maas_gateway_https_service_ready()
        if svc_ready:
            print(f"✓ MaaS gateway HTTPS service ready ({svc_detail})", flush=True)
            return
        now = time.time()
        if now - last_nudge >= 60:
            last_nudge = now
            _nudge_maas_gateway_reconcile()
        detail = _maas_gateway_https_wait_detail(svc_detail)
        if int(now) % 30 < 12:
            print(
                f"Waiting for MaaS gateway HTTPS service (modelsAsService gate): {detail[:120]}",
                flush=True,
            )
        time.sleep(12)
    _, svc_detail = _maas_gateway_https_service_ready()
    msg = f"MaaS gateway HTTPS service not ready after {timeout_sec}s — {svc_detail[:300]}"
    try:
        _dump_maas_gateway_https_diagnostics()
    except Exception as exc:
        print(f"WARN: MaaS gateway HTTPS diagnostics failed: {exc}", file=sys.stderr, flush=True)
    mark_maas_gateway_https_failed(msg)
    raise RuntimeError(msg)


def _wait_for_maas_smoke_ready(*, timeout_sec: int) -> None:
    from components.maas_billing.common import deps_only_install_dependencies_smoke

    if deps_only_install_dependencies_smoke():
        deadline = time.time() + timeout_sec
        while time.time() < deadline:
            ready, reason = maas_functional_smoke_ready()
            if ready:
                print(
                    "✓ MaaS functional prerequisites ready (deps-only install-dependencies)",
                    flush=True,
                )
                return
            if int(time.time()) % 60 < 12:
                print(f"Waiting for MaaS functional readiness: {reason[:120]}", flush=True)
            time.sleep(12)
        _, reason = maas_functional_smoke_ready()
        raise RuntimeError(
            "MaaS functional prerequisites not ready after "
            f"{timeout_sec}s — {reason[:300]}"
        )

    maas_ready_type = models_as_service_ready_condition_type()
    require_prereq = "MaaSPrerequisitesAvailable" in _dsc_condition_types()
    if require_prereq:
        ready_label = (
            f"MaaSPrerequisitesAvailable + {maas_ready_type} + DSC Ready"
        )
    else:
        ready_label = f"{maas_ready_type} + DSC Ready (MaaSPrerequisitesAvailable not exposed)"
        print(
            "NOTE: DSC has no MaaSPrerequisitesAvailable condition on this cluster; "
            f"waiting for {maas_ready_type} + DSC Ready only",
            flush=True,
        )

    grace_sec = maas_dsc_prereq_grace_sec()
    started = time.time()
    deadline = started + timeout_sec
    grace_deadline = started + grace_sec
    last_nudge = 0.0

    while time.time() < deadline:
        maas_ready_type = models_as_service_ready_condition_type()
        acceptable, accept_reason = maas_smoke_acceptable_for_run()
        if acceptable:
            if "lagging" in accept_reason or "DSC Ready=False" in accept_reason:
                print(f"WARN: {accept_reason}", file=sys.stderr, flush=True)
            print(f"✓ MaaS component prerequisites ready ({ready_label})", flush=True)
            return

        prereq_status, _, prereq_msg = _dsc_condition("MaaSPrerequisitesAvailable")
        maas_status, _, maas_msg = _dsc_condition(maas_ready_type)
        ready_status, _, ready_msg = _dsc_condition("Ready")

        now = time.time()
        if now - last_nudge >= 60:
            last_nudge = now
            from install.dsc_install import (
                ensure_aigateway_models_as_a_service_managed,
                uses_aigateway_models_as_a_service,
            )

            if uses_aigateway_models_as_a_service() and maas_status != "True":
                try:
                    ensure_aigateway_models_as_a_service_managed(wait_timeout_sec=30)
                except Exception as exc:
                    print(
                        f"WARN: default-aigateway reconcile during MaaS wait: {exc}",
                        file=sys.stderr,
                        flush=True,
                    )

        if now >= grace_deadline:
            func_ready, func_reason = maas_functional_smoke_ready()
            if func_ready and ready_status == "True" and (
                maas_status == "True"
                or (not require_prereq or prereq_status == "True")
            ):
                print(
                    "WARN: accepting MaaS smoke after grace period "
                    f"({grace_sec}s) — functional ready"
                    + (
                        f" but DSC MaaSPrerequisites still {(prereq_status or '?')}: "
                        f"{(prereq_msg or func_reason)[:120]}"
                        if require_prereq and prereq_status != "True" and maas_status == "True"
                        else (
                            f" ({maas_ready_type} lagging: "
                            f"{(maas_msg or func_reason or 'reconciling')[:120]})"
                            if maas_status != "True"
                            else ""
                        )
                    ),
                    file=sys.stderr,
                    flush=True,
                )
                return

        if int(time.time()) % 60 < 12:
            prereq_display = prereq_status if require_prereq else "n/a"
            print(
                f"Waiting for MaaS smoke readiness "
                f"(MaaSPrerequisites={prereq_display or '?'} ModelsAsService={maas_status or '?'} "
                f"DSC Ready={ready_status or '?'}): "
                f"{(prereq_msg or maas_msg or 'reconciling...')[:120]}",
                flush=True,
            )
        time.sleep(12)

    acceptable, accept_reason = maas_smoke_acceptable_for_run()
    if acceptable:
        print(f"✓ MaaS component prerequisites ready at timeout boundary", flush=True)
        return

    prereq_status, _, prereq_msg = _dsc_condition("MaaSPrerequisitesAvailable")
    maas_status, _, maas_msg = _dsc_condition(maas_ready_type)
    ready_status, _, ready_msg = _dsc_condition("Ready")
    prereq_display = prereq_status if require_prereq else "n/a"
    raise RuntimeError(
        "MaaS component prerequisites not ready after "
        f"{timeout_sec}s "
        f"(MaaSPrerequisites={prereq_display}, ModelsAsService={maas_status}, "
        f"DSC Ready={ready_status}) — "
        f"{(prereq_msg or maas_msg or ready_msg or accept_reason or 'reconcile incomplete')[:300]}"
    )


def _wait_for_dsc_component_ready(*, condition_type: str, timeout_sec: int) -> None:
    deadline = time.time() + timeout_sec
    while time.time() < deadline:
        status, reason, msg = _dsc_condition(condition_type)
        if status == "True":
            print(f"✓ DataScienceCluster/default-dsc {condition_type}=True", flush=True)
            return
        if reason == "Removed":
            raise RuntimeError(f"DSC component disabled ({condition_type} reason=Removed)")
        if int(time.time()) % 60 < 12:
            print(
                f"Waiting for DSC {condition_type} "
                f"(status={status or '?'} reason={reason or '?'}): "
                f"{(msg or 'reconciling...')[:120]}",
                flush=True,
            )
        time.sleep(12)
    status, reason, msg = _dsc_condition(condition_type)
    raise RuntimeError(
        f"DSC {condition_type} not ready after {timeout_sec}s "
        f"(status={status or '?'}, reason={reason or '?'}): "
        f"{(msg or 'reconcile incomplete')[:300]}"
    )


def _wait_for_dsc_ready(*, timeout_sec: int) -> None:
    deadline = time.time() + timeout_sec
    while time.time() < deadline:
        r = oc_run(
            [
                "get",
                "datasciencecluster",
                "default-dsc",
                "-o",
                "jsonpath={.status.conditions[?(@.type=='Ready')].status}",
            ],
            check=False,
            capture_output=True,
            timeout=30,
        )
        if (r.stdout or "").strip() == "True":
            print("✓ DataScienceCluster/default-dsc Ready", flush=True)
            return
        time.sleep(10)
    print(
        "WARN: DataScienceCluster/default-dsc not Ready after "
        f"{timeout_sec}s — continuing smoke (tests may fail)",
        file=sys.stderr,
    )


_DASHBOARD_DEPLOY_NAMES = ("rhods-dashboard", "odh-dashboard")


def _dashboard_deploy_available() -> bool:
    """True when the RHOAI/ODH dashboard Deployment is Available."""
    for name in _DASHBOARD_DEPLOY_NAMES:
        r = oc_run(
            [
                "get",
                "deploy",
                name,
                "-n",
                _MAAS_APPS_NS,
                "-o",
                "jsonpath={.status.conditions[?(@.type=='Available')].status}",
            ],
            check=False,
            capture_output=True,
            timeout=30,
        )
        if (r.stdout or "").strip() == "True":
            return True
    return False


def _dsc_and_dashboard_ready_for_bvt() -> tuple[bool, str]:
    """Ready for operator_health BVT: DSC Ready, DashboardReady when present, deploy Available."""
    ready, reason, msg = _dsc_condition("Ready")
    if ready != "True":
        return False, f"Ready={ready or '?'} reason={reason or '?'} {(msg or '')[:120]}"
    types = _dsc_condition_types()
    if "DashboardReady" in types:
        dstatus, dreason, dmsg = _dsc_condition("DashboardReady")
        if dstatus != "True":
            return False, (
                f"DashboardReady={dstatus or '?'} reason={dreason or '?'} {(dmsg or '')[:120]}"
            )
    if not _dashboard_deploy_available():
        return False, f"dashboard Deployment not Available in {_MAAS_APPS_NS}"
    return True, "Ready+DashboardReady"


def require_dsc_ready_for_bvt(*, timeout_sec: int) -> None:
    """Block operator_health BVT until DSC Ready and dashboard stay healthy (pytest wait is 120s)."""
    from components.maas_billing.timeouts import bvt_dsc_ready_settle_sec

    settle_sec = max(0, bvt_dsc_ready_settle_sec())
    deadline = time.time() + timeout_sec
    stable_since: float | None = None
    last_detail = "reconciling..."
    while time.time() < deadline:
        ok, detail = _dsc_and_dashboard_ready_for_bvt()
        last_detail = detail
        if ok:
            if settle_sec <= 0:
                print(f"✓ DataScienceCluster/default-dsc {detail} (BVT gate)", flush=True)
                return
            if stable_since is None:
                stable_since = time.time()
                print(
                    f"✓ DataScienceCluster/default-dsc {detail}; "
                    f"settling {settle_sec}s before operator_health BVT",
                    flush=True,
                )
            elif time.time() - stable_since >= settle_sec:
                print(
                    f"✓ DataScienceCluster/default-dsc {detail} stable (BVT gate)",
                    flush=True,
                )
                return
        else:
            stable_since = None
            if int(time.time()) % 60 < 12:
                print(
                    f"Waiting for DSC+dashboard before operator_health BVT: {detail}",
                    flush=True,
                )
        time.sleep(10)
    raise RuntimeError(
        f"DSC/dashboard not Ready after {timeout_sec}s before operator_health BVT: {last_detail[:300]}"
    )
