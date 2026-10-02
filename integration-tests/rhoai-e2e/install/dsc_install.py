"""DSCInitialization / DataScienceCluster setup for install and component smoke."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, NoReturn

from _bootstrap import ensure_rhoai_e2e_path

ensure_rhoai_e2e_path()

from suite.component_version_gate import (
    probe_operator_version_from_cluster,
    resolve_operator_version_for_gates,
    rhoai_version_at_least,
)
from install.dsc_install_policy import resolve_managed_dsc_keys
from k8s.oc_util import run_oc

# Minimal DSCI + DSC for BVT. The operator must be installed first; these CRs
# activate RHOAI so the opendatahub-tests conftest can discover the cluster.
def _initial_dsci_servicemesh_state(components_csv: str) -> str:
    """Managed when gateway/dashboard smoke needs Istio; Removed for RawDeployment KServe path."""
    if _install_requires_dashboard_gateway():
        return "Managed"
    if _smoke_components_need_servicemesh(components_csv) and not smoke_components_use_kserve_raw_deployment(
        components_csv
    ):
        return "Managed"
    ids = {c.strip() for c in components_csv.split(",") if c.strip()}
    if "dashboard_cypress" in ids:
        return "Managed"
    return "Removed"


def _dsci_yaml(components_csv: str = "") -> str:
    sm_state = _initial_dsci_servicemesh_state(components_csv)
    return f"""\
apiVersion: dscinitialization.opendatahub.io/v1
kind: DSCInitialization
metadata:
  name: default-dsci
spec:
  applicationsNamespace: redhat-ods-applications
  monitoring:
    managementState: Managed
    namespace: redhat-ods-monitoring
  serviceMesh:
    managementState: {sm_state}
  trustedCABundle:
    customCABundle: ""
    managementState: Removed
"""

_DSC_YAML = """\
apiVersion: datasciencecluster.opendatahub.io/v2
kind: DataScienceCluster
metadata:
  name: default-dsc
spec:
  components:
    dashboard:
      managementState: Managed
    workbenches:
      managementState: Managed
    modelmeshserving:
      managementState: Removed
    datasciencepipelines:
      managementState: Removed
    kserve:
      managementState: Removed
    codeflare:
      managementState: Removed
    ray:
      managementState: Removed
    kueue:
      managementState: Removed
    modelregistry:
      managementState: Removed
    trainingoperator:
      managementState: Removed
    trustyai:
      managementState: Removed
    modelcontroller:
      managementState: Removed
