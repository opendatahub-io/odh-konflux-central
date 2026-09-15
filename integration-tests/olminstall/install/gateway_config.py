"""RHOAI GatewayConfig OIDC + readiness (Jenkins Verify RHODS / Patch GatewayConfig)."""

from __future__ import annotations

import base64
import json
import os
import sys
import time

from install.dsc_install import oc_run
from install.ldap import _cluster_is_byoidc
from suite.its_trigger_params import is_ephemeral_hosted_cluster_source

_GATEWAY_NAME = "default-gateway"
_OIDC_SECRET_NAME = "keycloak-client-secret"
_OIDC_SECRET_NS = "openshift-ingress"
_OIDC_SECRET_KEY = "clientSecret"
_DEFAULT_OIDC_CLIENT_ID = "odh-client"
_AUTH_NAME = "auth"
_READY_CONDITIONS = ("Ready", "ProvisioningSucceeded", "GatewayConfigReady")
_SERVICEMESH_CSV_PREFIX = "servicemeshoperator"
_KUBE_AUTH_PROXY_NS = "openshift-ingress"
_KUBE_AUTH_PROXY_DEPLOY = "kube-auth-proxy"
_KUBE_AUTH_PROXY_CREDS = "kube-auth-proxy-creds"
_OPENSHIFT_GATEWAY_ISTIO_NAME = "openshift-gateway"
_OPENSHIFT_GATEWAY_NS = "openshift-ingress"
_OPENSHIFT_GATEWAY_ISTIOD = "istiod-openshift-gateway"
_ISTIO_EOL_MARKERS = ("end-of-life", "end of life")
_DEFAULT_OPENSHIFT_GATEWAY_ISTIO_WAIT_SEC = 600


def cluster_source_is_ephc() -> bool:
    """True on an Ephemeral Hosted Cluster in OpenShift CI (Prow)."""
    return is_ephemeral_hosted_cluster_source(os.environ.get("CLUSTER_SOURCE", ""))


def gateway_oidc_configured() -> bool:
    """True when GatewayConfig spec.oidc has a usable issuer and client ID."""
    doc = _gateway_config_doc()
    if not doc:
        return False
    existing = ((doc.get("spec") or {}).get("oidc") or {})
    issuer = str(existing.get("issuerURL") or "").strip()
    client_id = str(existing.get("clientID") or "").strip()
    return bool(issuer) and bool(client_id) and not _malformed_oidc_client_id(client_id)


def _cluster_has_oidc_provider() -> bool | None:
    """True when cluster Authentication lists at least one OIDC provider. None if probe failed."""
    r = oc_run(
        [
            "get",
            "authentication",
            "cluster",
            "-o",
            "jsonpath={.spec.oidcProviders}",
        ],
        check=False,
        capture_output=True,
        timeout=30,
    )
    if r.returncode != 0:
        err = (r.stderr or r.stdout or "").strip()
        print(
            f"WARN: could not read Authentication/cluster oidcProviders: {err or 'unknown error'}",
            file=sys.stderr,
            flush=True,
        )
        return None
    raw = (r.stdout or "").strip()
    return bool(raw) and raw not in ("[]", "null")


def _wait_for_byoidc_cluster_signals(*, retries: int = 24, delay_sec: float = 15.0) -> bool:
    """EPHC may expose ``oidc/byoidc-credentials`` after gateway CR is Ready."""
    for attempt in range(retries):
        if _cluster_is_byoidc():
            return True
        if attempt + 1 < retries:
            time.sleep(delay_sec)
    return _cluster_is_byoidc()


def _resolve_byoidc_for_gateway() -> bool:
    if _cluster_is_byoidc():
        return True
    if not cluster_source_is_ephc():
        return False
    from install.ldap import _byoidc_credentials_ready

    has_provider = _cluster_has_oidc_provider()
    if not _byoidc_credentials_ready() and has_provider is False:
        return False
    print(
        "EPHC cluster: waiting for BYOIDC issuer/credentials before GatewayConfig OIDC patch...",
        flush=True,
    )
    return _wait_for_byoidc_cluster_signals()


def _byoidc_issuer_url() -> str:
    r = oc_run(
        [
            "get",
            "authentication",
            "cluster",
            "-o",
            "jsonpath={.spec.oidcProviders[0].issuer.issuerURL}",
        ],
        check=False,
        capture_output=True,
        timeout=30,
    )
    if r.returncode != 0:
        return ""
    return (r.stdout or "").strip()


def _gateway_oidc_audiences() -> list[str]:
    """Return individual OIDC audience strings from cluster Authentication."""
    r = oc_run(
        [
            "get",
            "authentication",
            "cluster",
            "-o",
            "jsonpath={.spec.oidcProviders[0].issuer.audiences[*]}",
        ],
        check=False,
        capture_output=True,
        timeout=30,
    )
    if r.returncode != 0:
        return []
    return [a for a in (r.stdout or "").split() if a]


