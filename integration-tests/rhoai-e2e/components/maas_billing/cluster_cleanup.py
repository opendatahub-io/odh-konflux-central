"""Remove leaked MaaS smoke RBAC so opendatahub-tests fixtures can recreate groups/IDPs."""

from __future__ import annotations

import json
import re

from install.dsc_install import oc_run

from components.maas_billing.common import (
    _GATEWAY_NAME,
    _GATEWAY_NS,
    _MAAS_APPS_NS,
    _MAAS_AUTH_POLICY,
    maas_api_namespace,
)

_MAAS_GATEWAY_AUTH_POLICY = "maas-gateway-auth"
_GATEWAY_DEFAULT_AUTH_POLICY = "gateway-default-auth"
_AITENANT_CRD_PLURAL = "aitenants"
_E2E_GATEWAY_NAME_RE = re.compile(r"^e2e-aigw-")

_MAAS_SMOKE_GROUPS = ("tier-free-users", "tier-premium-users")
_MAAS_HTPASSWD_SECRET_RE = re.compile(r"^maas-htpasswd-secret-[0-9a-f]+$")
_MAAS_HTPASSWD_IDP_RE = re.compile(r"^maas-htpasswd-idp-[0-9a-f]+$")
_OAUTH_NS = "openshift-config"
_OAUTH_NAME = "cluster"


def _delete_maas_smoke_groups() -> None:
    for group in _MAAS_SMOKE_GROUPS:
        deleted = oc_run(
            ["delete", "group", group, "--ignore-not-found"],
            check=False,
            capture_output=True,
            timeout=30,
        )
        if deleted.returncode == 0 and "deleted" in (deleted.stdout or "").lower():
            print(f"✓ Removed stale MaaS smoke group {group}", flush=True)


def _delete_maas_htpasswd_secrets() -> None:
    listed = oc_run(
        ["get", "secret", "-n", _OAUTH_NS, "-o", "jsonpath={.items[*].metadata.name}"],
        check=False,
        capture_output=True,
        timeout=30,
    )
    if listed.returncode != 0:
        return
    for name in (listed.stdout or "").split():
        if _MAAS_HTPASSWD_SECRET_RE.match(name):
            oc_run(
                ["delete", "secret", name, "-n", _OAUTH_NS, "--ignore-not-found"],
                check=False,
                capture_output=True,
                timeout=30,
            )
            print(f"✓ Removed stale MaaS htpasswd secret {_OAUTH_NS}/{name}", flush=True)


def _prune_maas_htpasswd_oauth_idps() -> None:
    current = oc_run(
        ["get", "oauth", _OAUTH_NAME, "-o", "json"],
        check=False,
        capture_output=True,
        timeout=30,
    )
    if current.returncode != 0:
        return
    oauth = json.loads(current.stdout or "{}")
    providers = oauth.get("spec", {}).get("identityProviders") or []
    kept = [p for p in providers if not _MAAS_HTPASSWD_IDP_RE.match(str(p.get("name", "")))]
    if len(kept) == len(providers):
        return
    oauth["spec"]["identityProviders"] = kept
    apply = oc_run(
        ["replace", "-f", "-"],
        stdin_text=json.dumps(oauth),
        check=False,
        capture_output=True,
        timeout=60,
    )
    if apply.returncode != 0:
        err = (apply.stderr or apply.stdout or "").strip()
        print(f"WARN: could not prune stale MaaS OAuth IDPs: {err[:200]}", flush=True)
        return
    print("✓ Pruned stale MaaS htpasswd OAuth identity providers", flush=True)


def _gateway_auth_has_api_key_validation(policy: dict) -> bool:
    try:
        url = (
            policy["spec"]["defaults"]["rules"]["metadata"]["apiKeyValidation"]["http"]["url"]
        )
    except (KeyError, TypeError):
        return False
    expected = (
        f"https://maas-api.{maas_api_namespace()}.svc.cluster.local:8443/internal/v1/api-keys/validate"
    )
    return bool(url) and url == expected