"""

_DSC_CRD = "datascienceclusters.datasciencecluster.opendatahub.io"
_DSCI_CRD = "dscinitializations.dscinitialization.opendatahub.io"


def _probe_dsc_resource_kind() -> tuple[str, bool]:
    """Resolve oc resource kind; second value True when api-resources probe succeeded."""
    proc = oc_run(
        ["api-resources", "--api-group=datasciencecluster.opendatahub.io", "-o", "name"],
        check=False,
        capture_output=True,
        timeout=30,
    )
    if proc.returncode != 0:
        return "datascienceclusters", False
    stdout = (proc.stdout or "").lower()
    if "datascienceclusters" in stdout:
        return "datascienceclusters", True
    return "datasciencecluster", True


def dsc_crd_available() -> bool:
    """True when the cluster exposes the DataScienceCluster CRD."""
    kind, probe_ok = _probe_dsc_resource_kind()
    if not probe_ok:
        return False
    return kind == "datascienceclusters"


_dsc_resource_kind: str | None = None


def reset_dsc_resource_kind_cache() -> None:
    """Clear cached kind after transient api-resources failure or restore retry."""
    global _dsc_resource_kind
    _dsc_resource_kind = None


def dsc_resource_kind(*, force_refresh: bool = False) -> str:
    """oc resource name for DataScienceCluster (plural on CRD v2+ clusters)."""
    global _dsc_resource_kind
    if force_refresh:
        reset_dsc_resource_kind_cache()
    if _dsc_resource_kind is not None:
        return _dsc_resource_kind
    kind, probe_ok = _probe_dsc_resource_kind()
    if probe_ok:
        _dsc_resource_kind = kind
    return kind

_DSC_COMPONENT_KEYS = (
    "dashboard",
    "workbenches",
    "trainingoperator",
    "trainer",
    "aipipelines",
    "kserve",
    "aigateway",
    "ray",
    "kueue",
    "modelregistry",
    "trustyai",
    "llamastackoperator",
    "mlflowoperator",
    "ogx",
    "sparkoperator",
    "codeflare",
    "feastoperator",
)


def fail(message: str = "") -> NoReturn:
    if message:
        print(message)
    p = os.environ.get("INSTALL_STATUS_PATH")
    if p:
        try:
            Path(p).write_text("FAILED", encoding="utf-8")
        except OSError:
            pass
    sys.exit(1)


def oc_run(
    args: list[str],
    *,
    check: bool = False,
    capture_output: bool = True,
    text: bool = True,
    stdin_text: str | None = None,
    timeout: float | None = 180,
) -> subprocess.CompletedProcess[str]:
    return run_oc(
        args,
        check=check,
        capture_output=capture_output,
        text=text,
        stdin_text=stdin_text,
        timeout=timeout,
        on_timeout=lambda msg: fail(f"❌ {msg}"),
    )


def components_need_models_as_service(component_ids: set[str]) -> bool:
    return bool(component_ids & {"model_server", "model_runtime", "maas_billing"})


def smoke_enables_models_as_service(component_ids: set[str]) -> bool:
    """DSC patch should enable MaaS for these smoke catalog ids."""
    return components_need_models_as_service(component_ids)


def _probe_update_channel_from_cluster() -> str:
    """Best-effort rhods-operator subscription channel (Tekton prep often omits UPDATE_CHANNEL)."""
    if not os.environ.get("KUBECONFIG", "").strip():
        return ""
    op_ns = os.environ.get("OPERATOR_NAMESPACE", "").strip() or "redhat-ods-operator"
    op_name = (os.environ.get("OPERATOR_NAME", "") or "rhods-operator").strip()
    r = run_oc(
        ["get", "subscription", op_name, "-n", op_ns, "-o", "jsonpath={.spec.channel}"],
        check=False,
        capture_output=True,
        timeout=20,
    )
    if r.returncode != 0:
        return ""
    return (r.stdout or "").strip()


_aigateway_maas_crd_probed: bool | None = None
_aigateway_maas_crd_supported: bool = False


def _dsc_crd_supports_aigateway_models_as_a_service() -> bool:
    """True when DSC exposes spec.components.aigateway.modelsAsAService (not all 3.5 builds)."""
    global _aigateway_maas_crd_probed, _aigateway_maas_crd_supported
    if _aigateway_maas_crd_probed is not None:
        return _aigateway_maas_crd_supported
    r = oc_run(
        ["explain", "datasciencecluster.spec.components.aigateway.modelsAsAService"],
        check=False,
        capture_output=True,
        timeout=30,
    )
    if r.returncode != 0:
        return False
    out = f"{r.stdout or ''}\n{r.stderr or ''}"
    _aigateway_maas_crd_supported = "FIELD: modelsAsAService" in out
    _aigateway_maas_crd_probed = True
    if not _aigateway_maas_crd_supported:
        print(
            "NOTE: DSC has no aigateway.modelsAsAService field; using kserve.modelsAsService for MaaS",
            flush=True,
        )
    return _aigateway_maas_crd_supported


def uses_aigateway_models_as_a_service(operator_version: str = "") -> bool:
    """RHOAI 3.5+ may move MaaS to aigateway.modelsAsAService when the DSC CRD exposes it."""
    if not _dsc_crd_supports_aigateway_models_as_a_service():
        return False
    ver = (operator_version or _resolve_operator_version_for_dsc()).strip()
    if ver and ver != "(unknown)":
        return rhoai_version_at_least(ver, "3.5")
    # Before CSV/OPERATOR_VERSION is written (early install-dep-operators), infer from pipeline params.
    channel = (
        os.environ.get("RHOAI_CHANNEL", "").strip()
        or os.environ.get("UPDATE_CHANNEL", "").strip()
        or _probe_update_channel_from_cluster()
    )
    if channel.startswith("stable-3.5") or channel == "beta":
        return rhoai_version_at_least("3.5", "3.5")
    if channel.startswith("stable-3."):
        channel_ver = channel.removeprefix("stable-")
        if rhoai_version_at_least(channel_ver, "3.5"):
            return True
    raw = os.environ.get("RHOAI_VERSION", "").strip()
    if raw:
        normalized = raw.removeprefix("rhoai-").replace("-", ".")
        if rhoai_version_at_least(normalized, "3.5"):
            return True
    return False


def _install_requires_dashboard_gateway() -> bool:
    """verify-operator-ready waits for dashboard route on every rhoai/odh install."""
    return os.environ.get("PRODUCT", "").strip().lower() in ("rhoai", "odh")


def _smoke_components_need_servicemesh(components_csv: str) -> bool:
    ids = {c.strip() for c in components_csv.split(",") if c.strip()}
    return bool(ids & {"model_server", "model_runtime", "maas_billing", "ai_safety", "dashboard_cypress"})


def smoke_components_use_kserve_raw_deployment(components_csv: str) -> bool:
    """EPHC smoke uses KServe RawDeployment to avoid Serverless/Service Mesh operators."""
    ids = {c.strip() for c in components_csv.split(",") if c.strip()}
    return bool(ids & {"model_server", "model_runtime", "maas_billing", "ai_safety"})


def _kserve_raw_deployment_patch() -> dict[str, Any]:
    return {
        "defaultDeploymentMode": "RawDeployment",
        "serving": {"managementState": "Removed", "name": "knative-serving"},
    }


def _kserve_component_block(
    *,
    managed: bool,
    enable_models_as_service: bool,
    use_raw_deployment: bool = False,
    operator_version: str = "",
) -> list[str]:
    lines = [
        "    kserve:",
        f"      managementState: {'Managed' if managed else 'Removed'}",
    ]
    if managed and use_raw_deployment:
        lines.extend(
            [
                "      defaultDeploymentMode: RawDeployment",
                "      serving:",
                "        managementState: Removed",
                "        name: knative-serving",
            ]
        )
    if enable_models_as_service and not uses_aigateway_models_as_a_service(operator_version):
        lines.extend(
            [
                "      modelsAsService:",
                "        managementState: Managed",
            ]
        )
    return lines


def _aigateway_component_block(
    *,
    managed: bool,
    enable_models_as_a_service: bool,
) -> list[str]:
    lines = [
        "    aigateway:",
        f"      managementState: {'Managed' if managed else 'Removed'}",
    ]
    if managed and enable_models_as_a_service:
        lines.extend(
            [
                "      modelsAsAService:",
                "        managementState: Managed",
            ]
        )
    return lines


def _resolve_operator_version_for_dsc() -> str:
    """Installed CSV version for install-time DSC policy (after operator install)."""
    path = os.environ.get("OPERATOR_VERSION_PATH", "").strip()
    if path:
        try:
            ver = Path(path).read_text(encoding="utf-8").strip()
            if ver and ver != "(unknown)":
                return ver
        except OSError:
            pass
    ver = os.environ.get("OPERATOR_VERSION", "").strip()
    if ver and ver != "(unknown)":
        return ver
    gate_ver = resolve_operator_version_for_gates()
    if gate_ver and gate_ver != "(unknown)":
        return gate_ver
    return probe_operator_version_from_cluster()


def _dsc_smoke_managed_components(
    components_csv: str,
    *,
    operator_version: str = "",
    defer_for_install: bool = False,
) -> set[str]:
    """DSC components to set Managed for selected smoke catalog ids (see rhoai-e2e-dsc-install.yaml)."""
    ver = operator_version or (_resolve_operator_version_for_dsc() if defer_for_install else "")
    managed = set(resolve_managed_dsc_keys(components_csv, ver, for_install=defer_for_install))
    if _install_requires_dashboard_gateway():
        managed.add("dashboard")
    return managed


def _build_dsc_smoke_yaml(
    components_csv: str,
    *,
    enable_models_as_service: bool = True,
    use_kserve_raw_deployment: bool | None = None,
    defer_for_install: bool = False,
    operator_version: str = "",
) -> str:
    """Build DSC for component smoke from COMPONENTS_CSV (enables only what tests need).

    When ``enable_models_as_service`` is False (operator install / BVT gate), kserve may be
    Managed but modelsAsService stays off until prepare-components-prerequisites creates
    maas-default-gateway and patches MaaS on.
    """
    managed = _dsc_smoke_managed_components(
        components_csv,
        operator_version=operator_version,
        defer_for_install=defer_for_install,
    )
    ids = {c.strip() for c in components_csv.split(",") if c.strip()}
    enable_maas = enable_models_as_service and smoke_enables_models_as_service(ids)
    if use_kserve_raw_deployment is None:
        use_kserve_raw_deployment = smoke_components_use_kserve_raw_deployment(components_csv)
    lines = [
        "apiVersion: datasciencecluster.opendatahub.io/v2",
        "kind: DataScienceCluster",
        "metadata:",
        "  name: default-dsc",
        "spec:",
        "  components:",
    ]
    op_ver = operator_version or (_resolve_operator_version_for_dsc() if defer_for_install else "")
    use_aigateway_maas = uses_aigateway_models_as_a_service(op_ver)
    for key in _DSC_COMPONENT_KEYS:
        if key == "kserve":
            lines.extend(
                _kserve_component_block(
                    managed=key in managed,
                    enable_models_as_service=enable_maas,
                    use_raw_deployment=use_kserve_raw_deployment,
                    operator_version=op_ver,
                )
            )
            continue
        if key == "aigateway":
            if not use_aigateway_maas:
                continue
            lines.extend(
                _aigateway_component_block(
                    managed=enable_maas or key in managed,
                    enable_models_as_a_service=enable_maas,
                )
            )
            continue
        state = "Managed" if key in managed else "Removed"
        lines.append(f"    {key}:")
        lines.append(f"      managementState: {state}")
    return "\n".join(lines) + "\n"


def _dsc_yaml_for_install() -> str:
    """Use expanded DSC when component smoke is selected (COMPONENTS_CSV from parse-pipeline-tests)."""
    csv = os.environ.get("COMPONENTS_CSV", "").strip()
    if csv:
        return _build_dsc_smoke_yaml(
            csv,
            enable_models_as_service=False,
            defer_for_install=True,
            operator_version=_resolve_operator_version_for_dsc(),
        )
    return _DSC_YAML


def _crd_served_versions(crd_name: str) -> list[str]:
    r = oc_run(["get", "crd", crd_name, "-o", "json"], check=False, capture_output=True, timeout=30)
    if r.returncode != 0:
        return []
    try:
        data = json.loads(r.stdout or "{}")
    except json.JSONDecodeError:
        return []
    served: list[str] = []
    for version in data.get("spec", {}).get("versions") or []:
        if version.get("served") and version.get("name"):
            served.append(str(version["name"]))
    return served


def _resolve_dsc_api_version() -> str:
    served = _crd_served_versions(_DSC_CRD)
    for preferred in ("v2", "v1", "v1alpha1"):
        if preferred in served:
            print(
                f"Using DataScienceCluster apiVersion datasciencecluster.opendatahub.io/{preferred} "
                f"(CRD served: {', '.join(served)})"
            )
            return preferred
    fail(f"No served DataScienceCluster API version in {_DSC_CRD} (served: {served or 'none'})")
    raise AssertionError("unreachable")


def _with_dsc_api_version(yaml_doc: str, version: str) -> str:
    prefix = "apiVersion: datasciencecluster.opendatahub.io/"
    lines = [f"{prefix}{version}" if line.startswith(prefix) else line for line in yaml_doc.splitlines()]
    return "\n".join(lines) + "\n"


def _crd_available(crd_name: str) -> bool:
    r = oc_run(["get", "crd", crd_name], check=False, capture_output=True, timeout=30)
    return r.returncode == 0


def _crd_established(crd_name: str) -> bool:
    r = oc_run(
        [
            "get",
            "crd",
            crd_name,
            "-o",
            "jsonpath={.status.conditions[?(@.type=='Established')].status}",
        ],
        check=False,
        capture_output=True,
        timeout=30,
    )
    return r.returncode == 0 and (r.stdout or "").strip() == "True"


def _wait_for_crd_established(crd_name: str, *, timeout_sec: int = 900) -> None:
    deadline = time.time() + timeout_sec
    while time.time() < deadline:
        if not _crd_available(crd_name):
            print(f"Waiting for {crd_name} CRD...")
            time.sleep(10)
            continue
        if _crd_established(crd_name):
            print(f"✓ {crd_name} CRD Established")
            return
        print(f"Waiting for {crd_name} CRD Established...")
        time.sleep(10)
    fail(f"{crd_name} CRD not Established after {timeout_sec}s")


def _cr_exists(kind: str, name: str) -> bool:
    r = oc_run(["get", kind, name], check=False, capture_output=True, timeout=30)
    return r.returncode == 0


def _apply_cr(kind: str, name: str, yaml_doc: str, *, timeout_sec: int = 300) -> None:
    deadline = time.time() + timeout_sec
    last_err = ""
    while time.time() < deadline:
        r = oc_run(["apply", "-f", "-"], stdin_text=yaml_doc, check=False, capture_output=True, timeout=60)
        if r.returncode == 0:
            print(f"✓ Applied {kind}/{name}")
            return
        last_err = (r.stderr or r.stdout or "").strip()
        err_lower = last_err.lower()
        if "resource mapping not found" in err_lower or "no matches for kind" in err_lower:
            print(f"Waiting to apply {kind}/{name} (API not ready)...")
            time.sleep(10)
            continue
        if "no endpoints available for service" in err_lower or "failed calling webhook" in err_lower:
            print(f"Waiting for admission webhook before apply {kind}/{name}...")
            time.sleep(10)
            continue
        print(f"⚠ Could not apply {kind}/{name}: {last_err}", file=sys.stderr)
        fail(f"oc apply failed for {kind}/{name}: {last_err or 'unknown error'}")
    fail(f"oc apply timed out for {kind}/{name}: {last_err or 'API not ready'}")


def _sync_dsc_smoke_components(
    components_csv: str,
    *,
    enable_models_as_service: bool = True,
) -> None:
    """Align default-dsc component managementState with the smoke COMPONENTS selection."""
    operator_version = _resolve_operator_version_for_dsc()
    managed = _dsc_smoke_managed_components(
        components_csv,
        defer_for_install=True,
        operator_version=operator_version,
    )
    ids = {c.strip() for c in components_csv.split(",") if c.strip()}
    use_raw = smoke_components_use_kserve_raw_deployment(components_csv)
    use_aigateway_maas = uses_aigateway_models_as_a_service(operator_version)
    enable_maas = enable_models_as_service and smoke_enables_models_as_service(ids)
    components_patch: dict[str, Any] = {}
    for key in _DSC_COMPONENT_KEYS:
        if key == "kserve":
            kserve_patch: dict[str, Any] = {
                "managementState": "Managed" if key in managed else "Removed",
            }
            if key in managed and use_raw:
                kserve_patch.update(_kserve_raw_deployment_patch())
            if enable_maas and not use_aigateway_maas:
                kserve_patch["modelsAsService"] = {"managementState": "Managed"}
            components_patch[key] = kserve_patch
            continue
        if key == "aigateway":
            if not use_aigateway_maas:
                continue
            components_patch[key] = {
                "managementState": "Managed"
                if (enable_maas or key in managed)
                else "Removed",
            }
            if enable_maas:
                components_patch[key]["modelsAsAService"] = {"managementState": "Managed"}
            continue
        components_patch[key] = {
            "managementState": "Managed" if key in managed else "Removed",
        }
    patch_doc = json.dumps({"spec": {"components": components_patch}})
    r = oc_run(
        ["patch", "datasciencecluster", "default-dsc", "--type=merge", "-p", patch_doc],
        check=False,
        capture_output=True,
        timeout=60,
    )
    if r.returncode != 0:
        err = (r.stderr or r.stdout or "").strip()
        fail(f"Could not patch DataScienceCluster/default-dsc for smoke components: {err or 'unknown error'}")
    print("✓ Patched DataScienceCluster/default-dsc component states for smoke selection")


def _webhook_retryable_patch_error(err: str) -> bool:
    err_lower = (err or "").lower()
    return (
        "no endpoints available for service" in err_lower
        or "failed calling webhook" in err_lower
        or "http response to https" in err_lower
    )


def _patch_dsc_merge_with_webhook_retry(
    patch_doc: str,
    *,
    label: str,
    timeout_sec: int = 300,
) -> None:
    """Merge-patch default-dsc, retrying when the operator admission webhook is unavailable."""
    deadline = time.time() + timeout_sec
    last_err = ""
    while time.time() < deadline:
        r = oc_run(
            ["patch", "datasciencecluster", "default-dsc", "--type=merge", "-p", patch_doc],
            check=False,
            capture_output=True,
            timeout=60,
        )
        if r.returncode == 0:
            print(f"✓ Patched DataScienceCluster/default-dsc {label}")
            return
        last_err = (r.stderr or r.stdout or "").strip()
        if _webhook_retryable_patch_error(last_err):
            print(
                f"Waiting for admission webhook before patch default-dsc {label}...",
                flush=True,
            )
            time.sleep(10)
            continue
        raise RuntimeError(
            f"Could not patch DataScienceCluster/default-dsc {label}: "
            f"{last_err or 'unknown error'}"
        )
    raise RuntimeError(
        f"Could not patch DataScienceCluster/default-dsc {label} after {timeout_sec}s: "
        f"{last_err or 'admission webhook unavailable'}"
    )


def _patch_dsci_merge_with_webhook_retry(
    patch_doc: str,
    *,
    label: str,
    timeout_sec: int = 300,
) -> None:
    """Merge-patch default-dsci, retrying when the operator admission webhook is unavailable."""
    deadline = time.time() + timeout_sec
    last_err = ""
    while time.time() < deadline:
        r = oc_run(
            ["patch", "dscinitialization", "default-dsci", "--type=merge", "-p", patch_doc],
            check=False,
            capture_output=True,
            timeout=60,
        )
        if r.returncode == 0:
            print(f"✓ Patched DSCInitialization/default-dsci {label}", flush=True)
            return
        last_err = (r.stderr or r.stdout or "").strip()
        if "unknown field" in (last_err or "").lower():
            print(
                f"WARN: DSCI {label} not on this cluster version; skipping ({last_err})",
                file=sys.stderr,
                flush=True,
            )
            return
        if _webhook_retryable_patch_error(last_err):
            print(
                f"Waiting for admission webhook before patch default-dsci {label}...",
                flush=True,
            )
            time.sleep(10)
            continue
        raise RuntimeError(
            f"Could not patch DSCInitialization/default-dsci {label}: "
            f"{last_err or 'unknown error'}"
        )
    raise RuntimeError(
        f"Could not patch DSCInitialization/default-dsci {label} after {timeout_sec}s: "
        f"{last_err or 'admission webhook unavailable'}"
    )


def ensure_dsc_component_management_state(dsc_key: str, management_state: str) -> None:
    """Merge-patch one DSC component managementState without changing other components."""
    if not _cr_exists("datasciencecluster", "default-dsc"):
        raise RuntimeError("DataScienceCluster/default-dsc missing; cannot update DSC component")
    patch_doc = json.dumps(
        {"spec": {"components": {dsc_key: {"managementState": management_state}}}}
    )
    _patch_dsc_merge_with_webhook_retry(
        patch_doc,
        label=f"{dsc_key}={management_state}",
    )


def ensure_dsc_component_managed(dsc_key: str) -> None:
    """Merge-patch one DSC component to Managed without changing other components."""
    if not _cr_exists("datasciencecluster", "default-dsc"):
        raise RuntimeError("DataScienceCluster/default-dsc missing; cannot enable DSC component")
    if dsc_component_management_state(dsc_key) == "Managed":
        return
    patch_doc = json.dumps({"spec": {"components": {dsc_key: {"managementState": "Managed"}}}})
    _patch_dsc_merge_with_webhook_retry(patch_doc, label=f"{dsc_key}=Managed")


def dsc_component_management_state(dsc_key: str) -> str:
    r = oc_run(
        [
            "get",
            "datasciencecluster",
            "default-dsc",
            "-o",
            f"jsonpath={{.spec.components.{dsc_key}.managementState}}",
        ],
        check=False,
        capture_output=True,
        timeout=30,
    )
    if r.returncode != 0:
        return ""
    return (r.stdout or "").strip()


def ensure_dsc_component_removed(dsc_key: str) -> None:
    """Merge-patch one DSC component to Removed without changing other components."""
    if not _cr_exists("datasciencecluster", "default-dsc"):
        fail("DataScienceCluster/default-dsc missing; cannot disable DSC component")
    patch_doc = json.dumps({"spec": {"components": {dsc_key: {"managementState": "Removed"}}}})
    r = oc_run(
        ["patch", "datasciencecluster", "default-dsc", "--type=merge", "-p", patch_doc],
        check=False,
        capture_output=True,
        timeout=60,
    )
    if r.returncode != 0:
        err = (r.stderr or r.stdout or "").strip()
        fail(f"Could not patch DataScienceCluster/default-dsc {dsc_key}=Removed: {err or 'unknown error'}")
    print(f"✓ Patched DataScienceCluster/default-dsc {dsc_key}=Removed")


def reconcile_stale_dsc_components_for_smoke(component_ids: set[str]) -> None:
    """Patch install-deferred or stale Managed DSC components to Removed for this selection."""
    if not component_ids or not _cr_exists("datasciencecluster", "default-dsc"):
        return
    from install.dsc_install_policy import stale_removed_dsc_keys_for_smoke

    csv = ",".join(sorted(component_ids))
    operator_version = _resolve_operator_version_for_dsc()
    for key in sorted(stale_removed_dsc_keys_for_smoke(csv, operator_version)):
        if dsc_component_management_state(key) == "Managed":
            ensure_dsc_component_removed(key)


def batch_ensure_dsc_managed_for_smoke(component_ids: set[str]) -> None:
    """Patch all DSC keys needed by selected smoke components in one pass."""
    if not component_ids or not _cr_exists("datasciencecluster", "default-dsc"):
        return
    csv = ",".join(sorted(component_ids))
    if (
        "ogx" in component_ids
        and "llama_stack" not in component_ids
        and dsc_component_management_state("llamastackoperator") == "Managed"
    ):
        ensure_dsc_component_removed("llamastackoperator")
    managed = _dsc_smoke_managed_components(
        csv,
        operator_version=_resolve_operator_version_for_dsc(),
    )
    for key in sorted(managed):
        ensure_dsc_component_managed(key)


def _aigateway_maas_wait_sec(wait_timeout_sec: int | None = None) -> int:
    if wait_timeout_sec is not None:
        return wait_timeout_sec
    return int(os.environ.get("MAAS_PREP_TIMEOUT_SEC", "900"))


def ensure_dsc_models_as_service(
    *,
    wait_timeout_sec: int | None = None,
    wait_for_aigateway: bool = True,
) -> None:
    """Ensure MaaS is Managed on default-dsc (kserve.modelsAsService pre-3.5; aigateway.modelsAsAService on 3.5+)."""
    if not _cr_exists("datasciencecluster", "default-dsc"):
        print("WARN: default-dsc missing; skipping modelsAsService patch", file=sys.stderr)
        return
    if uses_aigateway_models_as_a_service():
        patch_doc = json.dumps(
            {
                "spec": {
                    "components": {
                        "aigateway": {
                            "managementState": "Managed",
                            "modelsAsAService": {"managementState": "Managed"},
                        }
                    }
                }
            }
        )
        label = "aigateway.modelsAsAService"
    else:
        patch_doc = json.dumps(
            {
                "spec": {
                    "components": {
                        "kserve": {
                            "managementState": "Managed",
                            "modelsAsService": {"managementState": "Managed"},
                        }
                    }
                }
            }
        )
        label = "kserve.modelsAsService"
    wait_operator_admission_webhook(
        timeout_sec=int(os.environ.get("VERIFY_OPERATOR_WEBHOOK_WAIT_SEC", "300"))
    )
    _patch_dsc_merge_with_webhook_retry(
        patch_doc,
        label=f"{label}=Managed",
        timeout_sec=int(os.environ.get("DSC_PATCH_WEBHOOK_RETRY_SEC", "300")),
    )
    if uses_aigateway_models_as_a_service():
        ensure_aigateway_models_as_a_service_managed(
            wait_timeout_sec=_aigateway_maas_wait_sec(wait_timeout_sec),
            wait=wait_for_aigateway,
        )


_AIGATEWAY_CR = "default-aigateway"
_MAAS_API_DEPLOY_NS = ("redhat-ai-gateway-infra", "redhat-ods-applications")


def _aigateway_models_as_a_service_state() -> str:
    r = oc_run(
        [
            "get",
            "aigateway",
            _AIGATEWAY_CR,
            "-o",
            "jsonpath={.spec.modelsAsAService.managementState}",
        ],
        check=False,
        capture_output=True,
        timeout=30,
    )
    if r.returncode != 0:
        return ""
    return (r.stdout or "").strip()


def _maas_api_deployment_exists() -> bool:
    for ns in _MAAS_API_DEPLOY_NS:
        r = oc_run(
            ["get", "deployment", "maas-api", "-n", ns],
            check=False,
            capture_output=True,
            timeout=30,
        )
        if r.returncode == 0:
            return True
    return False


def _maas_api_deployment_ready() -> bool:
    for ns in _MAAS_API_DEPLOY_NS:
        r = oc_run(
            [
                "get",
                "deployment",
                "maas-api",
                "-n",
                ns,
                "-o",
                "jsonpath={.status.readyReplicas}",
            ],
            check=False,
            capture_output=True,
            timeout=30,
        )
        if r.returncode != 0:
            continue
        try:
            if int((r.stdout or "0").strip() or "0") >= 1:
                return True
        except ValueError:
            continue
    return False


def ensure_aigateway_models_as_a_service_managed(
    *,
    wait_timeout_sec: int | None = None,
    wait: bool = True,
) -> None:
    """Sync default-aigateway when DSC has modelsAsAService Managed but AIGateway CR lags."""
    if not uses_aigateway_models_as_a_service():
        return
    timeout_sec = _aigateway_maas_wait_sec(wait_timeout_sec)
    deadline = time.time() + timeout_sec
    aigateway_exists = _cr_exists("aigateway", _AIGATEWAY_CR)
    if not aigateway_exists and not wait:
        print(
            f"NOTE: deferring AIGateway/{_AIGATEWAY_CR} modelsAsAService reconcile wait",
            flush=True,
        )
        return
    while not aigateway_exists:
        if time.time() >= deadline:
            raise RuntimeError(
                f"AIGateway/{_AIGATEWAY_CR} not found after {timeout_sec}s"
            )
        print(f"Waiting for AIGateway/{_AIGATEWAY_CR} CR...", flush=True)
        time.sleep(12)
        aigateway_exists = _cr_exists("aigateway", _AIGATEWAY_CR)
    remaining = max(1, int(deadline - time.time()))
    state = _aigateway_models_as_a_service_state()
    if state != "Managed":
        patch_doc = json.dumps(
            {"spec": {"modelsAsAService": {"managementState": "Managed"}}}
        )
        r = oc_run(
            ["patch", "aigateway", _AIGATEWAY_CR, "--type=merge", "-p", patch_doc],
            check=False,
            capture_output=True,
            timeout=60,
        )
        if r.returncode != 0:
            err = (r.stderr or r.stdout or "").strip()
            raise RuntimeError(
                f"Could not patch AIGateway/{_AIGATEWAY_CR} modelsAsAService=Managed: "
                f"{err or 'unknown error'}"
            )
        print(f"✓ Patched AIGateway/{_AIGATEWAY_CR} modelsAsAService=Managed", flush=True)
    if not wait:
        print(
            f"NOTE: deferring AIGateway/{_AIGATEWAY_CR} modelsAsAService reconcile wait",
            flush=True,
        )
        return
    from components.maas_billing.bbr_pre_processing import (
        ensure_maas_controller_openshift_ingress_hpa_rbac,
    )

    ensure_maas_controller_openshift_ingress_hpa_rbac()
    _wait_aigateway_models_as_a_service_reconciled(timeout_sec=remaining)


def _patch_aigateway_models_as_a_service_state(state: str) -> bool:
    patch_doc = json.dumps(
        {"spec": {"modelsAsAService": {"managementState": state}}}
    )
    r = oc_run(
        ["patch", "aigateway", _AIGATEWAY_CR, "--type=merge", "-p", patch_doc],
        check=False,
        capture_output=True,
        timeout=60,
    )
    return r.returncode == 0


def _patch_dsc_aigateway_maas_state(state: str) -> bool:
    if not _cr_exists("datasciencecluster", "default-dsc"):
        return False
    if not uses_aigateway_models_as_a_service():
        return False
    patch_doc = json.dumps(
        {
            "spec": {
                "components": {
                    "aigateway": {
                        "managementState": "Managed",
                        "modelsAsAService": {"managementState": state},
                    }
                }
            }
        }
    )
    r = oc_run(
        ["patch", "datasciencecluster", "default-dsc", "--type=merge", "-p", patch_doc],
        check=False,
        capture_output=True,
        timeout=60,
    )
    return r.returncode == 0


def _cycle_aigateway_models_as_a_service_state() -> None:
    """Bump DSC + AIGateway generation when DeploymentsAvailable is stale without maas-api."""
    print(
        "NOTE: cycling DSC+AIGateway modelsAsAService Removed→Managed "
        "to force maas-api reconcile",
        flush=True,
    )
    _patch_dsc_aigateway_maas_state("Removed")
    if not _patch_aigateway_models_as_a_service_state("Removed"):
        print(
            f"WARN: could not patch AIGateway/{_AIGATEWAY_CR} modelsAsAService=Removed",
            flush=True,
        )
        return
    time.sleep(20)
    _patch_dsc_aigateway_maas_state("Managed")
    if _patch_aigateway_models_as_a_service_state("Managed"):
        print(
            "✓ Patched DSC+AIGateway modelsAsAService back to Managed",
            flush=True,
        )


def _nudge_maas_api_after_aigateway_deployments(*, cycle_spec: bool = False) -> None:
    """DeploymentsAvailable covers gateway infra; maas-api is reconciled separately."""
    from components.maas_billing.bbr_pre_processing import (
        ensure_maas_controller_openshift_ingress_hpa_rbac,
    )

    ensure_maas_controller_openshift_ingress_hpa_rbac()
    print(
        f"NOTE: AIGateway/{_AIGATEWAY_CR} DeploymentsAvailable but maas-api missing; "
        "nudging operator reconcile",
        flush=True,
    )
    if cycle_spec:
        _cycle_aigateway_models_as_a_service_state()
    else:
        _patch_aigateway_models_as_a_service_state("Managed")
    nudge_ts = datetime.now(timezone.utc).isoformat()
    ann_patch = json.dumps(
        {
            "metadata": {
                "annotations": {"rhoai-e2e.io/maas-api-nudge": nudge_ts}
            }
        }
    )
    oc_run(
        ["patch", "aigateway", _AIGATEWAY_CR, "--type=merge", "-p", ann_patch],
        check=False,
        capture_output=True,
        timeout=60,
    )
    restarted: list[str] = []
    for deployment in ("ai-gateway-operator", "maas-controller"):
        r = oc_run(
            ["rollout", "restart", f"deployment/{deployment}", "-n", "redhat-ods-applications"],
            check=False,
            capture_output=True,
            timeout=60,
        )
        if r.returncode == 0:
            restarted.append(deployment)
    if restarted:
        print(
            f"✓ Restarted {', '.join(restarted)} to reconcile maas-api",
            flush=True,
        )


def _wait_aigateway_models_as_a_service_reconciled(*, timeout_sec: int) -> None:
    deadline = time.time() + timeout_sec
    last_nudge = 0.0
    nudge_count = 0
    while time.time() < deadline:
        if _maas_api_deployment_ready():
            print(
                f"✓ AIGateway/{_AIGATEWAY_CR} reconciled (maas-api deployment ready)",
                flush=True,
            )
            return
        gen_r = oc_run(
            [
                "get",
                "aigateway",
                _AIGATEWAY_CR,
                "-o",
                "jsonpath={.metadata.generation}\t{.status.observedGeneration}",
            ],
            check=False,
            capture_output=True,
            timeout=30,
        )
        dep_r = oc_run(
            [
                "get",
                "aigateway",
                _AIGATEWAY_CR,
                "-o",
                'jsonpath={.status.conditions[?(@.type=="DeploymentsAvailable")].status}',
            ],
            check=False,
            capture_output=True,
            timeout=30,
        )
        parts = (gen_r.stdout or "").strip().split("\t")
        generation = parts[0] if parts else ""
        observed = parts[1] if len(parts) > 1 else ""
        dep_status = (dep_r.stdout or "").strip()
        if (
            generation
            and observed
            and generation == observed
            and dep_status == "True"
            and not _maas_api_deployment_exists()
        ):
            now = time.time()
            if now - last_nudge >= 60:
                last_nudge = now
                nudge_count += 1
                _nudge_maas_api_after_aigateway_deployments(
                    cycle_spec=(nudge_count % 3 == 0)
                )
        if int(time.time()) % 60 < 12:
            print(
                f"Waiting for AIGateway/{_AIGATEWAY_CR} modelsAsAService reconcile "
                f"(generation={generation or '?'} observed={observed or '?'} "
                f"DeploymentsAvailable={dep_status or '?'})...",
                flush=True,
            )
        time.sleep(12)
    if _maas_api_deployment_ready():
        print(
            f"✓ AIGateway/{_AIGATEWAY_CR} reconciled at timeout boundary (maas-api ready)",
            flush=True,
        )
        return
    raise RuntimeError(
        f"AIGateway/{_AIGATEWAY_CR} modelsAsAService not reconciled after {timeout_sec}s"
    )


def _smoke_components_need_s3(components_csv: str) -> bool:
    ids = {c.strip() for c in components_csv.split(",") if c.strip()}
    return bool(ids & {"model_server", "model_runtime", "maas_billing"})


def _dashboard_gateway_prereqs_needed(*, for_gateway_stack: bool = False) -> bool:
    if for_gateway_stack or _install_requires_dashboard_gateway():
        return True
    csv = os.environ.get("COMPONENTS_CSV", "").strip()
    ids = {c.strip() for c in csv.split(",") if c.strip()}
    return "dashboard_cypress" in ids


def ensure_dashboard_gateway_prereqs(*, for_gateway_stack: bool = False) -> None:
    """Ensure DSCI serviceMesh and DSC dashboard are Managed for dashboard gateway routes."""
    if not _dashboard_gateway_prereqs_needed(for_gateway_stack=for_gateway_stack):
        return
    wait_operator_admission_webhook(
        timeout_sec=int(os.environ.get("VERIFY_OPERATOR_WEBHOOK_WAIT_SEC", "300"))
    )
    if _cr_exists("dscinitialization", "default-dsci"):
        patch_doc = json.dumps({"spec": {"serviceMesh": {"managementState": "Managed"}}})
        _patch_dsci_merge_with_webhook_retry(
            patch_doc,
            label="serviceMesh=Managed for dashboard gateway",
        )
    if _cr_exists("datasciencecluster", "default-dsc"):
        patch_doc = json.dumps({"spec": {"components": {"dashboard": {"managementState": "Managed"}}}})
        _patch_dsc_merge_with_webhook_retry(
            patch_doc,
            label="dashboard=Managed for verify-operator",
        )


def _ensure_smoke_servicemesh() -> None:
    """Patch DSCI serviceMesh=Managed when smoke/dashboard components need the gateway stack."""
    csv = os.environ.get("COMPONENTS_CSV", "").strip()
    ids = {c.strip() for c in csv.split(",") if c.strip()}
    if _install_requires_dashboard_gateway():
        ensure_dashboard_gateway_prereqs()
        return
    if not csv or not _smoke_components_need_servicemesh(csv):
        return
    if smoke_components_use_kserve_raw_deployment(csv) and "dashboard_cypress" not in ids:
        print("Skipping DSCI ServiceMesh patch (KServe RawDeployment smoke path)")
        return
    if not _cr_exists("dscinitialization", "default-dsci"):
        print("WARN: default-dsci missing; skipping ServiceMesh patch", file=sys.stderr)
        return
    patch_doc = json.dumps({"spec": {"serviceMesh": {"managementState": "Managed"}}})
    r = oc_run(
        ["patch", "dscinitialization", "default-dsci", "--type=merge", "-p", patch_doc],
        check=False,
        capture_output=True,
        timeout=60,
    )
    if r.returncode != 0:
        err = (r.stderr or r.stdout or "").strip()
        fail(f"Could not patch DSCI serviceMesh=Managed for KServe smoke: {err or 'unknown error'}")
    print("✓ Patched DSCInitialization/default-dsci serviceMesh=Managed for KServe smoke")


def _ensure_smoke_s3_trusted_ca() -> None:
    """Patch DSCI trustedCABundle after install so storage-initializer trusts AWS S3 TLS."""
    csv = os.environ.get("COMPONENTS_CSV", "").strip()
    if not csv or not _smoke_components_need_s3(csv):
        return
    kc = os.environ.get("KUBECONFIG", "").strip()
    if not kc:
        print("WARN: KUBECONFIG unset; skipping smoke S3 trustedCABundle patch", file=sys.stderr)
        return
    from k8s.smoke_trusted_ca import ensure_trusted_ca_for_smoke_s3

    try:
        ensure_trusted_ca_for_smoke_s3(target_kubeconfig=Path(kc))
    except Exception as exc:
        print(f"WARN: smoke S3 trustedCABundle patch failed: {exc}", file=sys.stderr)


def _discover_operator_admission_webhook_service(namespace: str) -> str:
    """Resolve the operator admission webhook Service from webhook configurations."""
    operator_name = (os.environ.get("OPERATOR_NAME") or "rhods-operator").strip()
    fallback = f"{operator_name}-service"
    for wh_kind in ("validatingwebhookconfiguration", "mutatingwebhookconfiguration"):
        r = oc_run(
            ["get", wh_kind, "-o", "json"],
            check=False,
            capture_output=True,
            timeout=60,
        )
        if r.returncode != 0:
            continue
        try:
            doc = json.loads(r.stdout or "{}")
        except json.JSONDecodeError:
            continue
        for item in doc.get("items") or []:
            if not isinstance(item, dict):
                continue
            config_name = ((item.get("metadata") or {}).get("name") or "").lower()
            for webhook in item.get("webhooks") or []:
                if not isinstance(webhook, dict):
                    continue
                webhook_name = (webhook.get("name") or "").lower()
                service = webhook.get("clientConfig", {}).get("service") or {}
                if not isinstance(service, dict):
                    continue
                if (service.get("namespace") or "").strip() != namespace:
                    continue
                name = (service.get("name") or "").strip()
                svc_name = name.lower()
                if name and any(
                    token in value
                    for token in (operator_name.lower(), "rhods-operator")
                    for value in (config_name, webhook_name, svc_name)
                ):
                    return name
    for candidate in (fallback, "rhods-operator-service"):
        r = oc_run(
            [
                "get",
                "endpoints",
                candidate,
                "-n",
                namespace,
                "-o",
                "jsonpath={.subsets[*].addresses[*].ip}",
            ],
            check=False,
            capture_output=True,
            text=True,
            timeout=30,
        )
        if r.returncode == 0 and (r.stdout or "").strip():
            return candidate
    return fallback


def wait_operator_admission_webhook(
    *,
    service: str | None = None,
    namespace: str | None = None,
    timeout_sec: int = 600,
) -> None:
    """Wait until the operator admission webhook Service has endpoints."""
    ns = (namespace or os.environ.get("OPERATOR_NAMESPACE") or "redhat-ods-operator").strip()
    svc = (service or _discover_operator_admission_webhook_service(ns)).strip()
    print(f"Waiting for {svc} webhook endpoints in {ns} (up to {timeout_sec}s)...")
    deadline = time.time() + timeout_sec
    iteration = 0
    while time.time() < deadline:
        r = oc_run(
            [
                "get",
                "endpoints",
                svc,
                "-n",
                ns,
                "-o",
                "jsonpath={.subsets[*].addresses[*].ip}",
            ],
            check=False,
            capture_output=True,
            text=True,
            timeout=30,
        )
        if r.returncode == 0 and (r.stdout or "").strip():
            print(f"✓ {svc} webhook endpoints ready")
            return
        iteration += 1
        print(f"  {svc} endpoints not ready (iter {iteration})")
        time.sleep(10)
    fail(f"{svc} webhook endpoints not ready after {timeout_sec}s")


def setup_dsc_resources() -> None:
    """Create DSCInitialization and DataScienceCluster if they don't already exist."""
    print("\nSetting up RHOAI DataScienceCluster resources for BVT testing...")
    components_csv = os.environ.get("COMPONENTS_CSV", "").strip()

    if _cr_exists("dscinitialization", "default-dsci"):
        print("  DSCInitialization/default-dsci already exists — skipping")
    else:
        wait_operator_admission_webhook()
        _wait_for_crd_established(_DSCI_CRD, timeout_sec=600)
        _apply_cr("dscinitialization", "default-dsci", _dsci_yaml(components_csv), timeout_sec=600)

    _ensure_smoke_servicemesh()

    if _cr_exists("datasciencecluster", "default-dsc"):
        if components_csv:
            print("  DataScienceCluster/default-dsc already exists — syncing smoke component states")
            _sync_dsc_smoke_components(components_csv, enable_models_as_service=False)
        else:
            print("  DataScienceCluster/default-dsc already exists — skipping")
    else:
        _wait_for_crd_established(_DSC_CRD, timeout_sec=900)
        dsc_yaml = _with_dsc_api_version(_dsc_yaml_for_install(), _resolve_dsc_api_version())
        _apply_cr("datasciencecluster", "default-dsc", dsc_yaml, timeout_sec=600)

    _ensure_smoke_s3_trusted_ca()