def _gateway_oidc_client_id() -> str:
    env_id = os.environ.get("GATEWAY_OIDC_CLIENT_ID", "").strip()
    if env_id:
        return env_id
    audiences = _gateway_oidc_audiences()
    for audience in audiences:
        if audience == _DEFAULT_OIDC_CLIENT_ID:
            return _DEFAULT_OIDC_CLIENT_ID
    if audiences:
        if _cluster_is_byoidc():
            print(
                f"Using BYOIDC audience {audiences[0]!r} for GatewayConfig clientID",
                flush=True,
            )
            return audiences[0]
        return _DEFAULT_OIDC_CLIENT_ID
    if _cluster_is_byoidc():
        raise RuntimeError(
            "BYOIDC cluster has no OIDC audiences on Authentication; "
            "set GATEWAY_OIDC_CLIENT_ID to the issuer client ID."
        )
    return _DEFAULT_OIDC_CLIENT_ID


def _malformed_oidc_client_id(client_id: str) -> bool:
    cid = (client_id or "").strip()
    return not cid or any(ch in cid for ch in ('[', ']', ',', '"'))


def _kube_auth_proxy_client_id() -> str:
    r = oc_run(
        [
            "get",
            "secret",
            _KUBE_AUTH_PROXY_CREDS,
            "-n",
            _KUBE_AUTH_PROXY_NS,
            "-o",
            "jsonpath={.data.OAUTH2_PROXY_CLIENT_ID}",
        ],
        check=False,
        capture_output=True,
        timeout=30,
    )
    if r.returncode != 0 or not (r.stdout or "").strip():
        return ""
    try:
        return base64.b64decode((r.stdout or "").strip()).decode("utf-8")
    except (ValueError, UnicodeDecodeError):
        return ""


def _rollout_restart_kube_auth_proxy(*, timeout_sec: int = 180) -> None:
    r = oc_run(
        [
            "rollout",
            "restart",
            f"deployment/{_KUBE_AUTH_PROXY_DEPLOY}",
            "-n",
            _KUBE_AUTH_PROXY_NS,
        ],
        check=False,
        capture_output=True,
        timeout=60,
    )
    if r.returncode != 0:
        err = (r.stderr or r.stdout or "").strip()
        print(f"WARN: kube-auth-proxy rollout restart failed: {err}", file=sys.stderr)
        return
    oc_run(
        [
            "rollout",
            "status",
            f"deployment/{_KUBE_AUTH_PROXY_DEPLOY}",
            "-n",
            _KUBE_AUTH_PROXY_NS,
            f"--timeout={timeout_sec}s",
        ],
        check=False,
        capture_output=True,
        timeout=timeout_sec + 30,
    )
    print(f"✓ Restarted deployment/{_KUBE_AUTH_PROXY_DEPLOY} in {_KUBE_AUTH_PROXY_NS}", flush=True)


def sync_kube_auth_proxy_oidc_client(client_id: str) -> bool:
    """Align kube-auth-proxy OAuth client ID with GatewayConfig (operator may leave a JSON array)."""
    if not _cluster_is_byoidc():
        return False
    current = _kube_auth_proxy_client_id()
    if current == client_id and not _malformed_oidc_client_id(current):
        return False
    patch_doc = {"stringData": {"OAUTH2_PROXY_CLIENT_ID": client_id}}
    r = oc_run(
        [
            "patch",
            "secret",
            _KUBE_AUTH_PROXY_CREDS,
            "-n",
            _KUBE_AUTH_PROXY_NS,
            "--type=merge",
            "-p",
            json.dumps(patch_doc),
        ],
        check=False,
        capture_output=True,
        timeout=60,
    )
    if r.returncode != 0:
        err = (r.stderr or r.stdout or "").strip()
        raise RuntimeError(f"kube-auth-proxy client ID patch failed: {err or 'unknown error'}")
    print(
        f"✓ Patched {_KUBE_AUTH_PROXY_CREDS} OAUTH2_PROXY_CLIENT_ID={client_id!r}",
        flush=True,
    )
    _rollout_restart_kube_auth_proxy()
    return True


def _gateway_config_doc() -> dict | None:
    r = oc_run(
        ["get", "gatewayconfig", _GATEWAY_NAME, "-o", "json"],
        check=False,
        capture_output=True,
        timeout=30,
    )
    if r.returncode != 0 or not (r.stdout or "").strip():
        return None
    try:
        doc = json.loads(r.stdout)
    except json.JSONDecodeError:
        return None
    return doc if isinstance(doc, dict) else None


def _condition_status(doc: dict, condition_type: str) -> str:
    for item in (doc.get("status") or {}).get("conditions") or []:
        if isinstance(item, dict) and item.get("type") == condition_type:
            return str(item.get("status") or "")
    return ""


def gateway_config_ready() -> bool:
    doc = _gateway_config_doc()
    if not doc:
        return False
    return all(_condition_status(doc, name) == "True" for name in _READY_CONDITIONS)


