"""Shared MaaS prerequisite constants and low-level helpers."""

from __future__ import annotations

import json
import os
from pathlib import Path

from install.dsc_install import dsc_crd_available, oc_run
from install.gateway_config import cluster_source_is_ephc
from k8s.kubectl_shim import kubectl_shim_dir as _kubectl_shim_dir

_GATEWAY_NS = "openshift-ingress"
_GATEWAY_NAME = "maas-default-gateway"
_GATEWAY_SVC = f"{_GATEWAY_NAME}-openshift-default"
_MAAS_APPS_NS = "redhat-ods-applications"
_MAAS_API_NS_CANDIDATES = ("redhat-ai-gateway-infra", "redhat-ods-applications")
_MAAS_AUTH_POLICY = "maas-api-auth-policy"
_MAAS_AUTH_POLICY_PATH = Path("deployment/base/maas-api/policies/auth-policy.yaml")
_MAAS_DB_SECRET = "maas-db-config"
_MONITORING_NS = "openshift-monitoring"
_MONITORING_CM = "cluster-monitoring-config"
_MODELS_AS_SERVICE_REPO = "https://github.com/opendatahub-io/models-as-a-service.git"
_MODELS_AS_SERVICE_DEST = Path("/tmp/models-as-a-service-components-prereqs")
_AUTHORINO_TLS_ANNOTATION = "security.opendatahub.io/authorino-tls-bootstrap"
_MANAGED_ANNOTATION = "opendatahub.io/managed"
_AUTHORINO_CR_NAME = "authorino"
_AUTHORINO_SVC = "authorino-authorino-authorization"
_AUTHORINO_TLS_SECRET = "authorino-server-cert"
_OLMINSTALL_MAAS_DEST = Path("/tmp/rhoai-e2e-maas-prereqs")
_KUADRANT_CR_NAME = "kuadrant"
_KUADRANT_OPERATOR_LABELS = (
    "control-plane=controller-manager",
    "app=kuadrant",
)


def _secret_exists(namespace: str, name: str) -> bool:
    r = oc_run(
        ["get", "secret", name, "-n", namespace],
        check=False,
        capture_output=True,
        timeout=30,
    )
    return r.returncode == 0


def models_as_service_ready_condition_type() -> str:
    """DSC Ready condition for MaaS (ModelsAsAServiceReady on RHOAI 3.5+)."""
    types = _dsc_condition_types()
    if "ModelsAsAServiceReady" in types:
        return "ModelsAsAServiceReady"
    return "ModelsAsServiceReady"


def maas_api_namespace() -> str:
    """Namespace hosting maas-api (redhat-ai-gateway-infra on RHOAI 3.5+)."""
    for ns in _MAAS_API_NS_CANDIDATES:
        r = oc_run(
            ["get", "deployment", "maas-api", "-n", ns],
            check=False,
            capture_output=True,
            timeout=30,
        )
        if r.returncode == 0:
            return ns
    return _MAAS_APPS_NS


def maas_api_auth_validate_url() -> str:
    """Internal URL Authorino uses to validate API keys against maas-api."""
    ns = maas_api_namespace()
    return (
        f"https://maas-api.{ns}.svc.cluster.local:8443/internal/v1/api-keys/validate"
    )


def maas_api_deployment_exists() -> bool:
    """True when the RHOAI operator has created the maas-api Deployment."""
    for ns in _MAAS_API_NS_CANDIDATES:
        r = oc_run(
            ["get", "deployment", "maas-api", "-n", ns],
            check=False,
            capture_output=True,
            timeout=30,
        )
        if r.returncode == 0:
            return True
    return False


def _dsc_condition(condition_type: str) -> tuple[str, str, str]:
    path = (
        f'{{.status.conditions[?(@.type=="{condition_type}")].status}}'
        f'\t{{.status.conditions[?(@.type=="{condition_type}")].reason}}'
        f'\t{{.status.conditions[?(@.type=="{condition_type}")].message}}'
    )
    r = oc_run(
        ["get", "datasciencecluster", "default-dsc", "-o", f"jsonpath={path}"],
        check=False,
        capture_output=True,
        timeout=30,
    )
    parts = (r.stdout or "").strip().split("\t")
    while len(parts) < 3:
        parts.append("")
    return parts[0], parts[1], parts[2]


def _dsc_condition_types() -> set[str]:
    r = oc_run(
        [
            "get",
            "datasciencecluster",
            "default-dsc",
            "-o",
            "jsonpath={.status.conditions[*].type}",
        ],
        check=False,
        capture_output=True,
        timeout=30,
    )
    return {t.strip() for t in (r.stdout or "").split() if t.strip()}


def _maas_smoke_ready(
    *,
    prereq_status: str,
    maas_status: str,
    ready_status: str,
    require_prereq_condition: bool,
) -> bool:
    if maas_status != "True" or ready_status != "True":
        return False
    if require_prereq_condition:
        return prereq_status == "True"
    return True