def require_dsc_ready_for_install() -> bool:
    """True when install must fail if DSC is not Ready (BVT or smoke gate)."""
    for key in ("RUN_BVT", "RUN_SMOKE"):
        if os.environ.get(key, "").strip().lower() in ("true", "1", "yes"):
            return True
    return False


def _trainer_selected_for_gate() -> bool:
    """True when trainer is in COMPONENTS_CSV or the CSV is empty (full matrix)."""
    raw = os.environ.get("COMPONENTS_CSV", "").strip()
    if not raw:
        return True
    return "trainer" in {c.strip() for c in raw.split(",") if c.strip()}


def _dsc_condition_status(cond_type: str) -> str:
    r = oc_run(
        [
            "get",
            "datasciencecluster",
            "default-dsc",
            "-o",
            f"jsonpath={{.status.conditions[?(@.type==\"{cond_type}\")].status}}",
        ],
        check=False,
        capture_output=True,
        text=True,
        timeout=30,
    )
    return (r.stdout or "").strip()


_DASHBOARD_DEPLOY_NAMES = ("rhods-dashboard", "odh-dashboard")
_RHODS_DASHBOARD_NS = "redhat-ods-applications"
_DASHBOARD_POD_PREFIXES = ("rhods-dashboard-", "odh-dashboard-")
_rhods_dashboard_scale_down_attempted = False


