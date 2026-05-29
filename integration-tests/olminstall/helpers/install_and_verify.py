#!/usr/bin/env python3
"""
Install the RHOAI operator via OLM from a Konflux FBCF image and verify the CSV
reaches Succeeded status.

Internal Tekton pipeline step — not meant to be called directly.
From a laptop, trigger tests via: python3 …/olm_pipeline.py

OLM install manifests come from ${OLMINSTALL_DIR} (cloned olminstall repo).
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, NoReturn

OC_PATH = shutil.which("oc")
K8S_DNS_LABEL = re.compile(r"^[a-z0-9]([-a-z0-9]*[a-z0-9])?$")

FBCF_IMAGE_PATTERN = re.compile(r"^[A-Za-z0-9./_:@-]+$")
NS_PATCH_PATTERN = re.compile(r"^(\s*namespace:\s*)redhat-ods-operator\s*$", re.MULTILINE)
OC_WAIT_NEEDLE = 'local namespace="${2:-default}"'

# Minimal DSCI + DSC for BVT. The operator must be installed first; these CRs
# activate RHOAI so the opendatahub-tests conftest can discover the cluster.
_DSCI_YAML = """\
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
    managementState: Removed
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


def require_env(name: str) -> str:
    # Local version that calls fail() to write INSTALL_STATUS on error.
    # Other scripts use the shared helpers.tekton_util.require_env instead.
    v = os.environ.get(name, "").strip()
    if not v:
        fail(f"❌ Required environment variable is missing: {name}")
    return v


def validate_operator_namespace(ns: str) -> None:
    if len(ns) > 63 or not K8S_DNS_LABEL.fullmatch(ns):
        fail(f"❌ OPERATOR_NAMESPACE must be a single RFC 1123 DNS label (len≤63): {ns!r}")


def validate_dns_label(value: str, desc: str) -> None:
    if len(value) > 63 or not K8S_DNS_LABEL.fullmatch(value):
        fail(f"❌ Invalid {desc} (RFC 1123 DNS label, len≤63): {value!r}")


def oc_run(
    args: list[str],
    *,
    check: bool = False,
    capture_output: bool = True,
    text: bool = True,
    stdin_text: str | None = None,
    timeout: float | None = 180,
) -> subprocess.CompletedProcess[str]:
    if not OC_PATH:
        fail("❌ 'oc' binary not found in PATH")
    kw: dict[str, Any] = {"check": check, "text": text}
    if capture_output:
        kw["capture_output"] = True
    else:
        kw["capture_output"] = False
    if stdin_text is not None:
        kw["input"] = stdin_text
    if timeout is not None:
        kw["timeout"] = timeout
    try:
        return subprocess.run([OC_PATH, *args], **kw)
    except subprocess.TimeoutExpired:
        tail = " ".join(args[:10]) + (" ..." if len(args) > 10 else "")
        fail(f"❌ oc timed out ({timeout}s): {tail}")


def validate_fbcf_image(ref: str) -> None:
    if not FBCF_IMAGE_PATTERN.fullmatch(ref):
        fail(f"❌ FBCF_IMAGE contains unexpected characters: {ref}")


def patch_oc_wait_sh(olminstall_dir: Path, operator_namespace: str) -> None:
    path = olminstall_dir / "utils" / "oc_wait.sh"
    text = path.read_text(encoding="utf-8")
    replacement = f'local namespace="${{2:-{operator_namespace}}}"'
    if OC_WAIT_NEEDLE not in text:
        fail(f"❌ Expected snippet not found in {path}")
    path.write_text(text.replace(OC_WAIT_NEEDLE, replacement, 1), encoding="utf-8")


def patch_manifest_namespace(manifest_path: Path, operator_namespace: str) -> None:
    content = manifest_path.read_text(encoding="utf-8")
    if not NS_PATCH_PATTERN.search(content):
        fail(f"❌ Expected namespace stanza for redhat-ods-operator not found in {manifest_path}")
    patched = NS_PATCH_PATTERN.sub(lambda m: m.group(1) + operator_namespace, content)
    manifest_path.write_text(patched, encoding="utf-8")


