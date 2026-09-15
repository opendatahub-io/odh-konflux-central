"""MaaS cluster-prep wait timeouts (env overrides for Tekton)."""

from __future__ import annotations

import os

_DEFAULT_MAAS_PREP_TIMEOUT_SEC = 900
_DEFAULT_MAAS_RESYNC_TIMEOUT_SEC = 300
_DEFAULT_MAAS_DSC_PREREQ_GRACE_SEC = 120
_DEFAULT_MAAS_GATEWAY_PROGRAMMED_WAIT_SEC = 600
_DEFAULT_MAAS_GATEWAY_HTTPS_WAIT_SEC = 120
_DEFAULT_MAAS_GATEWAY_PREP_PROGRAMMED_WAIT_SEC = 300
_DEFAULT_MAAS_GATEWAY_PREP_PROGRAMMED_WAIT_SEC_EHC = 480


def maas_prep_timeout_sec() -> int:
    return int(os.environ.get("MAAS_PREP_TIMEOUT_SEC", str(_DEFAULT_MAAS_PREP_TIMEOUT_SEC)))


def maas_resync_timeout_sec() -> int:
    return int(os.environ.get("MAAS_RESYNC_TIMEOUT_SEC", str(_DEFAULT_MAAS_RESYNC_TIMEOUT_SEC)))


def maas_dsc_prereq_grace_sec() -> int:
    return int(os.environ.get("MAAS_DSC_PREREQ_GRACE_SEC", str(_DEFAULT_MAAS_DSC_PREREQ_GRACE_SEC)))


def maas_gateway_programmed_wait_sec() -> int:
    return int(
        os.environ.get(
            "MAAS_GATEWAY_PROGRAMMED_WAIT_SEC",
            str(_DEFAULT_MAAS_GATEWAY_PROGRAMMED_WAIT_SEC),
        )
    )


def maas_gateway_https_wait_sec() -> int:
    """Bounded wait for gateway-owned HTTPS service before enabling modelsAsService."""
    return int(
        os.environ.get(
            "MAAS_GATEWAY_HTTPS_WAIT_SEC",
            str(_DEFAULT_MAAS_GATEWAY_HTTPS_WAIT_SEC),
        )
    )


def maas_gateway_prep_programmed_wait_sec() -> int:
    """Wait for Gateway Programmed before HTTPS/modelsAsService (EPHC reconciles slowly)."""
    from install.gateway_config import cluster_source_is_ephc

    default = (
        _DEFAULT_MAAS_GATEWAY_PREP_PROGRAMMED_WAIT_SEC_EHC
        if cluster_source_is_ephc()
        else _DEFAULT_MAAS_GATEWAY_PREP_PROGRAMMED_WAIT_SEC
    )
    return int(
        os.environ.get(
            "MAAS_GATEWAY_PREP_PROGRAMMED_WAIT_SEC",
            str(default),
        )
    )


def bvt_dsc_ready_timeout_sec() -> int:
    """Wait for DSC Ready+DashboardReady before operator_health BVT (pytest only allows 120s)."""
    return int(os.environ.get("BVT_DSC_READY_TIMEOUT_SEC", "900"))


def bvt_dsc_ready_settle_sec() -> int:
    """Require Ready+DashboardReady to hold this long so a dashboard rollout cannot race pytest."""
    return int(os.environ.get("BVT_DSC_READY_SETTLE_SEC", "45"))


def bvt_cluster_nodes_timeout_sec() -> int:
    """Wait for all nodes schedulable before cluster_health BVT."""
    return int(os.environ.get("BVT_CLUSTER_NODES_TIMEOUT_SEC", "600"))


def component_cluster_nodes_timeout_sec() -> int:
    """Wait for schedulable nodes before each component pytest (mid-smoke re-check)."""
    base = bvt_cluster_nodes_timeout_sec()
    from suite.its_trigger_params import is_ephemeral_hosted_cluster_source

    source = os.environ.get("CLUSTER_SOURCE", "").strip()
    if not is_ephemeral_hosted_cluster_source(source):
        return base
    cap_raw = os.environ.get("OLMINSTALL_EPHC_CLUSTER_NODES_WAIT_SEC", "120").strip()
    try:
        cap = int(cap_raw)
    except ValueError:
        cap = 120
    if cap <= 0:
        return base
    return min(base, cap)