def patch_gateway_config_oidc() -> bool:
    """Patch cluster GatewayConfig with external OIDC settings on BYOIDC clusters."""
    if not _resolve_byoidc_for_gateway():
        print("✓ Cluster not BYOIDC — skipping GatewayConfig OIDC patch", flush=True)
        return False
    issuer = _byoidc_issuer_url()
    if not issuer:
        print("WARN: BYOIDC cluster but issuer URL missing; skip GatewayConfig OIDC", file=sys.stderr)
        return False

    doc = _gateway_config_doc()
    existing = ((doc or {}).get("spec") or {}).get("oidc") or {}
    client_id = _gateway_oidc_client_id()
    existing_client_id = str(existing.get("clientID") or "")
    gateway_ok = (
        existing.get("issuerURL") == issuer
        and existing_client_id == client_id
        and not _malformed_oidc_client_id(existing_client_id)
        and (existing.get("clientSecretRef") or {}).get("name") == _OIDC_SECRET_NAME
    )
    changed = False
    if gateway_ok:
        print("✓ GatewayConfig OIDC already configured", flush=True)
    else:
        patch_doc = {
            "spec": {
                "oidc": {
                    "issuerURL": issuer,
                    "clientID": client_id,
                    "clientSecretRef": {
                        "name": _OIDC_SECRET_NAME,
                        "key": _OIDC_SECRET_KEY,
                        "namespace": _OIDC_SECRET_NS,
                    },
                }
            }
        }
        r = oc_run(
            [
                "patch",
                "gatewayconfig",
                _GATEWAY_NAME,
                "--type=merge",
                "-p",
                json.dumps(patch_doc),
            ],
            check=False,
            capture_output=True,
            timeout=60,
        )
        if r.returncode != 0:
            err = (r.stderr or r.stdout or "").strip()
            raise RuntimeError(f"GatewayConfig OIDC patch failed: {err or 'unknown error'}")
        print(
            f"✓ Patched GatewayConfig/{_GATEWAY_NAME} OIDC (issuer={issuer}, clientID={client_id})",
            flush=True,
        )
        changed = True
    if sync_kube_auth_proxy_oidc_client(client_id):
        changed = True
    return changed


def configure_auth_cr_groups() -> bool:
    """Ensure Auth CR allows authenticated OIDC users (Jenkins Configure OIDC Auth CR Groups)."""
    r = oc_run(["get", "auth", _AUTH_NAME, "-o", "json"], check=False, capture_output=True, timeout=30)
    if r.returncode != 0:
        print(f"WARN: Auth/{_AUTH_NAME} not found; skip group patch", file=sys.stderr)
        return False
    try:
        doc = json.loads(r.stdout or "{}")
    except json.JSONDecodeError:
        return False
    spec = doc.get("spec") or {}
    allowed = list(spec.get("allowedGroups") or [])
    admin = list(spec.get("adminGroups") or [])
    changed = False
    if "system:authenticated" not in allowed:
        allowed.append("system:authenticated")
        changed = True
    if "rhods-admins" not in admin:
        admin.append("rhods-admins")
        changed = True
    extra_allowed = os.environ.get("GATEWAY_AUTH_ALLOWED_GROUPS", "").strip()
    if extra_allowed:
        for group in (g.strip() for g in extra_allowed.split(",") if g.strip()):
            if group not in allowed:
                allowed.append(group)
                changed = True
    if not changed:
        return False
    patch_doc = {"spec": {"allowedGroups": allowed, "adminGroups": admin}}
    pr = oc_run(
        ["patch", "auth", _AUTH_NAME, "--type=merge", "-p", json.dumps(patch_doc)],
        check=False,
        capture_output=True,
        timeout=60,
    )
    if pr.returncode != 0:
        err = (pr.stderr or pr.stdout or "").strip()
        print(f"WARN: Auth/{_AUTH_NAME} group patch failed: {err}", file=sys.stderr)
        return False
    print(f"✓ Patched Auth/{_AUTH_NAME} allowedGroups/adminGroups", flush=True)
    return True


def _servicemesh_subscription_names(sub: dict) -> set[str]:
    status = sub.get("status") or {}
    names: set[str] = set()
    for key in ("currentCSV", "installedCSV"):
        val = str(status.get(key) or "").strip()
        if val:
            names.add(val)
    return names


def _subscription_resolution_failed(sub: dict) -> bool:
    for cond in (sub.get("status") or {}).get("conditions") or []:
        if isinstance(cond, dict) and cond.get("type") == "ResolutionFailed" and cond.get("status") == "True":
            return True
    return False


def _is_servicemesh_csv_name(name: str) -> bool:
    return name.lower().startswith(_SERVICEMESH_CSV_PREFIX)


def _subscription_installplan_missing(sub: dict) -> bool:
    for cond in (sub.get("status") or {}).get("conditions") or []:
        if not isinstance(cond, dict):
            continue
        if cond.get("type") == "InstallPlanMissing" and cond.get("status") == "True":
            return True
        reason = str(cond.get("reason") or "")
        if reason == "ReferencedInstallPlanNotFound":
            return True
    return False


def _subscription_csv_names(sub: dict) -> set[str]:
    status = sub.get("status") or {}
    names: set[str] = set()
    for key in ("currentCSV", "installedCSV"):
        val = str(status.get(key) or "").strip()
        if val:
            names.add(val)
    return names


def _csv_exists(csv_names: set[str], csv_items: list[dict]) -> bool:
    if not csv_names:
        return True
    present = {
        str((item.get("metadata") or {}).get("name") or "").strip()
        for item in csv_items
        if isinstance(item, dict)
    }
    return all(name in present for name in csv_names)