def normalize_odh_olm_targets(operator_name: str, operator_namespace: str, update_channel: str) -> tuple[str, str]:
    """Align ODH catalog installs with Jenkins (odhTestConfigOperator / generateTestConfigFile).

    For ``odh-stable`` (Konflux ODH catalog) Jenkins uses the rhods-operator OLM package and
    downstream (RHOAI) operator namespace — same as ``install-operator.sh rhods-operator`` in olminstall.
    """
    if update_channel == "odh-stable":
        if operator_name != "rhods-operator" or operator_namespace != "redhat-ods-operator":
            print(
                "ODH odh-stable: using Jenkins/olminstall targets "
                f"rhods-operator / redhat-ods-operator "
                f"(was {operator_name!r} / {operator_namespace!r})"
            )
        return "rhods-operator", "redhat-ods-operator"
    if update_channel == "odh-nightlies" and operator_name == "opendatahub-operator":
        print("ODH odh-nightlies: using rhods-operator OLM package (Jenkins default)")
        return "rhods-operator", operator_namespace
    return operator_name, operator_namespace


def resolve_olminstall_manifest(olminstall_dir: Path, operator_name: str) -> Path:
    """Return path to ``resources/install-<operator>.yaml`` in the cloned olminstall repo."""
    resources_dir = (olminstall_dir / "resources").resolve()
    manifest = olminstall_dir / "resources" / f"install-{operator_name}.yaml"
    try:
        manifest.resolve().relative_to(resources_dir)
    except (ValueError, OSError):
        fail(f"❌ Resolved manifest path escapes olminstall dir: {manifest}")
    if not manifest.is_file():
        fail(f"❌ Missing olminstall manifest: {manifest}")
    return manifest


def apply_catalog_source(name: str, fbcf_image: str) -> None:
    yaml_doc = f"""apiVersion: operators.coreos.com/v1alpha1
kind: CatalogSource
metadata:
  name: {name}
  namespace: openshift-marketplace
spec:
  sourceType: grpc
  image: {fbcf_image}
  displayName: RHOAI Dev Catalog
  publisher: Red Hat
  updateStrategy:
    registryPoll:
      interval: 30m
  grpcPodConfig:
    securityContextConfig: legacy
"""
    oc_run(["apply", "-f", "-"], stdin_text=yaml_doc, check=True, capture_output=True, timeout=300)


def wait_for_sa(sa_name: str, namespace: str, deadline_s: float) -> bool:
    while time.time() < deadline_s:
        r = oc_run(["get", "sa", sa_name, "-n", namespace], capture_output=True, check=False, timeout=30)
        if r.returncode == 0:
            return True
        time.sleep(5)
    return False


def catalog_connection_state(catalog_name: str) -> str:
    r = oc_run(
        [
            "get",
            "catalogsource",
            catalog_name,
            "-n",
            "openshift-marketplace",
            "-o",
            "jsonpath={.status.connectionState.lastObservedState}",
        ],
        capture_output=True,
        text=True,
        check=False,
        timeout=60,
    )
    return (r.stdout or "").strip() if r.returncode == 0 else ""


def copy_pull_secret(secret_name: str, dest_namespace: str) -> bool:
    r = oc_run(
        ["get", "secret", secret_name, "-n", "openshift-marketplace", "-o", "json"],
        capture_output=True,
        text=True,
        check=False,
        timeout=60,
    )
    if r.returncode != 0:
        return False
    try:
        parsed = json.loads(r.stdout or "{}")
    except json.JSONDecodeError:
        return False
    if not isinstance(parsed, dict):
        return False
    obj: dict[str, Any] = parsed
    md = dict(obj.get("metadata") or {})
    for k in ("uid", "resourceVersion", "creationTimestamp", "managedFields", "ownerReferences", "generation"):
        md.pop(k, None)
    md.pop("selfLink", None)
    ann = dict(md.get("annotations") or {})
    ann.pop("kubectl.kubernetes.io/last-applied-configuration", None)
    if ann:
        md["annotations"] = ann
    else:
        md.pop("annotations", None)
    md["namespace"] = dest_namespace
    obj["metadata"] = md
    p = oc_run(
        ["apply", "-f", "-"],
        stdin_text=json.dumps(obj),
        capture_output=True,
        text=True,
        check=False,
        timeout=120,
    )
    return p.returncode == 0