def _dashboard_pod_name(name: str) -> bool:
    return any(name.startswith(prefix) for prefix in _DASHBOARD_POD_PREFIXES)


def _deployment_replica_counts(deploy: str) -> tuple[int, int] | None:
    spec = oc_run(
        [
            "get",
            "deployment",
            deploy,
            "-n",
            _RHODS_DASHBOARD_NS,
            "-o",
            "jsonpath={.spec.replicas},{.status.readyReplicas}",
        ],
        check=False,
        capture_output=True,
        text=True,
        timeout=30,
    )
    if spec.returncode != 0:
        return None
    raw = (spec.stdout or "").strip()
    if not raw:
        return None
    parts = raw.split(",", 1)
    try:
        desired = int((parts[0] if parts else "") or "0")
        ready = int((parts[1] if len(parts) > 1 else "") or "0")
    except ValueError:
        return None
    return desired, ready


def _pending_dashboard_has_resource_pressure() -> bool:
    pending = oc_run(
        [
            "get",
            "pods",
            "-n",
            _RHODS_DASHBOARD_NS,
            "--field-selector=status.phase=Pending",
            "-o",
            "json",
        ],
        check=False,
        capture_output=True,
        text=True,
        timeout=60,
    )
    if pending.returncode != 0:
        return False
    try:
        pod_list = json.loads(pending.stdout or "{}").get("items") or []
    except json.JSONDecodeError:
        return False
    for pod in pod_list:
        pod_name = (pod.get("metadata") or {}).get("name", "")
        if not _dashboard_pod_name(pod_name):
            continue
        for cond in (pod.get("status") or {}).get("conditions") or []:
            if cond.get("type") == "PodScheduled" and cond.get("status") == "False":
                msg = (cond.get("message") or "").lower()
                if "insufficient cpu" in msg or "insufficient memory" in msg:
                    return True
    return False