def _recreate_subscription(namespace: str, sub: dict) -> bool:
    meta = sub.get("metadata") or {}
    spec = sub.get("spec") or {}
    name = str(meta.get("name") or "").strip()
    if not name:
        return False
    channel = str(spec.get("channel") or "").strip()
    source = str(spec.get("source") or "").strip()
    source_ns = str(spec.get("sourceNamespace") or "").strip()
    if not channel or not source or not source_ns:
        print(
            f"WARN: cannot recreate Subscription/{name} in {namespace} (incomplete spec)",
            file=sys.stderr,
        )
        return False
    new_doc = {
        "apiVersion": "operators.coreos.com/v1alpha1",
        "kind": "Subscription",
        "metadata": {"name": name, "namespace": namespace},
        "spec": {
            "channel": channel,
            "name": str(spec.get("name") or name).strip(),
            "source": source,
            "sourceNamespace": source_ns,
            "installPlanApproval": str(spec.get("installPlanApproval") or "Manual").strip()
            or "Manual",
        },
    }
    dr = oc_run(
        ["delete", "subscription", name, "-n", namespace, "--wait=true"],
        check=False,
        capture_output=True,
        timeout=180,
    )
    if dr.returncode != 0:
        err = (dr.stderr or dr.stdout or "").strip()
        print(f"WARN: could not delete Subscription/{name}: {err}", file=sys.stderr)
        return False
    ar = oc_run(
        ["apply", "-f", "-"],
        stdin_text=json.dumps(new_doc),
        check=False,
        capture_output=True,
        timeout=60,
    )
    if ar.returncode != 0:
        err = (ar.stderr or ar.stdout or "").strip()
        print(f"WARN: could not recreate Subscription/{name}: {err}", file=sys.stderr)
        return False
    print(f"✓ Recreated Subscription/{name} in {namespace} (stale InstallPlan ref)", flush=True)
    return True


def repair_servicemesh_subscription_stale_refs(namespace: str = "openshift-operators") -> int:
    """Recreate Service Mesh subscriptions stuck with missing InstallPlan or CSV."""
    sub_r = oc_run(
        ["get", "subscription", "-n", namespace, "-o", "json"],
        check=False,
        capture_output=True,
        timeout=60,
    )
    if sub_r.returncode != 0:
        return 0
    try:
        sub_doc = json.loads(sub_r.stdout or "{}")
    except json.JSONDecodeError:
        return 0

    csv_r = oc_run(
        ["get", "csv", "-n", namespace, "-o", "json"],
        check=False,
        capture_output=True,
        timeout=60,
    )
    csv_items: list[dict] = []
    if csv_r.returncode == 0:
        try:
            csv_items = [
                item
                for item in (json.loads(csv_r.stdout or "{}").get("items") or [])
                if isinstance(item, dict)
            ]
        except json.JSONDecodeError:
            csv_items = []

    repaired = 0
    for item in sub_doc.get("items") or []:
        if not isinstance(item, dict):
            continue
        name = ((item.get("metadata") or {}).get("name") or "").lower()
        if not _is_servicemesh_csv_name(name):
            continue
        csv_names = _subscription_csv_names(item)
        needs_repair = _subscription_installplan_missing(item) or (
            bool(csv_names) and not _csv_exists(csv_names, csv_items)
        )
        if needs_repair and _recreate_subscription(namespace, item):
            repaired += 1
    return repaired


def _istio_resource_not_found(stderr: str, stdout: str = "") -> bool:
    combined = f"{stderr}\n{stdout}".lower()
    return (
        "notfound" in combined
        or "not found" in combined
        or "doesn't have a resource type" in combined
        or "no matches for kind" in combined
    )


def _parse_openshift_gateway_istio_json(stdout: str) -> tuple[dict | None, str]:
    if not (stdout or "").strip():
        return None, "missing"
    try:
        doc = json.loads(stdout)
    except json.JSONDecodeError:
        return None, "error"
    if not isinstance(doc, dict):
        return None, "error"
    return doc, "ok"


def _fetch_openshift_gateway_istio_doc() -> tuple[dict | None, str]:
    """Return (doc, status) where status is ``ok``, ``missing``, or ``error``."""
    get_commands = (
        ["get", "istio", _OPENSHIFT_GATEWAY_ISTIO_NAME, "-o", "json"],
        ["get", "istios.sailoperator.io", _OPENSHIFT_GATEWAY_ISTIO_NAME, "-o", "json"],
    )
    last_err = ""
    for command in get_commands:
        r = oc_run(
            command,
            check=False,
            capture_output=True,
            timeout=30,
        )
        if r.returncode == 0:
            return _parse_openshift_gateway_istio_json(r.stdout or "")
        err = (r.stderr or r.stdout or "").strip()
        last_err = err or last_err
        if _istio_resource_not_found(r.stderr or "", r.stdout or ""):
            continue
        print(
            f"WARN: could not read Istio/{_OPENSHIFT_GATEWAY_ISTIO_NAME}: "
            f"{err or 'unknown error'}",
            file=sys.stderr,
            flush=True,
        )
        return None, "error"
    if last_err:
        print(
            f"WARN: Istio/{_OPENSHIFT_GATEWAY_ISTIO_NAME} not found via istio or "
            f"istios.sailoperator.io: {last_err}",
            file=sys.stderr,
            flush=True,
        )
    return None, "missing"


def _openshift_gateway_istio_doc() -> dict | None:
    doc, status = _fetch_openshift_gateway_istio_doc()
    return doc if status == "ok" else None