def link_secret_to_all_sas(secret_name: str, namespace: str) -> bool:
    r = oc_run(
        ["get", "sa", "-n", namespace, "--no-headers", "-o", "custom-columns=:metadata.name"],
        capture_output=True,
        text=True,
        check=False,
        timeout=120,
    )
    if r.returncode != 0:
        print(f"⚠ Failed to list service accounts in {namespace}")
        return False
    failures: list[str] = []
    successes = 0
    for line in (r.stdout or "").splitlines():
        name = line.strip()
        if not name:
            continue
        cp = oc_run(
            ["secrets", "link", name, secret_name, "-n", namespace, "--for=pull"],
            capture_output=True,
            check=False,
            timeout=60,
        )
        if cp.returncode != 0:
            failures.append(f"{name}: {(cp.stderr or cp.stdout or '').strip()}")
        else:
            successes += 1
    if failures:
        print(f"❌ Failed linking {secret_name} to some SAs in {namespace}:", file=sys.stderr)
        for item in failures:
            print(f"  {item}", file=sys.stderr)
        return False
    if successes > 0:
        print(f"✓ {secret_name} linked to all SAs in {namespace}")
    else:
        print(f"⚠ No service accounts found in {namespace}")
    return True


def wait_global_pull_secret_syncer() -> None:
    print("Waiting for HyperShift HCCO to sync quay.io/rhoai credentials to all nodes (up to 5m)...")
    sync_desired = 0
    for i in range(1, 25):
        chk = oc_run(
            ["get", "daemonset", "global-pull-secret-syncer", "-n", "kube-system"],
            capture_output=True,
            check=False,
            timeout=30,
        )
        if chk.returncode == 0:
            r = oc_run(
                [
                    "get",
                    "ds",
                    "global-pull-secret-syncer",
                    "-n",
                    "kube-system",
                    "-o",
                    "jsonpath={.status.desiredNumberScheduled}",
                ],
                capture_output=True,
                text=True,
                check=False,
                timeout=30,
            )
            try:
                sync_desired = int((r.stdout or "0").strip() or "0")
            except ValueError:
                sync_desired = 0
            if sync_desired > 0:
                break
        print(f"  waiting for global-pull-secret-syncer DaemonSet... (check {i}/24)")
        time.sleep(5)

    if sync_desired == 0:
        print("⚠ global-pull-secret-syncer DaemonSet not found after 2m — HCCO feature may not be")
        print("  available on this cluster version. Proceeding; bundle-unpack may fail with ErrImagePull.")
        return

    print(f"  global-pull-secret-syncer desired={sync_desired}")
    sync_ready = 0
    ready_deadline = time.time() + 180
    while time.time() < ready_deadline:
        r = oc_run(
            [
                "get",
                "ds",
                "global-pull-secret-syncer",
                "-n",
                "kube-system",
                "-o",
                "jsonpath={.status.numberReady}",
            ],
            capture_output=True,
            text=True,
            check=False,
            timeout=30,
        )
        try:
            sync_ready = int((r.stdout or "0").strip() or "0")
        except ValueError:
            sync_ready = 0
        print(f"  nodes synced: {sync_ready}/{sync_desired}")
        if sync_ready >= max(sync_desired, 1):
            print(f"✓ quay.io/rhoai credentials synced to all {sync_desired} nodes")
            return
        time.sleep(10)

    print(f"⚠ Syncer incomplete after 3m ({sync_ready}/{sync_desired} nodes) — proceeding")
    pods_diag = oc_run(
        ["get", "pods", "-n", "kube-system", "-l", "name=global-pull-secret-syncer", "--no-headers"],
        capture_output=True,
        text=True,
        check=False,
        timeout=60,
    )
    if pods_diag.stdout:
        print(pods_diag.stdout.rstrip())


def _cr_exists(kind: str, name: str) -> bool:
    r = oc_run(["get", kind, name], check=False, capture_output=True, timeout=30)
    return r.returncode == 0


