"""MaaS Gateway API and OpenShift Route setup."""

from __future__ import annotations

import os
import sys
import time

from install.dsc_install import oc_run

from components.maas_billing.common import (
    _AUTHORINO_TLS_ANNOTATION,
    _GATEWAY_NAME,
    _GATEWAY_NS,
    _GATEWAY_SVC,
    _MAAS_APPS_NS,
    _MAAS_AUTH_POLICY,
    _MAAS_AUTH_POLICY_PATH,
    _MANAGED_ANNOTATION,
    _secret_exists,
    maas_api_auth_validate_url,
    maas_api_namespace,
)
from components.maas_billing.database import _clone_models_as_a_service


def _cluster_domain() -> str:
    r = oc_run(
        ["get", "ingresses.config.openshift.io", "cluster", "-o", "jsonpath={.spec.domain}"],
        check=False,
        capture_output=True,
        timeout=30,
    )
    domain = (r.stdout or "").strip()
    if not domain:
        raise RuntimeError("Could not detect OpenShift cluster domain from ingresses.config/cluster")
    return domain


def _ingress_tls_secret_name() -> str:
    r = oc_run(
        [
            "get",
            "ingresscontroller",
            "default",
            "-n",
            "openshift-ingress-operator",
            "-o",
            "jsonpath={.spec.defaultCertificate.name}",
        ],
        check=False,
        capture_output=True,
        timeout=30,
    )
    cert_name = (r.stdout or "").strip()
    return cert_name or "router-certs-default"


_INGRESS_OPERATOR_NS = "openshift-ingress-operator"
_ROUTER_CERT_FALLBACK = "router-certs-default"


def _copy_tls_secret_to_gateway_ns(name: str, *, src_ns: str) -> bool:
    import json

    r = oc_run(
        ["get", "secret", name, "-n", src_ns, "-o", "json"],
        check=False,
        capture_output=True,
        timeout=30,
    )
    if r.returncode != 0:
        return False
    try:
        doc = json.loads(r.stdout or "{}")
    except json.JSONDecodeError:
        return False
    meta = dict(doc.get("metadata") or {})
    meta["namespace"] = _GATEWAY_NS
    for key in ("resourceVersion", "uid", "creationTimestamp", "managedFields", "ownerReferences"):
        meta.pop(key, None)
    doc["metadata"] = meta
    apply = oc_run(
        ["apply", "-f", "-"],
        stdin_text=json.dumps(doc),
        check=False,
        capture_output=True,
        timeout=60,
    )
    if apply.returncode != 0:
        err = (apply.stderr or apply.stdout or "").strip()
        print(
            f"WARN: could not copy TLS secret {src_ns}/{name} to {_GATEWAY_NS}: {err[:200]}",
            flush=True,
        )
        return False
    print(f"✓ Copied TLS secret {src_ns}/{name} → {_GATEWAY_NS}/{name}", flush=True)
    return True


_OPENSHIFT_DEFAULT_GATEWAY_CLASS = "openshift-default"
_OPENSHIFT_GATEWAY_CONTROLLER = "openshift.io/gateway-controller/v1"


def ensure_openshift_default_gateway_class() -> None:
    """Ensure GatewayClass openshift-default exists (Jenkins configure-maas-gateway parity)."""
    r = oc_run(
        [
            "get",
            "gatewayclass",
            _OPENSHIFT_DEFAULT_GATEWAY_CLASS,
            "-o",
            "jsonpath={.status.conditions[?(@.type==\"Accepted\")].status}",
        ],
        check=False,
        capture_output=True,
        timeout=30,
    )
    if r.returncode == 0 and (r.stdout or "").strip() == "True":
        print(
            f"✓ GatewayClass {_OPENSHIFT_DEFAULT_GATEWAY_CLASS} Accepted=True",
            flush=True,
        )
        return
    yaml_doc = f"""\
apiVersion: gateway.networking.k8s.io/v1
kind: GatewayClass
metadata:
  name: {_OPENSHIFT_DEFAULT_GATEWAY_CLASS}
spec:
  controllerName: "{_OPENSHIFT_GATEWAY_CONTROLLER}"
"""
    print(f"Applying GatewayClass {_OPENSHIFT_DEFAULT_GATEWAY_CLASS}...", flush=True)
    apply = oc_run(
        ["apply", "-f", "-"],
        stdin_text=yaml_doc,
        check=False,
        capture_output=True,
        timeout=60,
    )
    if apply.returncode != 0:
        err = (apply.stderr or apply.stdout or "").strip()
        print(
            f"WARN: could not apply GatewayClass {_OPENSHIFT_DEFAULT_GATEWAY_CLASS}: "
            f"{err[:200]}",
            flush=True,
        )
        return
    print(f"✓ GatewayClass {_OPENSHIFT_DEFAULT_GATEWAY_CLASS} applied", flush=True)