def _istio_reconcile_error_message(doc: dict) -> str:
    for item in (doc.get("status") or {}).get("conditions") or []:
        if not isinstance(item, dict):
            continue
        if item.get("type") == "Reconciled" and str(item.get("status") or "") == "False":
            return str(item.get("message") or "")
    return ""


def _is_istio_eol_reconcile_error(message: str) -> bool:
    lowered = message.lower()
    return any(marker in lowered for marker in _ISTIO_EOL_MARKERS) or "cannot be installed" in lowered


def _openshift_gateway_istio_wait_sec() -> int:
    raw = os.environ.get("OPENSHIFT_GATEWAY_ISTIO_WAIT_SEC", "").strip()
    if not raw:
        return _DEFAULT_OPENSHIFT_GATEWAY_ISTIO_WAIT_SEC
    try:
        return int(raw)
    except ValueError:
        print(
            f"WARN: invalid OPENSHIFT_GATEWAY_ISTIO_WAIT_SEC={raw!r}; "
            f"using default {_DEFAULT_OPENSHIFT_GATEWAY_ISTIO_WAIT_SEC}s",
            file=sys.stderr,
            flush=True,
        )
        return _DEFAULT_OPENSHIFT_GATEWAY_ISTIO_WAIT_SEC


def _openshift_gateway_istio_reconciled(doc: dict) -> bool:
    state = str((doc.get("status") or {}).get("state") or "")
    if state == "ReconcileError":
        return False
    for item in (doc.get("status") or {}).get("conditions") or []:
        if not isinstance(item, dict):
            continue
        if item.get("type") == "Reconciled":
            return str(item.get("status") or "") == "True"
    return state == "Healthy"


def _openshift_gateway_istio_revision_doc() -> dict | None:
    r = oc_run(
        [
            "get",
            "istiorevision",
            _OPENSHIFT_GATEWAY_ISTIO_NAME,
            "-n",
            _OPENSHIFT_GATEWAY_NS,
            "-o",
            "json",
        ],
        check=False,
        capture_output=True,
        timeout=30,
    )
    if r.returncode != 0 or not (r.stdout or "").strip():
        return None
    try:
        doc = json.loads(r.stdout)
    except json.JSONDecodeError:
        return None
    return doc if isinstance(doc, dict) else None


def _istiorevision_ready(doc: dict) -> bool:
    status = str((doc.get("status") or {}).get("status") or "").strip()
    if status == "Healthy":
        return True
    for item in (doc.get("status") or {}).get("conditions") or []:
        if not isinstance(item, dict):
            continue
        if item.get("type") == "Ready" and str(item.get("status") or "") == "True":
            return True
    return False


def _openshift_gateway_controller_ready() -> bool:
    r = oc_run(
        [
            "get",
            "deployment",
            _OPENSHIFT_GATEWAY_ISTIOD,
            "-n",
            _OPENSHIFT_GATEWAY_NS,
            "-o",
            "jsonpath={.status.availableReplicas}",
        ],
        check=False,
        capture_output=True,
        timeout=30,
    )
    if r.returncode != 0:
        return False
    try:
        return int((r.stdout or "0").strip() or "0") >= 1
    except ValueError:
        return False


def _openshift_gateway_istio_stack_ready(*, target_version: str | None = None) -> bool:
    """True when openshift-gateway controller is up (Istio CR or revision+istiod)."""
    doc = _openshift_gateway_istio_doc()
    if doc and _openshift_gateway_istio_reconciled(doc):
        if target_version:
            istio_version = str((doc.get("spec") or {}).get("version") or "").strip()
            if istio_version and istio_version != target_version:
                return False
        return True
    revision = _openshift_gateway_istio_revision_doc()
    if not revision or not _istiorevision_ready(revision):
        return False
    if target_version:
        revision_version = str((revision.get("spec") or {}).get("version") or "").strip()
        if revision_version and revision_version != target_version:
            return False
    return _openshift_gateway_controller_ready()


def openshift_gateway_istio_stack_ready(*, target_version: str | None = None) -> bool:
    """Public probe for install-dep-operators and tests."""
    return _openshift_gateway_istio_stack_ready(target_version=target_version)


def _parse_istio_version_from_alm_examples(alm_raw: str) -> str | None:
    try:
        examples = json.loads(alm_raw)
    except json.JSONDecodeError:
        return None
    if not isinstance(examples, list):
        return None
    for item in examples:
        if not isinstance(item, dict) or item.get("kind") != "Istio":
            continue
        version = str((item.get("spec") or {}).get("version") or "").strip()
        if version:
            return version
    return None


def _servicemesh_istio_version_from_csv(namespace: str = "openshift-operators") -> str | None:
    env_version = os.environ.get("SERVICEMESH_ISTIO_VERSION", "").strip()
    if env_version:
        return env_version
    r = oc_run(
        ["get", "csv", "-n", namespace, "-o", "json"],
        check=False,
        capture_output=True,
        timeout=60,
    )
    if r.returncode != 0:
        return None
    try:
        doc = json.loads(r.stdout or "{}")
    except json.JSONDecodeError:
        return None
    for item in doc.get("items") or []:
        if not isinstance(item, dict):
            continue
        name = ((item.get("metadata") or {}).get("name") or "").lower()
        if not name.startswith(_SERVICEMESH_CSV_PREFIX):
            continue
        phase = ((item.get("status") or {}).get("phase") or "").strip()
        if phase != "Succeeded":
            continue
        annotations = (item.get("metadata") or {}).get("annotations") or {}
        version = _parse_istio_version_from_alm_examples(str(annotations.get("alm-examples") or ""))
        if version:
            return version
    return None