def _apply_cr(kind: str, name: str, yaml_doc: str) -> None:
    r = oc_run(["apply", "-f", "-"], stdin_text=yaml_doc, check=False, capture_output=True, timeout=60)
    if r.returncode != 0:
        err = (r.stderr or r.stdout or "").strip()
        print(f"⚠ Could not apply {kind}/{name}: {err}", file=sys.stderr)
        fail(f"oc apply failed for {kind}/{name}: {err or 'unknown error'}")
    print(f"✓ Applied {kind}/{name}")


def setup_dsc_resources() -> None:
    """Create DSCInitialization and DataScienceCluster if they don't already exist."""
    print("\nSetting up RHOAI DataScienceCluster resources for BVT testing...")

    if _cr_exists("dscinitialization", "default-dsci"):
        print("  DSCInitialization/default-dsci already exists — skipping")
    else:
        _apply_cr("dscinitialization", "default-dsci", _DSCI_YAML)

    if _cr_exists("datasciencecluster", "default-dsc"):
        print("  DataScienceCluster/default-dsc already exists — skipping")
    else:
        _apply_cr("datasciencecluster", "default-dsc", _DSC_YAML)


def wait_dsc_ready(timeout_s: int = 600) -> bool:
    """Poll until DataScienceCluster/default-dsc has Ready==True or timeout expires."""
    print(f"Waiting for DataScienceCluster/default-dsc to be Ready (up to {timeout_s}s)...")
    deadline = time.time() + timeout_s
    iteration = 0
    while time.time() < deadline:
        r = oc_run(
            [
                "get", "datasciencecluster", "default-dsc",
                "-o", "jsonpath={.status.conditions[?(@.type==\"Ready\")].status}",
            ],
            check=False, capture_output=True, text=True, timeout=30,
        )
        status = (r.stdout or "").strip()
        if status == "True":
            print("✓ DataScienceCluster/default-dsc is Ready")
            return True
        iteration += 1
        print(f"  DSC Ready status: {status or 'unknown'} (iter {iteration})")
        if iteration % 4 == 0:
            oc_run(
                ["get", "datasciencecluster", "default-dsc", "-o",
                 "custom-columns=NAME:.metadata.name,PHASE:.status.phase,"
                 "READY:.status.conditions[?(@.type==\"Ready\")].status"],
                capture_output=False, check=False, timeout=60,
            )
        time.sleep(15)
    print(f"⚠ DataScienceCluster/default-dsc not Ready after {timeout_s}s — BVT tests may fail")
    oc_run(["describe", "datasciencecluster", "default-dsc"], capture_output=False, check=False, timeout=120)
    return False


def pick_succeeded_csv_version(namespace: str, olminstall_operator: str) -> str | None:
    r = oc_run(["get", "csv", "-n", namespace, "-o", "json"], capture_output=True, text=True, check=False, timeout=120)
    if r.returncode != 0:
        return None
    try:
        data = json.loads(r.stdout or "{}")
    except json.JSONDecodeError:
        return None
    if not isinstance(data, dict):
        return None
    op_pat = re.compile(re.escape(olminstall_operator), re.I)
    for item in data.get("items") or []:
        if (item.get("status") or {}).get("phase") != "Succeeded":
            continue
        md_name = (item.get("metadata") or {}).get("name") or ""
        disp = ((item.get("spec") or {}).get("displayName")) or ""
        if md_name.startswith(olminstall_operator) or (disp and op_pat.search(disp)):
            ver = (item.get("spec") or {}).get("version")
            if ver:
                return str(ver)
    return None