def _maas_gateway_annotations_ready() -> tuple[bool, str]:
    """True when gateway has Authorino TLS bootstrap annotation on the live object."""
    r = oc_run(
        [
            "get",
            "gateway",
            _GATEWAY_NAME,
            "-n",
            _GATEWAY_NS,
            "-o",
            "json",
        ],
        check=False,
        capture_output=True,
        timeout=30,
    )
    if r.returncode != 0:
        return False, f"MaaS gateway {_GATEWAY_NS}/{_GATEWAY_NAME} not present"
    try:
        doc = json.loads(r.stdout or "{}")
    except json.JSONDecodeError:
        return False, f"MaaS gateway {_GATEWAY_NS}/{_GATEWAY_NAME} returned invalid JSON"
    annotations = (doc.get("metadata") or {}).get("annotations") or {}
    value = str(annotations.get(_AUTHORINO_TLS_ANNOTATION) or "").strip().lower()
    if value in ("true", "1"):
        return True, ""
    return (
        False,
        f"{_GATEWAY_NAME} missing annotation {_AUTHORINO_TLS_ANNOTATION}=\"true\"",
    )


def _dsc_maas_prerequisites_met() -> tuple[bool, str]:
    """True when DSC reports MaaSPrerequisitesAvailable=True."""
    if "MaaSPrerequisitesAvailable" not in _dsc_condition_types():
        return False, "MaaSPrerequisitesAvailable condition not exposed"
    status, _, msg = _dsc_condition("MaaSPrerequisitesAvailable")
    if status == "True":
        return True, msg or "MaaSPrerequisitesAvailable=True"
    return False, msg or f"MaaSPrerequisitesAvailable={status or 'Unknown'}"


def maas_smoke_acceptable_for_run() -> tuple[bool, str]:
    """Shared prepare wait + pytest gate: DSC conditions or functional+annotation fallback."""
    from install.dependency_operators import maas_dependency_operators_ready

    if deps_only_install_dependencies_smoke():
        return maas_functional_smoke_ready()
    if not maas_dependency_operators_ready():
        return False, (
            "Kuadrant/Authorino dependency operators are missing "
            "(expected install-dep-operators / setup-dependencies.sh)"
        )

    require_prereq = "MaaSPrerequisitesAvailable" in _dsc_condition_types()
    prereq_status, _, prereq_msg = _dsc_condition("MaaSPrerequisitesAvailable")
    maas_status, _, maas_msg = _dsc_condition(models_as_service_ready_condition_type())
    ready_status, _, ready_msg = _dsc_condition("Ready")

    if _maas_smoke_ready(
        prereq_status=prereq_status,
        maas_status=maas_status,
        ready_status=ready_status,
        require_prereq_condition=require_prereq,
    ):
        return True, ""

    if maas_status == "True":
        func_ready, func_reason = maas_functional_smoke_ready()
        if func_ready and ready_status != "True":
            return True, (
                f"functional MaaS ready (DSC Ready=False: "
                f"{(ready_msg or func_reason or 'non-MaaS reconciling')[:120]})"
            )
        ann_ready, ann_reason = _maas_gateway_annotations_ready()
        if func_ready and ann_ready:
            if require_prereq and prereq_status != "True":
                return True, (
                    "functional MaaS ready with gateway annotations "
                    f"(MaaSPrerequisites lagging: {(prereq_msg or ann_reason)[:120]})"
                )
            return True, "functional MaaS ready with gateway annotations"

    if maas_status != "True" and ready_status == "True":
        func_ready, func_reason = maas_functional_smoke_ready()
        ann_ready, ann_reason = _maas_gateway_annotations_ready()
        if func_ready and ann_ready:
            maas_type = models_as_service_ready_condition_type()
            lag_detail = (maas_msg or f"{maas_type} not True")[:120]
            if require_prereq and prereq_status != "True":
                return True, (
                    "functional MaaS ready with gateway annotations "
                    f"(MaaSPrerequisites lagging: {(prereq_msg or ann_reason)[:120]})"
                )
            return True, (
                f"functional MaaS ready (DSC {maas_type} lagging: {lag_detail})"
            )

    if require_prereq and prereq_status != "True":
        return False, prereq_msg or "MaaSPrerequisitesAvailable not True"
    if maas_status != "True":
        return False, maas_msg or f"{models_as_service_ready_condition_type()} not True"
    if ready_status != "True":
        return False, ready_msg or "DSC Ready not True"
    return False, "MaaS smoke prerequisites not ready"


def deps_only_install_dependencies_smoke() -> bool:
    """True when MaaS smoke should use cluster checks instead of DSC conditions."""
    from suite.constants import is_test_only_product

    if not is_test_only_product(os.environ.get("PRODUCT", "")):
        return False
    if not dsc_crd_available():
        return True
    truthy = ("1", "true", "yes")
    if os.environ.get("INSTALL_DEPENDENCIES", "").strip().lower() in truthy:
        return True
    return os.environ.get("RUN_COMPONENT_CLUSTER_PREP_IN_DEP_OPERATORS", "").strip().lower() in truthy