def reconcile_openshift_gateway_istio_eol(namespace: str = "openshift-operators") -> bool:
    """Patch cluster Istio/openshift-gateway when OSSM rejects an end-of-life spec.version."""
    doc = _openshift_gateway_istio_doc()
    if not doc:
        return False
    state = str((doc.get("status") or {}).get("state") or "")
    message = _istio_reconcile_error_message(doc)
    if state != "ReconcileError" or not _is_istio_eol_reconcile_error(message):
        return False
    current = str((doc.get("spec") or {}).get("version") or "").strip()
    target = _servicemesh_istio_version_from_csv(namespace)
    if not target:
        print(
            "WARN: openshift-gateway Istio is end-of-life but no supported version found "
            "(set SERVICEMESH_ISTIO_VERSION or install Service Mesh CSV)",
            file=sys.stderr,
            flush=True,
        )
        return False
    if current == target:
        return False
    patch_doc = {"spec": {"version": target}}
    r = oc_run(
        [
            "patch",
            "istio",
            _OPENSHIFT_GATEWAY_ISTIO_NAME,
            "--type=merge",
            "-p",
            json.dumps(patch_doc),
        ],
        check=False,
        capture_output=True,
        timeout=60,
    )
    if r.returncode != 0:
        err = (r.stderr or r.stdout or "").strip()
        print(
            f"WARN: could not patch Istio/{_OPENSHIFT_GATEWAY_ISTIO_NAME} to {target}: {err}",
            file=sys.stderr,
            flush=True,
        )
        return False
    print(
        f"✓ Patched Istio/{_OPENSHIFT_GATEWAY_ISTIO_NAME} version {current or '?'} -> {target}",
        flush=True,
    )
    return True


def wait_openshift_gateway_istio_ready(
    *, timeout_sec: int = 300, target_version: str | None = None
) -> bool:
    """Wait for openshift-gateway Istio stack (CR reconcile or revision+istiod after EOL patch)."""
    print(
        f"Waiting for Istio/{_OPENSHIFT_GATEWAY_ISTIO_NAME} reconcile (up to {timeout_sec}s)...",
        flush=True,
    )
    deadline = time.monotonic() + timeout_sec
    attempt = 0
    while time.monotonic() < deadline:
        attempt += 1
        if _openshift_gateway_istio_stack_ready(target_version=target_version):
            doc = _openshift_gateway_istio_doc()
            if doc and _openshift_gateway_istio_reconciled(doc):
                state = str((doc.get("status") or {}).get("state") or "")
                print(
                    f"✓ Istio/{_OPENSHIFT_GATEWAY_ISTIO_NAME} reconciled (state={state or 'Ready'})",
                    flush=True,
                )
            else:
                revision = _openshift_gateway_istio_revision_doc()
                rev_version = str((revision.get("spec") or {}).get("version") or "?") if revision else "?"
                print(
                    f"✓ Istio/{_OPENSHIFT_GATEWAY_ISTIO_NAME} controller ready "
                    f"(revision={rev_version}, istiod available)",
                    flush=True,
                )
            return True
        doc = _openshift_gateway_istio_doc()
        if doc:
            state = str((doc.get("status") or {}).get("state") or "")
            message = _istio_reconcile_error_message(doc)
            print(
                f"  Istio/{_OPENSHIFT_GATEWAY_ISTIO_NAME} attempt {attempt}: "
                f"state={state or '?'} message={message or 'pending'}",
                flush=True,
            )
        else:
            print(f"  Istio/{_OPENSHIFT_GATEWAY_ISTIO_NAME} attempt {attempt}: CR not found", flush=True)
        time.sleep(15)
    print(
        f"WARN: Istio/{_OPENSHIFT_GATEWAY_ISTIO_NAME} not reconciled within {timeout_sec}s",
        file=sys.stderr,
        flush=True,
    )
    return False


_OPENSHIFT_GATEWAY_CONTROLLER_DEPLOYMENTS = (
    "istiod-openshift-gateway",
    "data-science-gateway-data-science-gateway-class",
)


def wait_openshift_gateway_controller_deployments(
    *,
    namespace: str = _OPENSHIFT_GATEWAY_NS,
    timeout_sec: int = 300,
) -> bool:
    """Wait for istiod and data-science-gateway controller deployments (rh-ai route backend)."""
    ready = True
    for name in _OPENSHIFT_GATEWAY_CONTROLLER_DEPLOYMENTS:
        r = oc_run(
            [
                "wait",
                "--for=condition=available",
                f"--timeout={timeout_sec}s",
                f"deployment/{name}",
                "-n",
                namespace,
            ],
            check=False,
            capture_output=True,
            timeout=timeout_sec + 30,
        )
        if r.returncode == 0:
            print(f"✓ deployment/{name} in {namespace} is Available", flush=True)
            continue
        err = (r.stderr or r.stdout or "").strip()
        print(
            f"WARN: deployment/{name} in {namespace} not Available within {timeout_sec}s"
            f"{f': {err}' if err else ''}",
            file=sys.stderr,
            flush=True,
        )
        ready = False
    return ready


