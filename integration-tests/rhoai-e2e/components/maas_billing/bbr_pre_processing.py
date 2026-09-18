"""BBR pre-auth payload-processing for RHOAI 3.5 EA.x (maas-controller gap)."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

from install.dsc_install import oc_run

from components.maas_billing.common import (
    _GATEWAY_NS,
    _MODELS_AS_SERVICE_DEST,
    _MODELS_AS_SERVICE_REPO,
    _dsc_condition,
    models_as_service_ready_condition_type,
)

_BBR_PRE_DEPLOY = "payload-pre-processing"
_BBR_POST_DEPLOY = "payload-processing"
_MAAS_CONTROLLER_SA_NS = "redhat-ods-applications"
_MAAS_CONTROLLER_SA = "maas-controller"
_OLMINSTALL_MAAS_INGRESS_RBAC_ROLE = "olminstall-maas-controller-payload-processing"
_OLMINSTALL_MANAGED_BY = "rhoai-e2e"
_HPA_RBAC_RULE = {
    "apiGroups": ["autoscaling"],
    "resources": ["horizontalpodautoscalers"],
    "verbs": ["create", "delete", "get", "list", "patch", "update", "watch"],
}
_IPP_PRE_FILTER = "envoy.filters.http.ext_proc.ipp-pre"
_BBR_PRE_FILTER_LEGACY = "envoy.filters.http.ext_proc.bbr-pre"
_BBR_POST_FILTER = "envoy.filters.http.ext_proc.bbr"
_MAS_BBR_REF = "a787174bb6886702c75139d7040fffa04f1c0522"
_ENVOY_FILTER_NAME = "payload-processing"


def _envoyfilter_crd_available() -> bool:
    r = oc_run(
        ["api-resources", "--api-group=networking.istio.io", "-o", "name"],
        check=False,
        capture_output=True,
        timeout=30,
    )
    if r.returncode != 0:
        return False
    return any(
        line.strip().endswith("envoyfilters.networking.istio.io")
        for line in (r.stdout or "").splitlines()
    )


def _resolve_payload_processing_image() -> str:
    """Prefer post-auth payload-processing; fall back to pre-auth when controller has not created post yet."""
    post_image = _deployment_image(_GATEWAY_NS, _BBR_POST_DEPLOY)
    if post_image:
        return post_image
    return _deployment_image(_GATEWAY_NS, _BBR_PRE_DEPLOY)


def _deployment_image(namespace: str, name: str) -> str:
    r = oc_run(
        [
            "get",
            "deployment",
            name,
            "-n",
            namespace,
            "-o",
            "jsonpath={.spec.template.spec.containers[0].image}",
        ],
        check=False,
        capture_output=True,
        timeout=30,
    )
    if r.returncode != 0:
        return ""
    return (r.stdout or "").strip()


def _envoy_filter_stage_names() -> list[str]:
    r = oc_run(
        [
            "get",
            "envoyfilter",
            _ENVOY_FILTER_NAME,
            "-n",
            _GATEWAY_NS,
            "-o",
            "jsonpath={.spec.configPatches[*].patch.value.name}",
        ],
        check=False,
        capture_output=True,
        timeout=30,
    )
    if r.returncode != 0:
        return []
    return [n.strip() for n in (r.stdout or "").split() if n.strip()]


def _pre_auth_filter_configured(stages: list[str]) -> bool:
    return _IPP_PRE_FILTER in stages


def _legacy_pre_auth_filter_only(stages: list[str]) -> bool:
    return _BBR_PRE_FILTER_LEGACY in stages and _IPP_PRE_FILTER not in stages


def _normalize_envoy_filter_spec(spec: dict) -> dict:
    """MAS manifests may still name the pre-auth stage bbr-pre; tests expect ipp-pre."""
    text = json.dumps(spec)
    text = text.replace(_BBR_PRE_FILTER_LEGACY, _IPP_PRE_FILTER)
    return json.loads(text)


def _clone_models_as_a_service_bbr_ref() -> Path:
    dest = _MODELS_AS_SERVICE_DEST.parent / "models-as-a-service-bbr-pre"
    if dest.is_dir() and (dest / ".git").is_dir():
        subprocess.run(
            ["git", "fetch", "--depth", "1", "origin", _MAS_BBR_REF],
            cwd=dest,
            check=False,
            capture_output=True,
            timeout=120,
        )
        subprocess.run(
            ["git", "checkout", "--force", _MAS_BBR_REF],
            cwd=dest,
            check=False,
            capture_output=True,
            timeout=60,
        )
        return dest
    dest.mkdir(parents=True, exist_ok=True)
    proc = subprocess.run(
        [
            "git",
            "clone",
            "--depth",
            "1",
            _MODELS_AS_SERVICE_REPO,
            str(dest),
        ],
        check=False,
        capture_output=True,
        timeout=300,
    )
    if proc.returncode != 0:
        err = (proc.stderr or proc.stdout or b"").decode(errors="replace").strip()
        raise RuntimeError(f"Could not clone {_MODELS_AS_SERVICE_REPO}: {err or 'unknown error'}")
    subprocess.run(
        ["git", "fetch", "--depth", "1", "origin", _MAS_BBR_REF],
        cwd=dest,
        check=True,
        capture_output=True,
        timeout=120,
    )
    subprocess.run(
        ["git", "checkout", "--force", _MAS_BBR_REF],
        cwd=dest,
        check=True,
        capture_output=True,
        timeout=60,
    )
    return dest


def _kustomize_pre_processing_manifests(repo: Path, image: str) -> str:
    overlay = repo / "deployment/base/payload-processing/pre-processing"
    if not overlay.is_dir():
        raise FileNotFoundError(f"Missing BBR pre-processing overlay: {overlay}")
    built = oc_run(
        ["kustomize", str(overlay)],
        check=True,
        capture_output=True,
        timeout=120,
    )
    doc = (built.stdout or "").replace("image: payload-processing", f"image: {image}")
    return doc


def _patch_envoy_filter_bbr_stages(repo: Path) -> None:
    manifest = repo / "deployment/base/payload-processing/manager/envoy-filter.yaml"
    if not manifest.is_file():
        raise FileNotFoundError(f"Missing BBR EnvoyFilter manifest: {manifest}")
    desired = oc_run(
        ["create", "--dry-run=client", "-o", "json", "-f", str(manifest)],
        check=True,
        capture_output=True,
        timeout=30,
    )
    desired_obj = json.loads(desired.stdout or "{}")
    current = oc_run(
        ["get", "envoyfilter", _ENVOY_FILTER_NAME, "-n", _GATEWAY_NS, "-o", "json"],
        check=True,
        capture_output=True,
        timeout=30,
    )
    current_obj = json.loads(current.stdout or "{}")
    current_obj["spec"] = _normalize_envoy_filter_spec(desired_obj["spec"])
    apply = oc_run(
        ["apply", "-f", "-"],
        stdin_text=json.dumps(current_obj),
        check=False,
        capture_output=True,
        timeout=60,
    )
    if apply.returncode != 0:
        err = (apply.stderr or apply.stdout or "").strip()
        raise RuntimeError(f"Could not patch {_GATEWAY_NS}/{_ENVOY_FILTER_NAME}: {err or 'unknown error'}")


def _models_as_service_selector_conflict() -> bool:
    """True when platform reconcile cannot patch openshift-ingress/payload-pre-processing."""
    status, _, msg = _dsc_condition(models_as_service_ready_condition_type())
    if status == "True":
        return False
    combined = f"{msg}".lower()
    return (
        "payload-pre-processing" in combined
        and "spec.selector" in combined
        and "immutable" in combined
    )


_MAAS_INGRESS_CLEANUP_DEPLOYS = (_BBR_PRE_DEPLOY, _BBR_POST_DEPLOY)


def _maas_controller_can_manage_openshift_ingress_hpa() -> bool:
    """True when maas-controller has every verb required for openshift-ingress HPAs."""
    for verb in _HPA_RBAC_RULE["verbs"]:
        r = oc_run(
            [
                "auth",
                "can-i",
                verb,
                "horizontalpodautoscalers.autoscaling",
                "--as",
                f"system:serviceaccount:{_MAAS_CONTROLLER_SA_NS}:{_MAAS_CONTROLLER_SA}",
                "-n",
                _GATEWAY_NS,
            ],
            check=False,
            capture_output=True,
            timeout=30,
        )
        if r.returncode != 0 or (r.stdout or "").strip().lower() != "yes":
            return False
    return True


def ensure_maas_controller_openshift_ingress_hpa_rbac() -> None:
    """Grant maas-controller HPA access in openshift-ingress (trimmed RHOAI ClusterRole gap)."""
    if _maas_controller_can_manage_openshift_ingress_hpa():
        print(
            f"✓ maas-controller can manage HPAs in {_GATEWAY_NS}",
            flush=True,
        )
        return
    role = {
        "apiVersion": "rbac.authorization.k8s.io/v1",
        "kind": "Role",
        "metadata": {
            "name": _OLMINSTALL_MAAS_INGRESS_RBAC_ROLE,
            "namespace": _GATEWAY_NS,
            "labels": {"app.kubernetes.io/managed-by": _OLMINSTALL_MANAGED_BY},
        },
        "rules": [_HPA_RBAC_RULE],
    }
    role_binding = {
        "apiVersion": "rbac.authorization.k8s.io/v1",
        "kind": "RoleBinding",
        "metadata": {
            "name": _OLMINSTALL_MAAS_INGRESS_RBAC_ROLE,
            "namespace": _GATEWAY_NS,
            "labels": {"app.kubernetes.io/managed-by": _OLMINSTALL_MANAGED_BY},
        },
        "subjects": [
            {
                "kind": "ServiceAccount",
                "name": _MAAS_CONTROLLER_SA,
                "namespace": _MAAS_CONTROLLER_SA_NS,
            }
        ],
        "roleRef": {
            "apiGroup": "rbac.authorization.k8s.io",
            "kind": "Role",
            "name": _OLMINSTALL_MAAS_INGRESS_RBAC_ROLE,
        },
    }
    manifest = json.dumps(role)
    apply_role = oc_run(
        ["apply", "-f", "-"],
        stdin_text=manifest,
        check=False,
        capture_output=True,
        timeout=60,
    )
    if apply_role.returncode != 0:
        err = (apply_role.stderr or apply_role.stdout or "").strip()
        raise RuntimeError(
            f"Could not grant maas-controller HPA Role in {_GATEWAY_NS}: "
            f"{err or 'unknown error'}"
        )
    apply_binding = oc_run(
        ["apply", "-f", "-"],
        stdin_text=json.dumps(role_binding),
        check=False,
        capture_output=True,
        timeout=60,
    )
    if apply_binding.returncode != 0:
        err = (apply_binding.stderr or apply_binding.stdout or "").strip()
        raise RuntimeError(
            f"Could not grant maas-controller HPA RoleBinding in {_GATEWAY_NS}: "
            f"{err or 'unknown error'}"
        )
    if not _maas_controller_can_manage_openshift_ingress_hpa():
        raise RuntimeError(
            f"maas-controller still cannot manage HPAs in {_GATEWAY_NS} after RBAC apply"
        )
    print(
        f"✓ Granted maas-controller HPA RBAC in {_GATEWAY_NS} "
        f"(Role/{_OLMINSTALL_MAAS_INGRESS_RBAC_ROLE})",
        flush=True,
    )


def _delete_stale_maas_ingress_hpa(name: str) -> None:
    proc = oc_run(
        [
            "delete",
            "horizontalpodautoscaler",
            name,
            "-n",
            _GATEWAY_NS,
            "--ignore-not-found",
            "--wait=false",
        ],
        check=False,
        capture_output=True,
        timeout=120,
    )
    if proc.returncode == 0 and "deleted" in f"{proc.stdout or ''}{proc.stderr or ''}".lower():
        print(f"✓ Removed stale MaaS ingress HPA {_GATEWAY_NS}/{name}", flush=True)


def cleanup_stale_maas_ingress_workloads() -> None:
    """Delete MaaS ingress Deployments left on pooled clusters after operator cleanup."""
    ensure_maas_controller_openshift_ingress_hpa_rbac()
    for name in _MAAS_INGRESS_CLEANUP_DEPLOYS:
        proc = oc_run(
            [
                "delete",
                "deployment",
                name,
                "-n",
                _GATEWAY_NS,
                "--ignore-not-found",
                "--wait=false",
            ],
            check=False,
            capture_output=True,
            timeout=120,
        )
        if proc.returncode == 0 and "deleted" in f"{proc.stdout or ''}{proc.stderr or ''}".lower():
            print(f"✓ Removed stale MaaS ingress deployment {_GATEWAY_NS}/{name}", flush=True)
    for name in _MAAS_INGRESS_CLEANUP_DEPLOYS:
        _delete_stale_maas_ingress_hpa(name)


def _wait_models_as_service_after_repair() -> None:
    from components.maas_billing.timeouts import maas_resync_timeout_sec
    from components.maas_billing.wait import _wait_for_dsc_component_ready

    try:
        _wait_for_dsc_component_ready(
            condition_type=models_as_service_ready_condition_type(),
            timeout_sec=maas_resync_timeout_sec(),
        )
    except RuntimeError as exc:
        print(
            f"WARN: {models_as_service_ready_condition_type()} not True after repair ({exc})",
            flush=True,
        )


def repair_payload_pre_processing_selector_conflict() -> bool:
    """Delete stale pre-auth Deployment so operator or rhoai-e2e can recreate it."""
    if not _models_as_service_selector_conflict():
        return False
    exists = (
        oc_run(
            ["get", "deployment", _BBR_PRE_DEPLOY, "-n", _GATEWAY_NS],
            check=False,
            capture_output=True,
            timeout=30,
        ).returncode
        == 0
    )
    if not exists:
        return False
    print(
        f"Deleting stale {_GATEWAY_NS}/{_BBR_PRE_DEPLOY} "
        "(ModelsAsServiceReady blocked on immutable spec.selector)...",
        flush=True,
    )
    oc_run(
        ["delete", "deployment", _BBR_PRE_DEPLOY, "-n", _GATEWAY_NS, "--wait=true"],
        check=False,
        capture_output=True,
        timeout=180,
    )
    _wait_models_as_service_after_repair()
    return True


def ensure_maas_bbr_pre_processing() -> None:
    """Apply payload-pre-processing and bbr-pre EnvoyFilter when EA.x controller omits them."""
    ensure_maas_controller_openshift_ingress_hpa_rbac()
    repair_payload_pre_processing_selector_conflict()
    if not _envoyfilter_crd_available():
        print(
            "WARN: EnvoyFilter CRD unavailable (Service Mesh not ready); "
            "deferring BBR pre-processing until MaaS gateway stack reconciles",
            flush=True,
        )
        return
    stages = _envoy_filter_stage_names()
    pre_ready = (
        oc_run(
            ["get", "deployment", _BBR_PRE_DEPLOY, "-n", _GATEWAY_NS],
            check=False,
            capture_output=True,
            timeout=30,
        ).returncode
        == 0
    )
    if _pre_auth_filter_configured(stages) and pre_ready:
        print(f"✓ BBR pre-processing ready ({_GATEWAY_NS}/{_BBR_PRE_DEPLOY}, {_IPP_PRE_FILTER})", flush=True)
        return

    post_image = _resolve_payload_processing_image()
    if not post_image:
        print(
            f"WARN: {_GATEWAY_NS}/{_BBR_POST_DEPLOY} and {_BBR_PRE_DEPLOY} have no image yet; "
            "deferring BBR pre-processing until modelsAsService reconciles",
            flush=True,
        )
        return

    repo = _clone_models_as_a_service_bbr_ref()
    if not pre_ready:
        print(f"Applying {_GATEWAY_NS}/{_BBR_PRE_DEPLOY} (BBR pre-auth)...", flush=True)
        manifests = _kustomize_pre_processing_manifests(repo, post_image)
        apply = oc_run(
            ["apply", "-n", _GATEWAY_NS, "-f", "-"],
            stdin_text=manifests,
            check=False,
            capture_output=True,
            timeout=120,
        )
        if apply.returncode != 0:
            err = (apply.stderr or apply.stdout or "").strip()
            raise RuntimeError(f"Could not apply BBR pre-processing: {err or 'unknown error'}")

    if not _pre_auth_filter_configured(stages) or _BBR_POST_FILTER not in stages:
        print(f"Patching {_GATEWAY_NS}/{_ENVOY_FILTER_NAME} for {_IPP_PRE_FILTER}...", flush=True)
        _patch_envoy_filter_bbr_stages(repo)

    print(f"✓ BBR pre-processing configured ({_IPP_PRE_FILTER} + {_BBR_POST_DEPLOY})", flush=True)