def ensure_maas_gateway_auth_policy_alias() -> None:
    """Promote maas-api auth rules to openshift-ingress/maas-gateway-auth on EA.x clusters."""
    api = oc_run(
        ["get", "authpolicy", _MAAS_AUTH_POLICY, "-n", _MAAS_APPS_NS, "-o", "json"],
        check=False,
        capture_output=True,
        timeout=30,
    )
    if api.returncode != 0:
        raise RuntimeError(
            f"Cannot build {_GATEWAY_NS}/{_MAAS_GATEWAY_AUTH_POLICY}: "
            f"{_MAAS_APPS_NS}/{_MAAS_AUTH_POLICY} missing"
        )

    api_policy = json.loads(api.stdout or "{}")
    api_rules = api_policy.get("spec", {}).get("rules")
    if not isinstance(api_rules, dict):
        raise RuntimeError(
            f"Cannot build {_GATEWAY_NS}/{_MAAS_GATEWAY_AUTH_POLICY}: "
            f"{_MAAS_APPS_NS}/{_MAAS_AUTH_POLICY} has no spec.rules"
        )

    gateway_when = api_policy.get("spec", {}).get("when") or [
        {
            "predicate": 'request.path != "/maas-api/health" || request.method != "GET"',
        }
    ]
    desired_spec = {
        "targetRef": {
            "group": "gateway.networking.k8s.io",
            "kind": "Gateway",
            "name": _GATEWAY_NAME,
        },
        "defaults": {
            "when": gateway_when,
            "strategy": "atomic",
            "rules": api_rules,
        },
    }

    current = oc_run(
        [
            "get",
            "authpolicy",
            _MAAS_GATEWAY_AUTH_POLICY,
            "-n",
            _GATEWAY_NS,
            "-o",
            "json",
        ],
        check=False,
        capture_output=True,
        timeout=30,
    )
    if current.returncode == 0:
        gateway_policy = json.loads(current.stdout or "{}")
        if _gateway_auth_has_api_key_validation(gateway_policy):
            print(
                f"✓ MaaS gateway AuthPolicy {_GATEWAY_NS}/{_MAAS_GATEWAY_AUTH_POLICY} "
                "has apiKeyValidation callback",
                flush=True,
            )
            return
        gateway_policy["spec"] = desired_spec
        apply = oc_run(
            ["apply", "-f", "-"],
            stdin_text=json.dumps(gateway_policy),
            check=False,
            capture_output=True,
            timeout=60,
        )
    else:
        gateway_policy = {
            "apiVersion": "kuadrant.io/v1",
            "kind": "AuthPolicy",
            "metadata": {
                "name": _MAAS_GATEWAY_AUTH_POLICY,
                "namespace": _GATEWAY_NS,
                "labels": {
                    "app.kubernetes.io/managed-by": "rhoai-e2e",
                    "app.kubernetes.io/part-of": "maas-gateway-auth",
                },
            },
            "spec": desired_spec,
        }
        apply = oc_run(
            ["apply", "-f", "-"],
            stdin_text=json.dumps(gateway_policy),
            check=False,
            capture_output=True,
            timeout=60,
        )

    if apply.returncode != 0:
        err = (apply.stderr or apply.stdout or "").strip()
        raise RuntimeError(
            f"Could not apply {_GATEWAY_NS}/{_MAAS_GATEWAY_AUTH_POLICY}: {err or 'unknown error'}"
        )
    oc_run(
        [
            "delete",
            "authpolicy",
            _GATEWAY_DEFAULT_AUTH_POLICY,
            "-n",
            _GATEWAY_NS,
            "--ignore-not-found",
        ],
        check=False,
        capture_output=True,
        timeout=30,
    )
    print(
        f"✓ Applied MaaS gateway AuthPolicy {_GATEWAY_NS}/{_MAAS_GATEWAY_AUTH_POLICY} "
        "from maas-api auth rules",
        flush=True,
    )


def _prune_stale_maas_aitenants() -> None:
    """Remove leaked AITenant CRs that pin a stale e2e gateway from prior maas_billing runs."""
    listed = oc_run(
        ["get", _AITENANT_CRD_PLURAL, "-A", "-o", "json"],
        check=False,
        capture_output=True,
        timeout=60,
    )
    if listed.returncode != 0:
        return
    try:
        doc = json.loads(listed.stdout or "{}")
    except json.JSONDecodeError:
        return
    for item in doc.get("items") or []:
        meta = item.get("metadata") or {}
        name = str(meta.get("name") or "").strip()
        namespace = str(meta.get("namespace") or "").strip()
        if not name or not namespace:
            continue
        gateway_ref = ((item.get("spec") or {}).get("gatewayRef") or {}).get("name") or ""
        if str(gateway_ref) == _GATEWAY_NAME:
            continue
        if not _E2E_GATEWAY_NAME_RE.match(str(gateway_ref)):
            continue
        oc_run(
            ["delete", _AITENANT_CRD_PLURAL, name, "-n", namespace, "--ignore-not-found"],
            check=False,
            capture_output=True,
            timeout=30,
        )
        print(
            f"✓ Removed stale MaaS AITenant {namespace}/{name} (gatewayRef={gateway_ref})",
            flush=True,
        )


def _prune_stale_maas_e2e_gateways() -> None:
    """Delete leftover e2e-aigw-* Gateways from prior maas_billing pytest (not maas-default-gateway)."""
    listed = oc_run(
        ["get", "gateway", "-A", "-o", "json"],
        check=False,
        capture_output=True,
        timeout=60,
    )
    if listed.returncode != 0:
        return
    try:
        doc = json.loads(listed.stdout or "{}")
    except json.JSONDecodeError:
        return
    for item in doc.get("items") or []:
        meta = item.get("metadata") or {}
        name = str(meta.get("name") or "").strip()
        namespace = str(meta.get("namespace") or "").strip()
        if not name or not namespace:
            continue
        if namespace == _GATEWAY_NS and name == _GATEWAY_NAME:
            continue
        if not _E2E_GATEWAY_NAME_RE.match(name):
            continue
        oc_run(
            ["delete", "gateway", name, "-n", namespace, "--ignore-not-found"],
            check=False,
            capture_output=True,
            timeout=30,
        )
        print(f"✓ Removed stale MaaS e2e gateway {namespace}/{name}", flush=True)


def cleanup_maas_smoke_stale_gateway_leaks() -> None:
    """Remove leaked e2e-aigw gateways and AITenants from prior maas_billing pytest runs."""
    _prune_stale_maas_e2e_gateways()
    _prune_stale_maas_aitenants()


def cleanup_maas_smoke_leaked_rbac() -> None:
    """Delete groups, htpasswd IDP state from prior maas_billing runs."""
    _delete_maas_smoke_groups()
    _delete_maas_htpasswd_secrets()
    _prune_maas_htpasswd_oauth_idps()