def ensure_openshift_gateway_istio_for_verify(namespace: str = "openshift-operators") -> bool:
    """verify-operator-ready: patch EOL openshift-gateway Istio and wait for controller pods."""
    deploy_wait = int(os.environ.get("OPENSHIFT_GATEWAY_CONTROLLER_WAIT_SEC", "300"))
    if openshift_gateway_istio_stack_ready():
        return wait_openshift_gateway_controller_deployments(timeout_sec=deploy_wait)
    reconcile_openshift_gateway_istio_eol(namespace)
    if not wait_openshift_gateway_istio_ready(timeout_sec=_openshift_gateway_istio_wait_sec()):
        return False
    return wait_openshift_gateway_controller_deployments(timeout_sec=deploy_wait)


def ensure_openshift_gateway_istio_for_dep_operators(namespace: str = "openshift-operators") -> bool:
    """install-dep-operators: fix EOL openshift-gateway Istio before RHOAI gateway stack install."""
    target_version = _servicemesh_istio_version_from_csv(namespace)
    if _openshift_gateway_istio_stack_ready(target_version=target_version):
        return True
    doc, status = _fetch_openshift_gateway_istio_doc()
    if status == "missing":
        return True
    if status == "error" or not doc:
        return _openshift_gateway_istio_stack_ready(target_version=target_version)
    if _openshift_gateway_istio_reconciled(doc):
        if target_version:
            istio_version = str((doc.get("spec") or {}).get("version") or "").strip()
            if not istio_version or istio_version == target_version:
                return True
        else:
            return True
    message = _istio_reconcile_error_message(doc)
    if not _is_istio_eol_reconcile_error(message):
        state = str((doc.get("status") or {}).get("state") or "")
        print(
            f"WARN: Istio/{_OPENSHIFT_GATEWAY_ISTIO_NAME} not reconciled "
            f"(state={state or '?'}); not an EOL version issue",
            file=sys.stderr,
            flush=True,
        )
        return False
    if not reconcile_openshift_gateway_istio_eol(namespace):
        return False
    return wait_openshift_gateway_istio_ready(
        timeout_sec=_openshift_gateway_istio_wait_sec(),
        target_version=target_version,
    )


def reconcile_servicemesh_olm_conflicts(namespace: str = "openshift-operators") -> int:
    """Drop orphan Pending/Failed Service Mesh CSVs blocking OLM resolution on HCP clusters."""
    repaired = repair_servicemesh_subscription_stale_refs(namespace)
    sub_r = oc_run(
        ["get", "subscription", "-n", namespace, "-o", "json"],
        check=False,
        capture_output=True,
        timeout=60,
    )
    if sub_r.returncode != 0:
        return 0
    try:
        sub_doc = json.loads(sub_r.stdout or "{}")
    except json.JSONDecodeError:
        return 0

    target_csvs: set[str] = set()
    resolution_failed = False
    for item in sub_doc.get("items") or []:
        if not isinstance(item, dict):
            continue
        name = ((item.get("metadata") or {}).get("name") or "").lower()
        if not _is_servicemesh_csv_name(name):
            continue
        target_csvs |= _servicemesh_subscription_names(item)
        if _subscription_resolution_failed(item):
            resolution_failed = True

    csv_r = oc_run(
        ["get", "csv", "-n", namespace, "-o", "json"],
        check=False,
        capture_output=True,
        timeout=60,
    )
    if csv_r.returncode != 0:
        return repaired
    try:
        csv_doc = json.loads(csv_r.stdout or "{}")
    except json.JSONDecodeError:
        return 0

    upgrade_stale_csvs: set[str] = set()
    for item in sub_doc.get("items") or []:
        if not isinstance(item, dict):
            continue
        name = ((item.get("metadata") or {}).get("name") or "").lower()
        if not _is_servicemesh_csv_name(name):
            continue
        status = item.get("status") or {}
        current_csv = str(status.get("currentCSV") or "").strip()
        installed_csv = str(status.get("installedCSV") or "").strip()
        if current_csv and installed_csv and current_csv != installed_csv:
            upgrade_stale_csvs.add(installed_csv)

    orphan_names: list[str] = []
    for item in csv_doc.get("items") or []:
        if not isinstance(item, dict):
            continue
        csv_name = ((item.get("metadata") or {}).get("name") or "").strip()
        if not _is_servicemesh_csv_name(csv_name):
            continue
        phase = ((item.get("status") or {}).get("phase") or "").strip()
        if phase == "Succeeded":
            continue
        if csv_name in target_csvs and phase in ("Installing", "Replacing"):
            continue
        if phase in ("Pending", "Failed") and csv_name not in target_csvs:
            orphan_names.append(csv_name)
        elif phase == "Pending" and csv_name in upgrade_stale_csvs:
            orphan_names.append(csv_name)
        elif resolution_failed and phase == "Pending" and len(target_csvs) == 1 and csv_name in target_csvs:
            orphan_names.append(csv_name)

    if not orphan_names:
        return 0

    removed = 0
    for csv_name in sorted(set(orphan_names)):
        dr = oc_run(
            ["delete", "csv", csv_name, "-n", namespace, "--wait=false"],
            check=False,
            capture_output=True,
            timeout=60,
        )
        if dr.returncode == 0:
            print(f"✓ Removed orphan Service Mesh CSV/{csv_name} in {namespace}", flush=True)
            removed += 1
        else:
            err = (dr.stderr or dr.stdout or "").strip()
            print(f"WARN: could not delete CSV/{csv_name}: {err}", file=sys.stderr)

    ip_r = oc_run(
        ["get", "installplan", "-n", namespace, "-o", "json"],
        check=False,
        capture_output=True,
        timeout=60,
    )
    if ip_r.returncode == 0:
        try:
            ip_doc = json.loads(ip_r.stdout or "{}")
        except json.JSONDecodeError:
            ip_doc = {}
        orphan_set = set(orphan_names)
        for item in ip_doc.get("items") or []:
            if not isinstance(item, dict):
                continue
            spec = item.get("spec") or {}
            csvs = {str(c) for c in (spec.get("clusterServiceVersionNames") or [])}
            if not csvs & orphan_set:
                continue
            phase = ((item.get("status") or {}).get("phase") or "").strip()
            if phase not in ("Failed", "RequiresApproval"):
                continue
            ip_name = (item.get("metadata") or {}).get("name") or ""
            if not ip_name:
                continue
            oc_run(
                ["delete", "installplan", ip_name, "-n", namespace, "--wait=false"],
                check=False,
                capture_output=True,
                timeout=60,
            )
            print(f"✓ Removed stale InstallPlan/{ip_name} for orphan Service Mesh CSV", flush=True)
    return repaired + removed