def openshift_default_gateway_class_accepted() -> bool:
    r = oc_run(
        [
            "get",
            "gatewayclass",
            _OPENSHIFT_DEFAULT_GATEWAY_CLASS,
            "-o",
            "jsonpath={.status.conditions[?(@.type==\"Accepted\")].status}",
        ],
        check=False,
        capture_output=True,
        timeout=30,
    )
    return r.returncode == 0 and (r.stdout or "").strip() == "True"


def wait_openshift_default_gateway_class_accepted(
    *,
    timeout_sec: int | None = None,
) -> bool:
    """Apply openshift-default GatewayClass if needed and poll until Accepted=True."""
    ensure_openshift_default_gateway_class()
    if openshift_default_gateway_class_accepted():
        return True
    raw = os.environ.get("GATEWAY_CLASS_ACCEPT_TIMEOUT_SEC", "").strip()
    if timeout_sec is None:
        try:
            timeout_sec = int(raw) if raw else 300
        except ValueError:
            timeout_sec = 300
    deadline = time.time() + timeout_sec
    while time.time() < deadline:
        if openshift_default_gateway_class_accepted():
            print(
                f"✓ GatewayClass {_OPENSHIFT_DEFAULT_GATEWAY_CLASS} Accepted=True",
                flush=True,
            )
            return True
        if int(time.time()) % 30 < 12:
            print(
                f"Waiting for GatewayClass {_OPENSHIFT_DEFAULT_GATEWAY_CLASS} Accepted=True...",
                flush=True,
            )
        time.sleep(10)
    print(
        f"WARN: GatewayClass {_OPENSHIFT_DEFAULT_GATEWAY_CLASS} not Accepted within {timeout_sec}s",
        file=sys.stderr,
        flush=True,
    )
    return False


def ensure_maas_gateway_ingress_tls_secret() -> None:
    """Ensure the Gateway HTTPS listener certificateRef exists in openshift-ingress."""
    cert_name = _ingress_tls_secret_name()
    if _secret_exists(_GATEWAY_NS, cert_name):
        print(f"✓ MaaS gateway TLS secret {_GATEWAY_NS}/{cert_name} exists", flush=True)
        return
    if _copy_tls_secret_to_gateway_ns(cert_name, src_ns=_INGRESS_OPERATOR_NS):
        return
    if cert_name != _ROUTER_CERT_FALLBACK and _secret_exists(_GATEWAY_NS, _ROUTER_CERT_FALLBACK):
        print(
            f"✓ MaaS gateway TLS fallback {_GATEWAY_NS}/{_ROUTER_CERT_FALLBACK} exists",
            flush=True,
        )
        return
    if _copy_tls_secret_to_gateway_ns(_ROUTER_CERT_FALLBACK, src_ns=_GATEWAY_NS):
        return
    if _copy_tls_secret_to_gateway_ns(_ROUTER_CERT_FALLBACK, src_ns=_INGRESS_OPERATOR_NS):
        return
    print(
        f"WARN: MaaS gateway TLS secret {_GATEWAY_NS}/{cert_name} missing "
        f"(Gateway may stay Programmed=Unknown)",
        flush=True,
    )


