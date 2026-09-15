"""Named install phases for install_and_verify (Tekton step)."""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import time
from dataclasses import dataclass, replace
from pathlib import Path

from install import install_and_verify as iav
from install.approve_transitive_installplans import approve_pending_installplans
from install.dsc_install import (
    ensure_dsc_models_as_service,
    require_dsc_ready_for_install,
    setup_dsc_resources,
    wait_dsc_ready,
)
from install.gateway_config import (
    ensure_rhoai_gateway_for_install,
    gateway_config_ready,
    reconcile_servicemesh_olm_conflicts,
)

_INSTALL_OPERATOR_SCRIPT_TIMEOUT_SEC = 2640  # Just under Tekton install-rhoai/odh 45m task limit


@dataclass(frozen=True)
class InstallContext:
    install_status_path: str
    operator_namespace: str
    operator_name: str
    update_channel: str
    fbcf_image: str
    olminstall_dir: Path
    operator_version_path: str
    catalog_name: str
    cluster_pull_secret: str
    packagemanifest_starting_csv: str | None = None


def load_install_context() -> InstallContext:
    install_status_path = iav.require_env("INSTALL_STATUS_PATH")
    operator_namespace = iav.require_env("OPERATOR_NAMESPACE")
    operator_name = iav.require_env("OPERATOR_NAME")
    iav.validate_dns_label(operator_name, "OPERATOR_NAME")
    update_channel = iav.require_env("UPDATE_CHANNEL")
    fbcf_image = iav.require_env("FBCF_IMAGE")
    olminstall_dir_s = iav.require_env("OLMINSTALL_DIR")
    operator_version_path = iav.require_env("OPERATOR_VERSION_PATH")
    catalog_name = os.environ.get("OLMINSTALL_CATALOG_NAME", "rhoai-catalog-dev")
    cluster_pull_secret = os.environ.get("CLUSTER_MARKETPLACE_PULL_SECRET_NAME", "rhoai-quay-pull")
    olminstall_dir = Path(olminstall_dir_s)
    if not olminstall_dir.is_dir():
        iav.fail(f"❌ OLMINSTALL_DIR is not a directory: {olminstall_dir_s}")
    install_script = olminstall_dir / "install-operator.sh"
    if not install_script.is_file():
        iav.fail(f"❌ install-operator.sh not found under OLMINSTALL_DIR: {install_script}")
    iav.validate_operator_namespace(operator_namespace)
    iav.validate_dns_label(catalog_name, "OLMINSTALL_CATALOG_NAME")
    iav.validate_dns_label(cluster_pull_secret, "CLUSTER_MARKETPLACE_PULL_SECRET_NAME")
    operator_name, operator_namespace = iav.normalize_odh_olm_targets(
        operator_name, operator_namespace, update_channel
    )
    return InstallContext(
        install_status_path=install_status_path,
        operator_namespace=operator_namespace,
        operator_name=operator_name,
        update_channel=update_channel,
        fbcf_image=fbcf_image,
        olminstall_dir=olminstall_dir,
        operator_version_path=operator_version_path,
        catalog_name=catalog_name,
        cluster_pull_secret=cluster_pull_secret,
    )


def phase_validate_cluster(ctx: InstallContext) -> None:
    print("=========================================")
    print(" ODH/RHOAI Operator Installation")
    print(f" FBCF:      {ctx.fbcf_image}")
    print(f" Channel:   {ctx.update_channel}")
    print(f" Operator:  {ctx.operator_name} -> {ctx.operator_namespace}")
    print("=========================================")
    try:
        iav.oc_run(["version"], check=True, capture_output=False, timeout=60)
    except subprocess.CalledProcessError:
        iav.fail("❌ Cannot connect to cluster")


def phase_prepare_namespace(ctx: InstallContext) -> None:
    ns_yaml = iav.oc_run(
        ["create", "namespace", ctx.operator_namespace, "--dry-run=client", "-o", "yaml"],
        check=True,
        capture_output=True,
        timeout=60,
    ).stdout
    iav.oc_run(["apply", "-f", "-"], stdin_text=ns_yaml, check=True, capture_output=True, timeout=120)
    iav.validate_fbcf_image(ctx.fbcf_image)