def _maas_gateway_programmed() -> tuple[bool, str]:
    r = oc_run(
        [
            "get",
            "gateway",
            _GATEWAY_NAME,
            "-n",
            _GATEWAY_NS,
            "-o",
            'jsonpath={.status.conditions[?(@.type=="Programmed")].status}',
        ],
        check=False,
        capture_output=True,
        timeout=30,
    )
    if r.returncode != 0:
        return False, f"MaaS gateway {_GATEWAY_NS}/{_GATEWAY_NAME} not present"
    status = (r.stdout or "").strip()
    if status == "True":
        return True, ""
    return False, (
        f"MaaS gateway {_GATEWAY_NS}/{_GATEWAY_NAME} "
        f"Programmed={status or 'Unknown'}"
    )


def _service_has_https_port(svc: dict) -> bool:
    ports = (svc.get("spec") or {}).get("ports") or []
    for port in ports:
        if not isinstance(port, dict):
            continue
        if port.get("port") == 443:
            return True
        name = str(port.get("name") or "").lower()
        if name in ("https", "tls"):
            return True
    return False


def _maas_gateway_https_service_ready() -> tuple[bool, str]:
    """True when the gateway controller exposed an HTTPS service maas-api can resolve."""
    listed = oc_run(
        [
            "get",
            "svc",
            "-n",
            _GATEWAY_NS,
            "-l",
            f"gateway.networking.k8s.io/gateway-name={_GATEWAY_NAME}",
            "-o",
            "json",
        ],
        check=False,
        capture_output=True,
        timeout=30,
    )
    if listed.returncode == 0:
        try:
            doc = json.loads(listed.stdout or "{}")
        except json.JSONDecodeError:
            doc = {}
        for svc in doc.get("items") or []:
            if isinstance(svc, dict) and _service_has_https_port(svc):
                name = (svc.get("metadata") or {}).get("name") or _GATEWAY_SVC
                return True, f"{_GATEWAY_NS}/{name}"
    named = oc_run(
        ["get", "svc", _GATEWAY_SVC, "-n", _GATEWAY_NS, "-o", "json"],
        check=False,
        capture_output=True,
        timeout=30,
    )
    if named.returncode == 0:
        try:
            svc = json.loads(named.stdout or "{}")
        except json.JSONDecodeError:
            svc = {}
        if _service_has_https_port(svc):
            return True, f"{_GATEWAY_NS}/{_GATEWAY_SVC}"
    return (
        False,
        f"no gateway-owned HTTPS service for {_GATEWAY_NS}/{_GATEWAY_NAME}",
    )


def _maas_gateway_ready_for_smoke() -> tuple[bool, str]:
    """Gateway ready for MaaS pytest: Programmed=True, or EPHC functional fallback."""
    programmed, prog_reason = _maas_gateway_programmed()
    if programmed:
        return True, ""
    if cluster_source_is_ephc():
        ann_ready, ann_reason = _maas_gateway_annotations_ready()
        if ann_ready:
            return True, (
                "EPHC: gateway functional (Authorino TLS annotated) without "
                f"Programmed=True ({prog_reason})"
            )
        dsc_met, dsc_msg = _dsc_maas_prerequisites_met()
        if dsc_met:
            return True, (
                "EPHC: trusting DSC MaaSPrerequisitesMet without "
                f"Programmed=True ({(dsc_msg or prog_reason)[:120]})"
            )
        return False, ann_reason or prog_reason
    return False, prog_reason


def maas_functional_smoke_ready() -> tuple[bool, str]:
    """Functional MaaS readiness when DSC conditions are unavailable."""
    from install.dependency_operators import maas_dependency_operators_ready

    if not maas_dependency_operators_ready():
        return False, (
            "Kuadrant/Authorino dependency operators are missing "
            "(expected install-dep-operators / setup-dependencies.sh)"
        )
    gateway = oc_run(
        ["get", "gateway", _GATEWAY_NAME, "-n", _GATEWAY_NS],
        check=False,
        capture_output=True,
        timeout=30,
    )
    if gateway.returncode != 0:
        return False, f"MaaS gateway {_GATEWAY_NS}/{_GATEWAY_NAME} not present"
    gateway_ready, reason = _maas_gateway_ready_for_smoke()
    if not gateway_ready:
        return False, reason
    if not _secret_exists(_MAAS_APPS_NS, _MAAS_DB_SECRET):
        return False, f"MaaS DB secret {_MAAS_APPS_NS}/{_MAAS_DB_SECRET} not present"
    from components.maas_billing.auth import _authorino_deployment_ready, _authorino_namespace

    authorino_ns = _authorino_namespace()
    if not _authorino_deployment_ready(authorino_ns):
        return False, f"Authorino deployment not ready in {authorino_ns}"
    return True, ""