def _gateway_yaml(cluster_domain: str, cert_name: str) -> str:
    hostname = f"maas.{cluster_domain}"
    return f"""\
apiVersion: gateway.networking.k8s.io/v1
kind: Gateway
metadata:
  name: {_GATEWAY_NAME}
  namespace: {_GATEWAY_NS}
  annotations:
    {_MANAGED_ANNOTATION}: "false"
    {_AUTHORINO_TLS_ANNOTATION}: "true"
  labels:
    app.kubernetes.io/name: maas
    app.kubernetes.io/instance: {_GATEWAY_NAME}
    app.kubernetes.io/component: gateway
    opendatahub.io/managed: "false"
spec:
  gatewayClassName: openshift-default
  listeners:
    - name: http
      hostname: "{hostname}"
      port: 80
      protocol: HTTP
      allowedRoutes:
        namespaces:
          from: All
    - name: https
      hostname: "{hostname}"
      port: 443
      protocol: HTTPS
      allowedRoutes:
        namespaces:
          from: All
      tls:
        certificateRefs:
          - group: ""
            kind: Secret
            name: {cert_name}
            namespace: {_GATEWAY_NS}
        mode: Terminate
"""


def _gateway_route_yaml(cluster_domain: str) -> str:
    hostname = f"maas.{cluster_domain}"
    return f"""\
apiVersion: route.openshift.io/v1
kind: Route
metadata:
  name: {_GATEWAY_NAME}
  namespace: {_GATEWAY_NS}
  labels:
    app.kubernetes.io/name: maas
    app.kubernetes.io/instance: {_GATEWAY_NAME}
    app.kubernetes.io/component: gateway
    opendatahub.io/managed: "false"
  annotations:
    opendatahub.io/managed: "false"
spec:
  host: {hostname}
  port:
    targetPort: 443
  tls:
    termination: passthrough
    insecureEdgeTerminationPolicy: Redirect
  to:
    kind: Service
    name: {_GATEWAY_SVC}
    weight: 100
  wildcardPolicy: None
"""


def _maas_gateway_route_ready(hostname: str) -> bool:
    r = oc_run(
        [
            "get",
            "route",
            _GATEWAY_NAME,
            "-n",
            _GATEWAY_NS,
            "-o",
            "jsonpath={.spec.host},{.spec.tls.termination}",
        ],
        check=False,
        capture_output=True,
        timeout=30,
    )
    if r.returncode != 0:
        return False
    parts = (r.stdout or "").strip().split(",", 1)
    if len(parts) != 2:
        return False
    host, termination = parts
    return host == hostname and termination == "passthrough"


def ensure_maas_gateway_route() -> None:
    """Expose MaaS gateway on the default ingress router (maas.<cluster-domain>)."""
    domain = _cluster_domain()
    hostname = f"maas.{domain}"
    if _maas_gateway_route_ready(hostname):
        print(
            f"✓ MaaS gateway route {_GATEWAY_NS}/{_GATEWAY_NAME} → {hostname} (passthrough)",
            flush=True,
        )
        return
    print(f"Applying {_GATEWAY_NS}/{_GATEWAY_NAME} route for {hostname}...", flush=True)
    apply = oc_run(
        ["apply", "-f", "-"],
        stdin_text=_gateway_route_yaml(domain),
        check=False,
        capture_output=True,
        timeout=60,
    )
    if apply.returncode != 0:
        err = (apply.stderr or apply.stdout or "").strip()
        raise RuntimeError(f"Could not apply MaaS gateway route: {err or 'unknown error'}")
    print(f"✓ MaaS gateway route {_GATEWAY_NS}/{_GATEWAY_NAME} → {hostname}", flush=True)


def _maas_api_auth_policy_callback_url() -> str:
    r = oc_run(
        [
            "get",
            "authpolicy",
            _MAAS_AUTH_POLICY,
            "-n",
            _MAAS_APPS_NS,
            "-o",
            "jsonpath={.spec.rules.metadata.apiKeyValidation.http.url}",
        ],
        check=False,
        capture_output=True,
        timeout=30,
    )
    if r.returncode != 0:
        return ""
    return (r.stdout or "").strip()