def phase_catalog_and_pull_secrets(ctx: InstallContext) -> InstallContext:
    catalog_name = ctx.catalog_name
    cluster_pull_secret = ctx.cluster_pull_secret
    iav.reset_stale_operator_install(ctx.operator_namespace, ctx.operator_name, catalog_name)
    iav.ensure_rhoai_registry_access()
    print("Creating CatalogSource (legacy security context)...")
    iav.apply_catalog_source(catalog_name, ctx.fbcf_image)
    print(f"Waiting for OLM to create the {catalog_name} ServiceAccount (up to 2m)...")
    if not iav.wait_for_sa(catalog_name, "openshift-marketplace", time.time() + 120):
        print(f"⚠ ServiceAccount {catalog_name} not observed within 2m")
    lk = iav.oc_run(
        ["secrets", "link", catalog_name, cluster_pull_secret, "-n", "openshift-marketplace", "--for=pull"],
        capture_output=True,
        check=False,
        timeout=60,
    )
    if lk.returncode != 0:
        print(
            f"⚠ Could not link {cluster_pull_secret} to {catalog_name} SA "
            "(SA may not exist yet — non-fatal)"
        )
    print(f"Restarting CatalogSource pod to pick up the {cluster_pull_secret} SA secret...")
    iav.oc_run(
        [
            "delete",
            "pod",
            "-n",
            "openshift-marketplace",
            "-l",
            f"olm.catalogSource={catalog_name}",
            "--ignore-not-found=true",
        ],
        capture_output=True,
        check=False,
        timeout=120,
    )
    iav.oc_run(
        [
            "wait",
            "--for=delete",
            "pod",
            "-n",
            "openshift-marketplace",
            "-l",
            f"olm.catalogSource={catalog_name}",
            "--timeout=60s",
        ],
        capture_output=True,
        check=False,
        timeout=120,
    )
    print("Waiting for CatalogSource to be READY (up to 15m)...")
    if not iav.wait_catalog_ready(catalog_name, time.time() + 900):
        print("❌ CatalogSource not READY after timeout")
        iav.oc_run(
            ["describe", "catalogsource", catalog_name, "-n", "openshift-marketplace"],
            capture_output=False,
            check=False,
            timeout=120,
        )
        pr = iav.oc_run(
            [
                "get",
                "pods",
                "-n",
                "openshift-marketplace",
                "-l",
                f"olm.catalogSource={catalog_name}",
                "-o",
                "jsonpath={.items[0].metadata.name}",
            ],
            capture_output=True,
            text=True,
            check=False,
            timeout=60,
        )
        cs_pod = (pr.stdout or "").strip()
        if cs_pod:
            iav.oc_run(
                ["describe", "pod", cs_pod, "-n", "openshift-marketplace"],
                capture_output=False,
                check=False,
                timeout=120,
            )
        iav.fail()
    print(
        f"Waiting for PackageManifest {ctx.operator_name}/{ctx.update_channel} "
        f"from {catalog_name} (up to 15m)..."
    )
    starting_csv = iav.wait_packagemanifest_ready(
        ctx.operator_name,
        catalog_name,
        ctx.update_channel,
        time.time() + 900,
    )
    if not starting_csv:
        print("❌ PackageManifest not available from catalog after timeout")
        iav.oc_run(
            ["get", "packagemanifest", ctx.operator_name, "-n", "openshift-marketplace", "-o", "yaml"],
            capture_output=False,
            check=False,
            timeout=120,
        )
        iav.fail()
    print(f"Copying {cluster_pull_secret} to {ctx.operator_namespace} and linking to all SAs...")
    if not iav.copy_pull_secret(cluster_pull_secret, ctx.operator_namespace):
        print(
            f"⚠ Failed to copy {cluster_pull_secret} to {ctx.operator_namespace} "
            "— OLM SA-level pulls may fail"
        )
    if not iav.link_secret_to_all_sas(cluster_pull_secret, ctx.operator_namespace):
        iav.fail("❌ oc secrets link failures")
    iav.wait_global_pull_secret_syncer()
    return replace(ctx, packagemanifest_starting_csv=starting_csv)