def _maybe_scale_rhods_dashboard_for_cpu_pressure() -> None:
    """Scale HA dashboard to one replica when pods stay unschedulable (small dev clusters)."""
    global _rhods_dashboard_scale_down_attempted
    if _rhods_dashboard_scale_down_attempted:
        return
    if os.environ.get("RHOAI_E2E_SKIP_DASHBOARD_SCALE_DOWN", "").strip().lower() in (
        "1",
        "true",
        "yes",
    ):
        return
    has_pressure = _pending_dashboard_has_resource_pressure()
    for deploy in _DASHBOARD_DEPLOY_NAMES:
        counts = _deployment_replica_counts(deploy)
        if counts is None:
            continue
        desired, ready = counts
        if desired <= 1 or ready > 0:
            continue
        if not has_pressure:
            continue
        scale = oc_run(
            [
                "scale",
                "deployment",
                deploy,
                "-n",
                _RHODS_DASHBOARD_NS,
                "--replicas=1",
            ],
            check=False,
            capture_output=False,
            timeout=60,
        )
        if scale.returncode == 0:
            _rhods_dashboard_scale_down_attempted = True
            print(
                f"Scaled {deploy} to 1 replica while waiting for DSC "
                "(Pending: insufficient cluster CPU/memory)",
                flush=True,
            )
        from install.small_cluster_cpu_relief import relieve_cpu_for_dashboard_install

        relieve_cpu_for_dashboard_install()
        return