def ensure_maas_api_auth_policy() -> None:
    """Apply maas-api Kuadrant AuthPolicy when missing or apiKeyValidation URL is wrong."""
    validate_url = maas_api_auth_validate_url()
    oc_run(
        ["delete", "authpolicy", _MAAS_AUTH_POLICY, "-n", "default", "--ignore-not-found"],
        check=False,
        capture_output=True,
        timeout=30,
    )
    current_url = _maas_api_auth_policy_callback_url()
    if current_url == validate_url:
        print(
            f"✓ MaaS API AuthPolicy {_MAAS_APPS_NS}/{_MAAS_AUTH_POLICY} has apiKeyValidation callback",
            flush=True,
        )
        return
    if current_url:
        print(
            f"Replacing {_MAAS_APPS_NS}/{_MAAS_AUTH_POLICY} "
            f"(stale callback {current_url!r} → {validate_url!r})...",
            flush=True,
        )
        deleted = oc_run(
            ["delete", "authpolicy", _MAAS_AUTH_POLICY, "-n", _MAAS_APPS_NS],
            check=False,
            capture_output=True,
            timeout=60,
        )
        if deleted.returncode != 0:
            err = (deleted.stderr or deleted.stdout or "").strip()
            raise RuntimeError(
                f"Could not delete stale MaaS API AuthPolicy: {err or 'unknown error'}"
            )
        time.sleep(2)

    repo = _clone_models_as_a_service()
    policy_path = repo / _MAAS_AUTH_POLICY_PATH
    if not policy_path.is_file():
        raise FileNotFoundError(f"Missing MaaS auth policy manifest: {policy_path}")

    yaml_doc = policy_path.read_text(encoding="utf-8").replace(
        "https://maas-api.placehold.svc.cluster.local:8443/internal/v1/api-keys/validate",
        validate_url,
    )
    print(f"Applying {_MAAS_APPS_NS}/{_MAAS_AUTH_POLICY}...", flush=True)
    apply = oc_run(
        ["apply", "-n", _MAAS_APPS_NS, "-f", "-"],
        stdin_text=yaml_doc,
        check=False,
        capture_output=True,
        timeout=60,
    )
    if apply.returncode != 0:
        err = (apply.stderr or apply.stdout or "").strip()
        raise RuntimeError(f"Could not apply MaaS API AuthPolicy: {err or 'unknown error'}")
    applied_url = _maas_api_auth_policy_callback_url()
    if applied_url != validate_url:
        raise RuntimeError(
            f"MaaS API AuthPolicy callback still {applied_url!r} after apply "
            f"(expected {validate_url!r})"
        )
    print(f"✓ MaaS API AuthPolicy {_MAAS_APPS_NS}/{_MAAS_AUTH_POLICY} applied", flush=True)


