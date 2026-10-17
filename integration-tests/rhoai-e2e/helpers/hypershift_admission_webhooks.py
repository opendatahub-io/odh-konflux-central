"""Neutralize broken HyperShift guest admission webhooks.

HyperShift sometimes projects Validating/MutatingWebhookConfigurations into the
guest cluster with ``clientConfig.service.name=xxx-invalid-service-xxx`` (or another
missing Service). Those fail closed and block Deployment/Service creates — breaking
OGX fixtures and OpenShift Gateway controller reconciliation on EPHC.
"""

from __future__ import annotations

import json
import sys
from typing import Any

from install.dsc_install import oc_run

_STUB_SERVICE_NAMES = frozenset({"xxx-invalid-service-xxx"})
_WEBHOOK_KINDS = (
    "validatingwebhookconfiguration",
    "mutatingwebhookconfiguration",
)


def _webhook_service(webhook: dict[str, Any]) -> tuple[str, str]:
    client = webhook.get("clientConfig") or {}
    service = client.get("service") or {}
    name = str(service.get("name") or "").strip()
    namespace = str(service.get("namespace") or "default").strip() or "default"
    return name, namespace


def _service_name(webhook: dict[str, Any]) -> str:
    return _webhook_service(webhook)[0]


def _oc_output_is_not_found(output: str) -> bool:
    low = output.lower()
    return "notfound" in low.replace(" ", "") or "not found" in low


def _service_probe(*, name: str, namespace: str) -> bool | None:
    """Return True if Service exists, False if NotFound, None if probe failed."""
    if not name or not namespace:
        return False
    r = oc_run(
        ["get", "svc", name, "-n", namespace, "-o", "name"],
        check=False,
        capture_output=True,
        timeout=20,
    )
    if r.returncode == 0:
        return True
    if _oc_output_is_not_found(f"{r.stderr or ''}{r.stdout or ''}"):
        return False
    print(
        f"WARN: could not probe svc/{name} -n {namespace} "
        f"(exit {r.returncode}); not treating webhook as broken",
        file=sys.stderr,
        flush=True,
    )
    return None


def _webhook_is_broken(webhook: dict[str, Any]) -> bool:
    name, namespace = _webhook_service(webhook)
    if not name:
        return False
    if name in _STUB_SERVICE_NAMES:
        return True
    exists = _service_probe(name=name, namespace=namespace)
    return exists is False


def _list_webhook_configs(kind: str) -> list[dict[str, Any]]:
    r = oc_run(
        ["get", kind, "-o", "json"],
        check=False,
        capture_output=True,
        timeout=60,
    )
    if r.returncode != 0:
        return []
    try:
        doc = json.loads(r.stdout or "{}")
    except json.JSONDecodeError:
        return []
    items = doc.get("items") or []
    return [i for i in items if isinstance(i, dict)]


def _delete_webhook_config(*, kind: str, name: str) -> bool:
    r = oc_run(
        ["delete", kind, name, "--ignore-not-found"],
        check=False,
        capture_output=True,
        timeout=60,
    )
    if r.returncode != 0:
        err = (r.stderr or r.stdout or "").strip()
        print(
            f"WARN: could not delete broken {kind}/{name}: {err[:200]}",
            file=sys.stderr,
            flush=True,
        )
        return False
    print(f"✓ Removed broken HyperShift admission {kind}/{name}", flush=True)
    return True


def _hypershift_webhook_cleanup_enabled() -> bool:
    from install.rosa_hcp_pull_setup import is_hypershift_managed_cluster
    from install.gateway_config import cluster_source_is_ephc

    return cluster_source_is_ephc() or is_hypershift_managed_cluster()


def neutralize_broken_hypershift_admission_webhooks() -> int:
    """Delete guest webhook configs whose clientConfig Service is a stub or missing.

    Returns the number of webhook configs removed.
    """
    if not _hypershift_webhook_cleanup_enabled():
        return 0

    removed = 0
    for kind in _WEBHOOK_KINDS:
        for cfg in _list_webhook_configs(kind):
            meta = cfg.get("metadata") or {}
            name = str(meta.get("name") or "").strip()
            if not name:
                continue
            webhooks = cfg.get("webhooks") or []
            if not isinstance(webhooks, list) or not webhooks:
                continue
            if any(
                isinstance(wh, dict) and _webhook_is_broken(wh) for wh in webhooks
            ):
                # One broken webhook in the list fails closed for the whole config.
                if _delete_webhook_config(kind=kind, name=name):
                    removed += 1
    if removed:
        print(
            f"✓ Neutralized {removed} broken HyperShift admission webhook config(s)",
            flush=True,
        )
    return removed


def broken_hypershift_admission_webhook_reason(*, probe: bool = True) -> str:
    """Return infra reason when a stub/missing HyperShift admission webhook remains."""
    if not probe or not _hypershift_webhook_cleanup_enabled():
        return ""
    for kind in _WEBHOOK_KINDS:
        for cfg in _list_webhook_configs(kind):
            meta = cfg.get("metadata") or {}
            name = str(meta.get("name") or "").strip() or "?"
            for wh in cfg.get("webhooks") or []:
                if not isinstance(wh, dict) or not _webhook_is_broken(wh):
                    continue
                svc = _service_name(wh) or "missing-service"
                return (
                    f"broken HyperShift admission webhook: {kind}/{name} → "
                    f"service {svc!r}"
                )
    return ""