def wait_catalog_ready(catalog_name: str, deadline_s: float) -> bool:
    cs_status = ""
    iteration = 0
    while time.time() < deadline_s:
        cs_status = catalog_connection_state(catalog_name)
        if cs_status == "READY":
            print("✓ CatalogSource READY")
            return True
        iteration += 1
        print(f"  CS state: {cs_status or 'unknown'} (iter {iteration})")
        if iteration % 4 == 0:
            pr = oc_run(
                [
                    "get",
                    "pods",
                    "-n",
                    "openshift-marketplace",
                    "-l",
                    f"olm.catalogSource={catalog_name}",
                    "--no-headers",
                    "-o",
                    "custom-columns=:metadata.name",
                ],
                capture_output=True,
                text=True,
                check=False,
                timeout=60,
            )
            pod = (pr.stdout.splitlines()[0].strip() if pr.stdout else "") or ""
            if pod:
                oc_run(["get", "pod", pod, "-n", "openshift-marketplace"], capture_output=False, check=False, timeout=60)
                ev = oc_run(
                    [
                        "get",
                        "events",
                        "-n",
                        "openshift-marketplace",
                        "--field-selector",
                        f"involvedObject.name={pod}",
                    ],
                    capture_output=True,
                    text=True,
                    check=False,
                    timeout=60,
                )
                lines = ev.stdout.splitlines()
                for line in lines[-3:]:
                    print(line)
            else:
                print("  no CatalogSource pod yet")
                oc_run(["get", "pods", "-n", "openshift-marketplace", "--no-headers"], capture_output=False, check=False, timeout=60)
        time.sleep(15)
    return False