def phase_olminstall_subscription(ctx: InstallContext) -> str:
    iav.patch_oc_wait_sh(ctx.olminstall_dir, ctx.operator_namespace)
    iav.patch_oc_wait_install_plan_timeout(ctx.olminstall_dir)
    try:
        csv_wait_retries = max(60, int(float(os.environ.get("OPERATOR_CSV_WAIT_SEC", "1200")) // 10))
    except ValueError:
        csv_wait_retries = 120
    iav.patch_oc_wait_csv_timeout(ctx.olminstall_dir, retries=csv_wait_retries)
    manifest_path = iav.resolve_olminstall_manifest(ctx.olminstall_dir, ctx.operator_name)
    iav.patch_manifest_namespace(manifest_path, ctx.operator_namespace)
    iav.patch_manifest_install_plan_automatic(manifest_path)
    iav.patch_manifest_channel(manifest_path, ctx.update_channel)
    starting_csv = ctx.packagemanifest_starting_csv or iav.packagemanifest_channel_csv(
        ctx.operator_name, ctx.catalog_name, ctx.update_channel
    )
    if starting_csv:
        if not iav.patch_manifest_starting_csv(manifest_path, starting_csv):
            iav.fail(
                f"❌ Could not patch startingCSV={starting_csv} into subscription manifest {manifest_path}"
            )
        print(f"Subscription manifest: channel={ctx.update_channel} startingCSV={starting_csv}")
    else:
        print(f"Subscription manifest: channel={ctx.update_channel} (no startingCSV from PackageManifest)")
    print("Applying OLM subscription manifest (bundle unpack may take 30m+ on HyperShift)...", flush=True)
    iav.oc_run(["apply", "-f", str(manifest_path)], check=True, capture_output=True, timeout=120)
    iav.ensure_operatorgroup_bundle_unpack_annotations(ctx.operator_namespace)
    # Keep under the Tekton install-rhoai/odh 45m task limit (catalog + unpack + CSV).
    unpack_timeout = int(os.environ.get("OLM_BUNDLE_UNPACK_TIMEOUT_SEC", "1800"))
    if not iav.wait_subscription_bundle_unpacked(
        ctx.operator_name,
        ctx.operator_namespace,
        time.time() + unpack_timeout,
    ):
        iav.oc_run(
            ["describe", "sub", ctx.operator_name, "-n", ctx.operator_namespace],
            capture_output=False,
            check=False,
            timeout=120,
        )
        iav.fail(
            f"❌ OLM bundle unpack did not complete within {unpack_timeout}s; "
            "not continuing to install-operator.sh (InstallPlan will never appear)"
        )
    print(
        f"Running olminstall (./install-operator.sh {ctx.operator_name} "
        f"{ctx.update_channel} {ctx.catalog_name})..."
    )
    r_install = subprocess.run(
        ["./install-operator.sh", ctx.operator_name, ctx.update_channel, ctx.catalog_name],
        cwd=ctx.olminstall_dir,
        timeout=_INSTALL_OPERATOR_SCRIPT_TIMEOUT_SEC,
    )
    if r_install.returncode != 0:
        print(
            "⚠ install-operator.sh exited non-zero (CSV may still be installing on HyperShift); "
            "continuing with gateway InstallPlan approval and extended CSV wait",
            flush=True,
        )
        iav.oc_run(
            ["get", "sub,csv,installplan", "-n", ctx.operator_namespace],
            capture_output=False,
            check=False,
            timeout=120,
        )
        iav.oc_run(
            ["describe", "sub", "-n", ctx.operator_namespace],
            capture_output=False,
            check=False,
            timeout=120,
        )
        approved = approve_pending_installplans("openshift-operators")
        if approved:
            print(f"Approved {approved} pending InstallPlan(s) in openshift-operators", flush=True)
    csv_version = iav.wait_for_succeeded_csv_version(ctx.operator_namespace, ctx.operator_name)
    if not csv_version:
        print(f"❌ No CSV reached Succeeded phase in namespace {ctx.operator_namespace}")
        iav.oc_run(["get", "csv", "-n", ctx.operator_namespace], capture_output=False, check=False, timeout=120)
        iav.fail()
    Path(ctx.operator_version_path).write_text(csv_version, encoding="utf-8")
    return csv_version


def phase_approve_transitive_olm_deps(ctx: InstallContext) -> None:
    """Jenkins parity: approve pending InstallPlans in openshift-operators (Service Mesh, etc.)."""
    approved = approve_pending_installplans("openshift-operators")
    if approved:
        print(f"Approved {approved} pending InstallPlan(s) in openshift-operators")


def _ensure_gateway_before_dsc_ready() -> None:
    """Service Mesh + GatewayConfig must exist before DSC provisions modelregistry."""
    removed = reconcile_servicemesh_olm_conflicts("openshift-operators")
    if removed:
        print(
            f"Reconciled {removed} orphan Service Mesh CSV(s) in openshift-operators (pre-DSC)",
            flush=True,
        )
    approved = approve_pending_installplans("openshift-operators")
    if approved:
        print(
            f"Approved {approved} gateway-stack InstallPlan(s) in openshift-operators (pre-DSC)",
            flush=True,
        )
    try:
        gateway_timeout = int(os.environ.get("GATEWAY_CONFIG_WAIT_SEC", "1200"))
        ensure_rhoai_gateway_for_install(
            wait_timeout_sec=gateway_timeout,
            wait_servicemesh_first=True,
        )
    except Exception as exc:
        print(f"WARN: pre-DSC gateway prep failed ({exc})", file=sys.stderr)


def phase_post_install_dsc(ctx: InstallContext) -> None:
    setup_dsc_resources()
    _ensure_gateway_before_dsc_ready()
    dsc_timeout = 900 if os.environ.get("COMPONENTS_CSV", "").strip() else 600
    if not wait_dsc_ready(timeout_s=dsc_timeout):
        if require_dsc_ready_for_install():
            print(
                "❌ DataScienceCluster/default-dsc did not become Ready within timeout",
                file=sys.stderr,
            )
            iav.fail("DSC not Ready")
        print(
            "⚠ DataScienceCluster/default-dsc not Ready — continuing install "
            "(no BVT/smoke gate; RUN_BVT and RUN_SMOKE both false)",
            file=sys.stderr,
        )
    components_csv = os.environ.get("COMPONENTS_CSV", "").strip()
    serving_ids = {c.strip() for c in components_csv.split(",") if c.strip()} & {
        "model_server",
        "model_runtime",
        "maas_billing",
    }
    if serving_ids:
        try:
            from components.maas_billing.common import maas_api_deployment_exists

            wait_aigateway = maas_api_deployment_exists()
            if not wait_aigateway:
                print(
                    "NOTE: maas-api not deployed yet; patching DSC modelsAsAService only and "
                    "deferring AIGateway reconcile wait to prepare-components-prerequisites",
                    flush=True,
                )
            ensure_dsc_models_as_service(wait_for_aigateway=wait_aigateway)
        except Exception as exc:
            print(
                f"WARN: post-install aigateway.modelsAsAService patch failed ({exc})",
                file=sys.stderr,
            )
    try:
        gateway_timeout = int(os.environ.get("GATEWAY_CONFIG_WAIT_SEC", "1200"))
        ensure_rhoai_gateway_for_install(wait_timeout_sec=gateway_timeout)
    except Exception as exc:
        print(f"WARN: post-install gateway prep failed ({exc})", file=sys.stderr)
    product = os.environ.get("PRODUCT", "").strip().lower()
    if product in ("rhoai", "odh") and not gateway_config_ready():
        print(
            "WARN: GatewayConfig not Ready after install prep; "
            "verify-operator-ready will retry gateway repair",
            file=sys.stderr,
        )


def print_install_results(ctx: InstallContext, csv_version: str) -> None:
    print("")
    print("=========================================")
    print(" Installation Results")
    print("=========================================")
    print(f" Operator version : {csv_version}")
    print(f" Namespace        : {ctx.operator_namespace}")
    print(f" Channel          : {ctx.update_channel}")
    print(f" FBCF image       : {ctx.fbcf_image}")
    print("-----------------------------------------")
    print(" CSV status:")
    iav.oc_run(
        [
            "get",
            "csv",
            "-n",
            ctx.operator_namespace,
            "-o",
            "custom-columns=NAME:.metadata.name,PHASE:.status.phase,VERSION:.spec.version",
        ],
        capture_output=False,
        check=False,
        timeout=120,
    )
    print("-----------------------------------------")
    print(" Operator deployment:")
    iav.oc_run(
        [
            "get",
            "deployment",
            "-n",
            ctx.operator_namespace,
            "-o",
            "custom-columns=NAME:.metadata.name,READY:.status.readyReplicas,"
            "AVAILABLE:.status.availableReplicas,IMAGE:.spec.template.spec.containers[0].image",
        ],
        capture_output=False,
        check=False,
        timeout=120,
    )
    print("-----------------------------------------")
    print(" Installed CRDs (rhoai):")
    cr = iav.oc_run(["get", "crd", "-o", "json"], capture_output=True, text=True, check=False, timeout=120)
    if cr.returncode == 0:
        try:
            crd_doc = json.loads(cr.stdout or "{}")
        except json.JSONDecodeError:
            crd_doc = None
        if isinstance(crd_doc, dict):
            pat = re.compile(r"opendatahub|datasciencecluster|rhoai|kfdef", re.I)
            for item in crd_doc.get("items") or []:
                name = (item.get("metadata") or {}).get("name") or ""
                if pat.search(name):
                    print(f"  {name}")
    print("=========================================")
    print(f"✅ Installation complete — operator version: {csv_version}")


def print_install_plan(ctx: InstallContext) -> None:
    product = os.environ.get("PRODUCT", "").strip().lower()
    product_label = {"rhoai": "RHOAI", "odh": "ODH"}.get(product, product or "operator")
    print("")
    print("=========================================")
    print(f" Install plan — {product_label}")
    print("=========================================")
    print(f" FBCF image       : {ctx.fbcf_image}")
    print(f" OLM channel      : {ctx.update_channel}")
    print(f" CatalogSource    : {ctx.catalog_name}")
    print(f" OLM package      : {ctx.operator_name}")
    print(f" Namespace        : {ctx.operator_namespace}")
    print("=========================================")
    print("")


def run_install() -> int:
    ctx = load_install_context()
    print_install_plan(ctx)
    phase_validate_cluster(ctx)
    phase_prepare_namespace(ctx)
    ctx = phase_catalog_and_pull_secrets(ctx)
    csv_version = phase_olminstall_subscription(ctx)
    phase_approve_transitive_olm_deps(ctx)
    phase_post_install_dsc(ctx)
    print_install_results(ctx, csv_version)
    Path(ctx.install_status_path).write_text("SUCCESS", encoding="utf-8")
    return 0