def wait_servicemesh_csv_succeeded(
    namespace: str = "openshift-operators",
    *,
    timeout_sec: int = 900,
) -> bool:
    """Wait for a Service Mesh operator CSV to reach Succeeded (after InstallPlan approve)."""
    if gateway_config_ready():
        print("✓ GatewayConfig already Ready — skipping Service Mesh CSV wait", flush=True)
        return True
    deadline = time.monotonic() + timeout_sec
    while time.monotonic() < deadline:
        r = oc_run(
            ["get", "csv", "-n", namespace, "-o", "json"],
            check=False,
            capture_output=True,
            timeout=60,
        )
        if r.returncode == 0:
            try:
                doc = json.loads(r.stdout or "{}")
            except json.JSONDecodeError:
                doc = {}
            for item in doc.get("items") or []:
                if not isinstance(item, dict):
                    continue
                name = ((item.get("metadata") or {}).get("name") or "").lower()
                if not name.startswith(_SERVICEMESH_CSV_PREFIX):
                    continue
                phase = ((item.get("status") or {}).get("phase") or "").strip()
                if phase == "Succeeded":
                    print(f"✓ Service Mesh CSV {name} is Succeeded", flush=True)
                    return True
                if phase == "Failed":
                    print(f"WARN: Service Mesh CSV {name} is Failed", file=sys.stderr)
                    return False
                print(f"  Service Mesh CSV {name} phase={phase or '?'}", flush=True)
        time.sleep(15)
    print(f"WARN: Service Mesh CSV not Succeeded within {timeout_sec}s", file=sys.stderr)
    return False


def wait_gateway_config_ready(*, timeout_sec: int = 900) -> bool:
    """Poll GatewayConfig until Ready, ProvisioningSucceeded, and GatewayConfigReady are True."""
    print(f"Waiting for GatewayConfig/{_GATEWAY_NAME} Ready (up to {timeout_sec}s)...", flush=True)
    deadline = time.monotonic() + timeout_sec
    attempt = 0
    while time.monotonic() < deadline:
        attempt += 1
        doc = _gateway_config_doc()
        if doc:
            statuses = {name: _condition_status(doc, name) for name in _READY_CONDITIONS}
            if all(status == "True" for status in statuses.values()):
                print(f"✓ GatewayConfig/{_GATEWAY_NAME} is Ready", flush=True)
                return True
            print(f"  GatewayConfig attempt {attempt}: {statuses}", flush=True)
        else:
            print(f"  GatewayConfig attempt {attempt}: CR not found yet", flush=True)
        time.sleep(15)
    print(f"WARN: GatewayConfig/{_GATEWAY_NAME} not Ready within {timeout_sec}s", file=sys.stderr)
    return False


def ensure_rhoai_gateway_for_install(
    *,
    wait_timeout_sec: int = 900,
    wait_servicemesh_first: bool = False,
) -> None:
    """Post-operator install: OIDC patch + Auth groups + optional SM wait + gateway Ready wait."""
    if wait_servicemesh_first:
        timeout = int(os.environ.get("SERVICEMESH_CSV_WAIT_SEC", "900"))
        wait_servicemesh_csv_succeeded(timeout_sec=timeout)
    patch_gateway_config_oidc()
    configure_auth_cr_groups()
    if not wait_servicemesh_first:
        timeout = int(os.environ.get("SERVICEMESH_CSV_WAIT_SEC", "300"))
        wait_servicemesh_csv_succeeded(timeout_sec=timeout)
    if not wait_gateway_config_ready(timeout_sec=wait_timeout_sec):
        print(
            "WARN: GatewayConfig not Ready after install prep; dashboard/gateway tests may fail",
            file=sys.stderr,
        )