def wait_dsc_ready(timeout_s: int = 600) -> bool:
    """Poll until DataScienceCluster/default-dsc has Ready==True (and TrainerReady when selected)."""
    need_trainer = _trainer_selected_for_gate()
    if need_trainer:
        # After CLEANUP, JobSetOperator/cluster is often missing until post-install; ensure
        # again here once CRDs exist (dep-operators may have skipped when setup exited ≠0).
        try:
            from install.dependency_operators import ensure_jobset_and_lws_operator_crs

            ensure_jobset_and_lws_operator_crs()
        except Exception as exc:
            print(
                f"WARN: JobSet/LWS ensure before DSC wait failed ({exc}); continuing poll",
                file=sys.stderr,
                flush=True,
            )
    print(
        f"Waiting for DataScienceCluster/default-dsc Ready"
        f"{' + TrainerReady' if need_trainer else ''} (up to {timeout_s}s)...",
        flush=True,
    )
    from install.small_cluster_cpu_relief import maybe_small_cluster_install_prep

    maybe_small_cluster_install_prep()
    deadline = time.time() + timeout_s
    iteration = 0
    _maybe_scale_rhods_dashboard_for_cpu_pressure()
    while time.time() < deadline:
        ready = _dsc_condition_status("Ready")
        trainer = _dsc_condition_status("TrainerReady") if need_trainer else "True"
        if ready == "True" and trainer == "True":
            print("✓ DataScienceCluster/default-dsc is Ready", flush=True)
            if need_trainer:
                print("✓ TrainerReady=True", flush=True)
            return True
        iteration += 1
        trainer_part = f" TrainerReady={trainer or 'unknown'}" if need_trainer else ""
        print(
            f"  DSC Ready={ready or 'unknown'}{trainer_part} (iter {iteration})",
            flush=True,
        )
        _maybe_scale_rhods_dashboard_for_cpu_pressure()
        if iteration % 2 == 0:
            from install.small_cluster_cpu_relief import relieve_cpu_for_dashboard_install

            relieve_cpu_for_dashboard_install()
        if iteration % 4 == 0:
            oc_run(
                [
                    "get",
                    "datasciencecluster",
                    "default-dsc",
                    "-o",
                    "custom-columns=NAME:.metadata.name,PHASE:.status.phase,"
                    "READY:.status.conditions[?(@.type==\"Ready\")].status,"
                    "TRAINER:.status.conditions[?(@.type==\"TrainerReady\")].status",
                ],
                capture_output=False,
                check=False,
                timeout=60,
            )
        time.sleep(15)
    print(
        f"⚠ DataScienceCluster/default-dsc not Ready after {timeout_s}s — tests may fail",
        flush=True,
    )
    oc_run(
        ["describe", "datasciencecluster", "default-dsc"],
        capture_output=False,
        check=False,
        timeout=120,
    )
    return False