def main() -> int:
    install_status_path = require_env("INSTALL_STATUS_PATH")
    operator_namespace = require_env("OPERATOR_NAMESPACE")
    operator_name = require_env("OPERATOR_NAME")
    validate_dns_label(operator_name, "OPERATOR_NAME")
    update_channel = require_env("UPDATE_CHANNEL")
    fbcf_image = require_env("FBCF_IMAGE")
    olminstall_dir_s = require_env("OLMINSTALL_DIR")
    operator_version_path = require_env("OPERATOR_VERSION_PATH")

    catalog_name = os.environ.get("OLMINSTALL_CATALOG_NAME", "rhoai-catalog-dev")
    # QUAY_PULL_SECRET_NAME names the Konflux tenant Secret mounted at /var/secret/quay for patch_cluster_pull_secret.
    # That script creates rhoai-quay-pull in openshift-marketplace on the target cluster — use that for oc link/copy here.
    cluster_pull_secret = os.environ.get("CLUSTER_MARKETPLACE_PULL_SECRET_NAME", "rhoai-quay-pull")

    olminstall_dir = Path(olminstall_dir_s)
    if not olminstall_dir.is_dir():
        fail(f"❌ OLMINSTALL_DIR is not a directory: {olminstall_dir_s}")
    install_script = olminstall_dir / "install-operator.sh"
    if not install_script.is_file():
        fail(f"❌ install-operator.sh not found under OLMINSTALL_DIR: {install_script}")
    validate_operator_namespace(operator_namespace)
    validate_dns_label(catalog_name, "OLMINSTALL_CATALOG_NAME")
    validate_dns_label(cluster_pull_secret, "CLUSTER_MARKETPLACE_PULL_SECRET_NAME")

    operator_name, operator_namespace = normalize_odh_olm_targets(
        operator_name, operator_namespace, update_channel
    )

    print("=========================================")
    print(" ODH/RHOAI Operator Installation")
    print(f" FBCF:      {fbcf_image}")
    print(f" Channel:   {update_channel}")
    print(f" Operator:  {operator_name} -> {operator_namespace}")
    print("=========================================")

    try:
        oc_run(["version"], check=True, capture_output=False, timeout=60)
    except subprocess.CalledProcessError:
        fail("❌ Cannot connect to cluster")

    ns_yaml = oc_run(
        ["create", "namespace", operator_namespace, "--dry-run=client", "-o", "yaml"],
        check=True,
        capture_output=True,
        timeout=60,
    ).stdout
    oc_run(["apply", "-f", "-"], stdin_text=ns_yaml, check=True, capture_output=True, timeout=120)

    validate_fbcf_image(fbcf_image)

    print("Creating CatalogSource (legacy security context)...")
    apply_catalog_source(catalog_name, fbcf_image)

    print(f"Waiting for OLM to create the {catalog_name} ServiceAccount (up to 2m)...")
    if not wait_for_sa(catalog_name, "openshift-marketplace", time.time() + 120):
        print(f"⚠ ServiceAccount {catalog_name} not observed within 2m")

    lk = oc_run(
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
    oc_run(
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
    oc_run(
        ["wait", "--for=delete", "pod", "-n", "openshift-marketplace", "-l", f"olm.catalogSource={catalog_name}", "--timeout=60s"],
        capture_output=True,
        check=False,
        timeout=120,
    )

    print("Waiting for CatalogSource to be READY (up to 15m)...")
    if not wait_catalog_ready(catalog_name, time.time() + 900):
        print("❌ CatalogSource not READY after timeout")
        oc_run(["describe", "catalogsource", catalog_name, "-n", "openshift-marketplace"], capture_output=False, check=False, timeout=120)
        pr = oc_run(
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
            oc_run(["describe", "pod", cs_pod, "-n", "openshift-marketplace"], capture_output=False, check=False, timeout=120)
        fail()

    print(f"Copying {cluster_pull_secret} to {operator_namespace} and linking to all SAs...")
    if not copy_pull_secret(cluster_pull_secret, operator_namespace):
        print(f"⚠ Failed to copy {cluster_pull_secret} to {operator_namespace} — OLM SA-level pulls may fail")
    if not link_secret_to_all_sas(cluster_pull_secret, operator_namespace):
        fail("❌ oc secrets link failures")

    wait_global_pull_secret_syncer()

    patch_oc_wait_sh(olminstall_dir, operator_namespace)
    manifest_path = resolve_olminstall_manifest(olminstall_dir, operator_name)
    patch_manifest_namespace(manifest_path, operator_namespace)

    print(
        f"Running olminstall (./install-operator.sh {operator_name} {update_channel} {catalog_name})..."
    )
    r_install = subprocess.run(
        ["./install-operator.sh", operator_name, update_channel, catalog_name],
        cwd=olminstall_dir,
        timeout=7200,
    )
    if r_install.returncode != 0:
        print("❌ olminstall install-operator.sh failed")
        oc_run(["get", "sub,csv,installplan", "-n", operator_namespace], capture_output=False, check=False, timeout=120)
        oc_run(["describe", "sub", "-n", operator_namespace], capture_output=False, check=False, timeout=120)
        fail()

    csv_version = pick_succeeded_csv_version(operator_namespace, operator_name)
    if not csv_version:
        print(f"❌ No CSV reached Succeeded phase in namespace {operator_namespace}")
        oc_run(["get", "csv", "-n", operator_namespace], capture_output=False, check=False, timeout=120)
        fail()
    Path(operator_version_path).write_text(csv_version, encoding="utf-8")

    setup_dsc_resources()
    if not wait_dsc_ready(timeout_s=600):
        print("❌ DataScienceCluster/default-dsc did not become Ready within timeout", file=sys.stderr)
        fail("DSC not Ready")

    print("")
    print("=========================================")
    print(" Installation Results")
    print("=========================================")
    print(f" Operator version : {csv_version}")
    print(f" Namespace        : {operator_namespace}")
    print(f" Channel          : {update_channel}")
    print(f" FBCF image       : {fbcf_image}")
    print("-----------------------------------------")
    print(" CSV status:")
    oc_run(
        [
            "get",
            "csv",
            "-n",
            operator_namespace,
            "-o",
            "custom-columns=NAME:.metadata.name,PHASE:.status.phase,VERSION:.spec.version",
        ],
        capture_output=False,
        check=False,
        timeout=120,
    )
    print("-----------------------------------------")
    print(" Operator deployment:")
    oc_run(
        [
            "get",
            "deployment",
            "-n",
            operator_namespace,
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
    cr = oc_run(["get", "crd", "-o", "json"], capture_output=True, text=True, check=False, timeout=120)
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
    Path(install_status_path).write_text("SUCCESS", encoding="utf-8")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        fail("❌ Interrupted")
    except json.JSONDecodeError as exc:
        fail(f"❌ Invalid JSON in command output: {exc}")
    except subprocess.TimeoutExpired:
        fail("❌ Command timed out (install step limit is 2h)")
    except subprocess.CalledProcessError as exc:
        if exc.stderr:
            print(exc.stderr, file=sys.stderr, end="" if exc.stderr.endswith("\n") else "\n")
        if exc.stdout:
            print(exc.stdout, file=sys.stderr, end="" if exc.stdout.endswith("\n") else "\n")
        fail(f"❌ Command failed (exit {exc.returncode})")