def ensure_maas_gateway_https_service_clusterip() -> bool:
    """On EPHC/HyperShift, create gateway-owned HTTPS ClusterIP when controller never Programs.

    OpenShift Gateway controller may leave Programmed=Unknown without creating the
    ``maas-default-gateway-openshift-default`` Service (LB provisioning gap). maas-api
    only needs a resolvable HTTPS ClusterIP + Route passthrough.
    """
    from components.maas_billing.common import _maas_gateway_https_service_ready
    from install.gateway_config import cluster_source_is_ephc
    from install.rosa_hcp_pull_setup import is_hypershift_managed_cluster

    if not (cluster_source_is_ephc() or is_hypershift_managed_cluster()):
        return False
    ready, detail = _maas_gateway_https_service_ready()
    if ready:
        return True
    pods = oc_run(
        [
            "get",
            "pods",
            "-n",
            _GATEWAY_NS,
            "-l",
            f"gateway.networking.k8s.io/gateway-name={_GATEWAY_NAME}",
            "-o",
            "jsonpath={.items[*].metadata.name}",
        ],
        check=False,
        capture_output=True,
        timeout=30,
    )
    if pods.returncode != 0 or not (pods.stdout or "").strip():
        print(
            f"NOTE: no gateway pods labeled for {_GATEWAY_NAME}; "
            "cannot synthesize HTTPS ClusterIP yet",
            flush=True,
        )
        return False
    yaml_doc = f"""\
apiVersion: v1
kind: Service
metadata:
  name: {_GATEWAY_SVC}
  namespace: {_GATEWAY_NS}
  labels:
    gateway.networking.k8s.io/gateway-name: {_GATEWAY_NAME}
    app.kubernetes.io/name: maas
    app.kubernetes.io/instance: {_GATEWAY_NAME}
    app.kubernetes.io/component: gateway
    opendatahub.io/managed: "false"
spec:
  type: ClusterIP
  selector:
    gateway.networking.k8s.io/gateway-name: {_GATEWAY_NAME}
  ports:
    - name: http
      port: 80
      targetPort: 8080
      protocol: TCP
    - name: https
      port: 443
      targetPort: 8443
      protocol: TCP
"""
    print(
        f"Applying EPHC fallback HTTPS ClusterIP {_GATEWAY_NS}/{_GATEWAY_SVC}...",
        flush=True,
    )
    apply = oc_run(
        ["apply", "-f", "-"],
        stdin_text=yaml_doc,
        check=False,
        capture_output=True,
        timeout=60,
    )
    if apply.returncode != 0:
        err = (apply.stderr or apply.stdout or "").strip()
        print(
            f"WARN: could not apply EPHC HTTPS ClusterIP: {err[:200]}",
            flush=True,
        )
        return False
    ready, detail = _maas_gateway_https_service_ready()
    if ready:
        print(f"✓ EPHC HTTPS ClusterIP ready ({detail})", flush=True)
        return True
    print(
        f"WARN: EPHC HTTPS ClusterIP applied but still not ready ({detail[:120]})",
        flush=True,
    )
    return False


def ensure_maas_gateway() -> None:
    """Ensure maas-default-gateway exists with MaaS-required annotations (rhoai-e2e configure-maas-gateway)."""
    from components.maas_billing.common import _maas_gateway_annotations_ready

    ensure_openshift_default_gateway_class()
    r = oc_run(
        ["get", "gateway", _GATEWAY_NAME, "-n", _GATEWAY_NS],
        check=False,
        capture_output=True,
        timeout=30,
    )
    if r.returncode != 0:
        domain = _cluster_domain()
        cert_name = _ingress_tls_secret_name()
        ensure_maas_gateway_ingress_tls_secret()
        print(f"Applying {_GATEWAY_NS}/{_GATEWAY_NAME} for maas.{domain}...", flush=True)
        yaml_doc = _gateway_yaml(domain, cert_name)
        apply = oc_run(["apply", "-f", "-"], stdin_text=yaml_doc, check=False, capture_output=True, timeout=60)
        if apply.returncode != 0:
            err = (apply.stderr or apply.stdout or "").strip()
            raise RuntimeError(f"Could not apply MaaS gateway: {err or 'unknown error'}")
    else:
        ann_ready, _ = _maas_gateway_annotations_ready()
        if not ann_ready:
            domain = _cluster_domain()
            cert_name = _ingress_tls_secret_name()
            print(
                f"Re-applying {_GATEWAY_NS}/{_GATEWAY_NAME} (annotations missing on live gateway)...",
                flush=True,
            )
            yaml_doc = _gateway_yaml(domain, cert_name)
            apply = oc_run(
                ["apply", "-f", "-"],
                stdin_text=yaml_doc,
                check=False,
                capture_output=True,
                timeout=60,
            )
            if apply.returncode != 0:
                err = (apply.stderr or apply.stdout or "").strip()
                raise RuntimeError(f"Could not re-apply MaaS gateway: {err or 'unknown error'}")
    oc_run(
        [
            "annotate",
            "gateway",
            _GATEWAY_NAME,
            "-n",
            _GATEWAY_NS,
            f"{_MANAGED_ANNOTATION}=false",
            f"{_AUTHORINO_TLS_ANNOTATION}=true",
            "--overwrite",
        ],
        check=False,
        capture_output=True,
        timeout=30,
    )
    print(f"✓ MaaS gateway {_GATEWAY_NS}/{_GATEWAY_NAME} annotated for Authorino TLS bootstrap", flush=True)
