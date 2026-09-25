#!/usr/bin/env python3
"""
Install the RHOAI operator via OLM from a Konflux FBCF image and verify the CSV
reaches Succeeded status.

Internal Tekton pipeline step — not meant to be called directly.
From a laptop, trigger tests via: python3 …/rhoai_e2e.py

OLM install manifests come from ${OLMINSTALL_DIR} (cloned upstream olminstall repo).
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, NoReturn

from _bootstrap import ensure_rhoai_e2e_path

ensure_rhoai_e2e_path()

from k8s.oc_util import run_oc

K8S_DNS_LABEL = re.compile(r"^[a-z0-9]([-a-z0-9]*[a-z0-9])?$")

FBCF_IMAGE_PATTERN = re.compile(r"^[A-Za-z0-9./_:@-]+$")
NS_PATCH_PATTERN = re.compile(r"^(\s*namespace:\s*)redhat-ods-operator\s*$", re.MULTILINE)
MANUAL_INSTALL_PLAN_PATTERN = re.compile(r"installPlanApproval:\s*Manual\b")
MANIFEST_CHANNEL_PATTERN = re.compile(r"^(\s*channel:\s*)\S+\s*$", re.MULTILINE)
MANIFEST_STARTING_CSV_PATTERN = re.compile(r'^(\s*startingCSV:\s*)(""|\'\'|)\s*$', re.MULTILINE)
OC_WAIT_NEEDLE = 'local namespace="${2:-default}"'
RHOAI_IDMS_SOURCE = "registry.redhat.io/rhoai"
RHOAI_IDMS_MIRROR = "quay.io/rhoai"
# OLM OperatorGroup annotations (see operator-framework bundle_unpacker.go).
_BUNDLE_UNPACK_TIMEOUT_ANN = "operatorframework.io/bundle-unpack-timeout"
_BUNDLE_UNPACK_RETRY_ANN = "operatorframework.io/bundle-unpack-min-retry-interval"
# Per-Job ActiveDeadlineSeconds via OperatorGroup annotation (operator-framework).
_DEFAULT_BUNDLE_UNPACK_JOB_TIMEOUT = "20m"
_EPHC_BUNDLE_UNPACK_JOB_TIMEOUT = "45m"
_MARKETPLACE_NS = "openshift-marketplace"
RHOAI_IDMS_NAME = "rhoai-idms-mirror"


def default_bundle_unpack_job_timeout() -> str:
    explicit = os.environ.get("OLM_BUNDLE_UNPACK_JOB_TIMEOUT", "").strip()
    if explicit:
        return explicit
    from install.gateway_config import cluster_source_is_ephc

    if cluster_source_is_ephc():
        return _EPHC_BUNDLE_UNPACK_JOB_TIMEOUT
    return _DEFAULT_BUNDLE_UNPACK_JOB_TIMEOUT


def default_bundle_unpack_min_retry_interval() -> str:
    explicit = os.environ.get("OLM_BUNDLE_UNPACK_MIN_RETRY_INTERVAL", "").strip()
    if explicit:
        return explicit
    from install.gateway_config import cluster_source_is_ephc

    return "3m" if cluster_source_is_ephc() else "5m"


def patch_manifest_operatorgroup_bundle_unpack(manifest_path: Path) -> None:
    """Embed bundle-unpack OperatorGroup annotations before apply (first OLM job).

    Tekton install-rhoai image has no PyYAML; patch manifest text in place.
    """
    text = manifest_path.read_text(encoding="utf-8")
    if not re.search(r"^kind:\s*OperatorGroup\s*$", text, re.MULTILINE):
        return
    timeout = default_bundle_unpack_job_timeout()
    min_retry = default_bundle_unpack_min_retry_interval()
    ann_block = (
        "  annotations:\n"
        f"    {_BUNDLE_UNPACK_TIMEOUT_ANN}: \"{timeout}\"\n"
        f"    {_BUNDLE_UNPACK_RETRY_ANN}: \"{min_retry}\"\n"
    )

    def _patch_doc(block: str) -> str:
        if not re.search(r"^kind:\s*OperatorGroup\s*$", block, re.MULTILINE):
            return block
        if re.search(rf"^\s*{_BUNDLE_UNPACK_TIMEOUT_ANN}:", block, re.MULTILINE):
            block = re.sub(
                rf"^(\s*{_BUNDLE_UNPACK_TIMEOUT_ANN}:)\s*.*$",
                rf'\1 "{timeout}"',
                block,
                count=1,
                flags=re.MULTILINE,
            )
            block = re.sub(
                rf"^(\s*{_BUNDLE_UNPACK_RETRY_ANN}:)\s*.*$",
                rf'\1 "{min_retry}"',
                block,
                count=1,
                flags=re.MULTILINE,
            )
            return block
        if re.search(r"^metadata:\s*$", block, re.MULTILINE):
            return re.sub(
                r"(^metadata:\s*\n)",
                lambda m: m.group(1) + ann_block,
                block,
                count=1,
                flags=re.MULTILINE,
            )
        return block

    parts = re.split(r"(?=^apiVersion:)", text, flags=re.MULTILINE)
    patched = "".join(_patch_doc(part) for part in parts if part)
    if patched == text:
        return
    manifest_path.write_text(patched, encoding="utf-8")
    print(
        f"✓ OperatorGroup in {manifest_path.name}: "
        f"{_BUNDLE_UNPACK_TIMEOUT_ANN}={timeout}, {_BUNDLE_UNPACK_RETRY_ANN}={min_retry}",
        flush=True,
    )


def _catalog_version_fallback() -> str:
    from suite.snapshot_catalog_line import catalog_version_for_install

    return catalog_version_for_install(
        rhoai_version_param=os.environ.get("RHOAI_VERSION", ""),
        fbcf_image=os.environ.get("FBCF_IMAGE", ""),
    )


def _seed_operator_version_from_catalog() -> None:
    ver = _catalog_version_fallback()
    if not ver:
        return
    op_path = os.environ.get("OPERATOR_VERSION_PATH", "").strip()
    if not op_path:
        return
    try:
        Path(op_path).write_text(ver, encoding="utf-8")
    except OSError:
        pass


def fail(message: str = "") -> NoReturn:
    if message:
        print(message)
    p = os.environ.get("INSTALL_STATUS_PATH")
    if p:
        try:
            Path(p).write_text("FAILED", encoding="utf-8")
        except OSError:
            pass
    op_path = os.environ.get("OPERATOR_VERSION_PATH", "").strip()
    if op_path:
        try:
            current = Path(op_path).read_text(encoding="utf-8").strip()
            if not current:
                ver = _catalog_version_fallback()
                if ver:
                    Path(op_path).write_text(ver, encoding="utf-8")
        except OSError:
            pass
    sys.exit(1)


def require_env(name: str) -> str:
    # Local version that calls fail() to write INSTALL_STATUS on error.
    # Other scripts use the shared steps.tekton_util.require_env instead.
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
    return run_oc(
        args,
        check=check,
        capture_output=capture_output,
        text=text,
        stdin_text=stdin_text,
        timeout=timeout,
        on_timeout=lambda msg: fail(f"❌ {msg}"),
    )


def validate_fbcf_image(ref: str) -> None:
    if not FBCF_IMAGE_PATTERN.fullmatch(ref):
        fail(f"❌ FBCF_IMAGE contains unexpected characters: {ref}")


def patch_oc_wait_sh(operator_install_dir: Path, operator_namespace: str) -> None:
    path = operator_install_dir / "utils" / "oc_wait.sh"
    text = path.read_text(encoding="utf-8")
    replacement = f'local namespace="${{2:-{operator_namespace}}}"'
    if OC_WAIT_NEEDLE not in text:
        fail(f"❌ Expected snippet not found in {path}")
    path.write_text(text.replace(OC_WAIT_NEEDLE, replacement, 1), encoding="utf-8")


def patch_oc_wait_install_plan_timeout(operator_install_dir: Path, *, retries: int = 240) -> None:
    """Extend rhoai-e2e ``oc_wait_for_ip`` (default 10×10s) for FBC catalog bundle unpack."""
    path = operator_install_dir / "utils" / "oc_wait.sh"
    text = path.read_text(encoding="utf-8")
    old = "for i in {1..10};"
    new = f"for i in {{1..{retries}}};"
    if old not in text:
        fail(f"❌ Expected installPlan wait loop not found in {path}")
    path.write_text(text.replace(old, new, 1), encoding="utf-8")


def patch_oc_wait_csv_timeout(operator_install_dir: Path, *, retries: int = 120) -> None:
    """Extend rhoai-e2e ``oc_wait_for_csv`` (default 60×10s) for HyperShift FBC installs."""
    path = operator_install_dir / "utils" / "oc_wait.sh"
    text = path.read_text(encoding="utf-8")
    marker = "oc_wait_for_csv() {"
    if marker not in text:
        fail(f"❌ Expected oc_wait_for_csv not found in {path}")
    head, rest = text.split(marker, 1)
    old = "for i in {1..60};"
    if old not in rest:
        fail(f"❌ Expected CSV wait loop not found in {path}")
    path.write_text(head + marker + rest.replace(old, f"for i in {{1..{retries}}};", 1), encoding="utf-8")


def patch_manifest_namespace(manifest_path: Path, operator_namespace: str) -> None:
    content = manifest_path.read_text(encoding="utf-8")
    if not NS_PATCH_PATTERN.search(content):
        fail(f"❌ Expected namespace stanza for redhat-ods-operator not found in {manifest_path}")
    patched = NS_PATCH_PATTERN.sub(lambda m: m.group(1) + operator_namespace, content)
    manifest_path.write_text(patched, encoding="utf-8")


def patch_manifest_install_plan_automatic(manifest_path: Path) -> None:
    """Konflux installs must auto-approve; rhoai-e2e defaults to Manual."""
    content = manifest_path.read_text(encoding="utf-8")
    if not MANUAL_INSTALL_PLAN_PATTERN.search(content):
        return
    manifest_path.write_text(
        MANUAL_INSTALL_PLAN_PATTERN.sub("installPlanApproval: Automatic", content),
        encoding="utf-8",
    )


def patch_manifest_channel(manifest_path: Path, channel: str) -> None:
    """Align subscription channel with UPDATE_CHANNEL (rhoai-e2e manifest defaults to fast)."""
    channel = channel.strip()
    if not channel:
        fail("❌ UPDATE_CHANNEL is empty")
    content = manifest_path.read_text(encoding="utf-8")
    if not MANIFEST_CHANNEL_PATTERN.search(content):
        fail(f"❌ Expected channel field in subscription manifest: {manifest_path}")
    manifest_path.write_text(
        MANIFEST_CHANNEL_PATTERN.sub(lambda m: f"{m.group(1)}{channel}", content),
        encoding="utf-8",
    )


def patch_manifest_starting_csv(manifest_path: Path, starting_csv: str) -> bool:
    """Pin startingCSV from PackageManifest so OLM targets the catalog channel CSV."""
    starting_csv = starting_csv.strip()
    if not starting_csv:
        return True
    content = manifest_path.read_text(encoding="utf-8")
    if MANIFEST_STARTING_CSV_PATTERN.search(content):
        patched = MANIFEST_STARTING_CSV_PATTERN.sub(
            lambda m: f'{m.group(1)}"{starting_csv}"',
            content,
        )
    elif re.search(r"^\s*startingCSV:\s*", content, re.MULTILINE):
        patched = re.sub(
            r"^(\s*startingCSV:\s*)\S+\s*$",
            lambda m: f'{m.group(1)}"{starting_csv}"',
            content,
            count=1,
            flags=re.MULTILINE,
        )
    elif re.search(r"^\s*channel:\s*", content, re.MULTILINE):
        patched = re.sub(
            r"^(\s*channel:\s*[^\n]+)",
            lambda m: f'{m.group(1)}\n  startingCSV: "{starting_csv}"',
            content,
            count=1,
            flags=re.MULTILINE,
        )
    else:
        return False
    manifest_path.write_text(patched, encoding="utf-8")
    return True


def idms_has_rhoai_mirror(spec: dict[str, Any]) -> bool:
    for entry in spec.get("imageDigestMirrors") or []:
        if entry.get("source") != RHOAI_IDMS_SOURCE:
            continue
        if RHOAI_IDMS_MIRROR in (entry.get("mirrors") or []):
            return True
    return False


def ensure_rhoai_idms_mirror() -> None:
    """Mirror registry.redhat.io/rhoai to quay.io/rhoai (EPHC provisioning default).

    External pooled clusters often lack this IDMS; OLM bundle-unpack then hangs until
    the unpack job deadline and fails with BundleUnpackFailed.
    """
    r = oc_run(["get", "imagedigestmirrorset", "-o", "json"], capture_output=True, check=False, timeout=60)
    if r.returncode == 0:
        try:
            items = json.loads(r.stdout or "{}").get("items") or []
        except json.JSONDecodeError:
            items = []
        for item in items:
            if idms_has_rhoai_mirror(item.get("spec") or {}):
                print(f"✓ IDMS mirror already configured ({RHOAI_IDMS_SOURCE} → {RHOAI_IDMS_MIRROR})")
                return

    entry = {"source": RHOAI_IDMS_SOURCE, "mirrors": [RHOAI_IDMS_MIRROR]}
    r_cluster = oc_run(
        ["get", "imagedigestmirrorset", "cluster", "-o", "json"],
        capture_output=True,
        check=False,
        timeout=30,
    )
    if r_cluster.returncode == 0:
        try:
            cluster = json.loads(r_cluster.stdout or "{}")
        except json.JSONDecodeError:
            cluster = {}
        spec = cluster.get("spec") or {}
        mirrors = list(spec.get("imageDigestMirrors") or [])
        if not idms_has_rhoai_mirror(spec):
            mirrors.append(entry)
            patch = json.dumps({"spec": {"imageDigestMirrors": mirrors}})
            pr = oc_run(
                ["patch", "imagedigestmirrorset", "cluster", "--type=merge", "-p", patch],
                capture_output=True,
                check=False,
                timeout=120,
            )
            if pr.returncode == 0:
                print(f"✓ Patched cluster IDMS: {RHOAI_IDMS_SOURCE} → {RHOAI_IDMS_MIRROR}")
                return
            print(f"⚠ Could not patch cluster IDMS: {(pr.stderr or pr.stdout).strip()}")

    yaml_doc = f"""apiVersion: config.openshift.io/v1
kind: ImageDigestMirrorSet
metadata:
  name: {RHOAI_IDMS_NAME}
spec:
  imageDigestMirrors:
  - source: {RHOAI_IDMS_SOURCE}
    mirrors:
    - {RHOAI_IDMS_MIRROR}
"""
    print(f"Applying IDMS mirror {RHOAI_IDMS_SOURCE} → {RHOAI_IDMS_MIRROR}...")
    pr = oc_run(["apply", "-f", "-"], stdin_text=yaml_doc, capture_output=True, check=False, timeout=120)
    if pr.returncode != 0:
        err = (pr.stderr or pr.stdout).strip()
        if "mirror-binding" in err or "HostedCluster" in err:
            fail(
                "❌ HyperShift denied runtime IDMS; use ROSA HCP Kyverno setup (prepare-cluster-registry)."
            )
        fail(f"❌ Could not apply IDMS mirror: {err}")
    print("✓ IDMS mirror applied")


def ensure_rhoai_registry_access() -> None:
    """IDMS on standard clusters; ROSA HCP Kyverno on HyperShift (Jenkins addICSP)."""
    r = oc_run(["get", "imagedigestmirrorset", "-o", "json"], capture_output=True, check=False, timeout=60)
    if r.returncode == 0:
        try:
            items = json.loads(r.stdout or "{}").get("items") or []
        except json.JSONDecodeError:
            items = []
        for item in items:
            if idms_has_rhoai_mirror(item.get("spec") or {}):
                print(f"✓ IDMS mirror already configured ({RHOAI_IDMS_SOURCE} → {RHOAI_IDMS_MIRROR})")
                return

    from install.rosa_hcp_pull_setup import is_hypershift_managed_cluster, rosa_hcp_pull_setup_ready

    if is_hypershift_managed_cluster():
        if rosa_hcp_pull_setup_ready():
            print("✓ ROSA HCP Kyverno pull setup ready (registry.redhat.io/rhoai → quay.io/rhoai)")
            return
        fail(
            "❌ HyperShift cluster missing ROSA HCP Kyverno pull setup. "
            "prepare-cluster-registry should configure it before install."
        )
    ensure_rhoai_idms_mirror()


def _operator_csv_package_name(csv_name: str) -> str:
    return csv_name.split(".", 1)[0]


def _delete_operator_scoped_csvs(operator_namespace: str, operator_name: str) -> None:
    """Remove only the main operator CSV copies; keep dependency CSVs in the same namespace."""
    listed = oc_run(
        ["get", "clusterserviceversion", "-n", operator_namespace, "-o", "json"],
        capture_output=True,
        check=False,
        timeout=60,
    )
    if listed.returncode != 0:
        return
    try:
        doc = json.loads(listed.stdout or "{}")
    except json.JSONDecodeError:
        return
    targets: list[str] = []
    for item in doc.get("items") or []:
        if not isinstance(item, dict):
            continue
        name = str((item.get("metadata") or {}).get("name") or "").strip()
        if not name:
            continue
        if _operator_csv_package_name(name) == operator_name:
            targets.append(name)
    for name in sorted(set(targets)):
        oc_run(
            ["delete", "clusterserviceversion", name, "-n", operator_namespace, "--ignore-not-found"],
            capture_output=True,
            check=False,
            timeout=120,
        )


def _delete_operator_scoped_installplans(operator_namespace: str, operator_name: str) -> None:
    listed = oc_run(
        ["get", "installplan", "-n", operator_namespace, "-o", "json"],
        capture_output=True,
        check=False,
        timeout=60,
    )
    if listed.returncode != 0:
        return
    try:
        doc = json.loads(listed.stdout or "{}")
    except json.JSONDecodeError:
        return
    for item in doc.get("items") or []:
        if not isinstance(item, dict):
            continue
        meta = item.get("metadata") or {}
        spec = item.get("spec") or {}
        ip_name = str(meta.get("name") or "").strip()
        csv_names = [str(n) for n in (spec.get("clusterServiceVersionNames") or []) if str(n).strip()]
        if not ip_name or not csv_names:
            continue
        if all(_operator_csv_package_name(n) == operator_name for n in csv_names):
            oc_run(
                ["delete", "installplan", ip_name, "-n", operator_namespace, "--ignore-not-found"],
                capture_output=True,
                check=False,
                timeout=120,
            )


def reset_stale_operator_install(
    operator_namespace: str,
    operator_name: str,
    catalog_name: str,
) -> None:
    """Drop leftover subscription/catalog state from a prior failed run on external clusters."""
    print(
        f"Resetting prior OLM install state for {operator_name} "
        f"(namespace {operator_namespace}, catalog {catalog_name})..."
    )
    for args in (
        ["delete", "subscription", operator_name, "-n", operator_namespace],
        ["delete", "operatorgroup", "--all", "-n", operator_namespace],
        ["delete", "catalogsource", catalog_name, "-n", "openshift-marketplace"],
    ):
        oc_run([*args, "--ignore-not-found"], capture_output=True, check=False, timeout=120)
    _delete_operator_scoped_installplans(operator_namespace, operator_name)
    _delete_operator_scoped_csvs(operator_namespace, operator_name)
    delete_failed_olm_bundle_unpack_jobs()
    deadline = time.time() + 120
    while time.time() < deadline:
        r = oc_run(
            ["get", "subscription", operator_name, "-n", operator_namespace],
            capture_output=True,
            check=False,
            timeout=30,
        )
        if r.returncode != 0:
            print("✓ Prior subscription removed")
            return
        time.sleep(5)
    print("⚠ Prior subscription still present after 120s; continuing install")


def normalize_odh_olm_targets(operator_name: str, operator_namespace: str, update_channel: str) -> tuple[str, str]:
    """Align ODH catalog installs with Jenkins (odhTestConfigOperator / generateTestConfigFile).

    For ``odh-stable`` (Konflux ODH catalog) Jenkins uses the rhods-operator OLM package and
    downstream (RHOAI) operator namespace — same as ``install-operator.sh rhods-operator`` in rhoai-e2e.
    """
    if update_channel == "odh-stable":
        if operator_name != "rhods-operator" or operator_namespace != "redhat-ods-operator":
            print(
                "ODH odh-stable: using Jenkins/rhoai-e2e targets "
                f"rhods-operator / redhat-ods-operator "
                f"(was {operator_name!r} / {operator_namespace!r})"
            )
        return "rhods-operator", "redhat-ods-operator"
    if update_channel == "odh-nightlies" and operator_name == "opendatahub-operator":
        print("ODH odh-nightlies: using rhods-operator OLM package (Jenkins default)")
        return "rhods-operator", operator_namespace
    return operator_name, operator_namespace


def resolve_olminstall_manifest(operator_install_dir: Path, operator_name: str) -> Path:
    """Return path to ``resources/install-<operator>.yaml`` in the cloned olminstall repo."""
    resources_dir = (operator_install_dir / "resources").resolve()
    manifest = operator_install_dir / "resources" / f"install-{operator_name}.yaml"
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


def pick_succeeded_csv_version(
    namespace: str,
    olminstall_operator: str,
    *,
    timeout: float = 120,
) -> str | None:
    r = oc_run(
        ["get", "csv", "-n", namespace, "-o", "json"],
        capture_output=True,
        text=True,
        check=False,
        timeout=timeout,
    )
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


def _operator_csv_phase(
    namespace: str,
    olminstall_operator: str,
) -> tuple[str | None, str | None]:
    """Return (csv_name, phase) for the target operator CSV, if present."""
    r = oc_run(
        ["get", "csv", "-n", namespace, "-o", "json"],
        capture_output=True,
        text=True,
        check=False,
        timeout=60,
    )
    if r.returncode != 0:
        return None, None
    try:
        data = json.loads(r.stdout or "{}")
    except json.JSONDecodeError:
        return None, None
    if not isinstance(data, dict):
        return None, None
    op_pat = re.compile(re.escape(olminstall_operator), re.I)
    for item in data.get("items") or []:
        md_name = (item.get("metadata") or {}).get("name") or ""
        disp = ((item.get("spec") or {}).get("displayName")) or ""
        if not (md_name.startswith(olminstall_operator) or (disp and op_pat.search(disp))):
            continue
        phase = ((item.get("status") or {}).get("phase") or "").strip() or None
        return md_name or None, phase
    return None, None


def _subscription_target_csv(namespace: str, operator_name: str) -> str | None:
    """Return the CSV OLM is installing (current > installed > starting)."""
    for jsonpath in (
        "{.status.currentCSV}",
        "{.status.installedCSV}",
        "{.spec.startingCSV}",
    ):
        r = oc_run(
            ["get", "subscription", operator_name, "-n", namespace, "-o", f"jsonpath={jsonpath}"],
            capture_output=True,
            text=True,
            check=False,
            timeout=30,
        )
        if r.returncode != 0:
            continue
        csv_name = (r.stdout or "").strip()
        if csv_name:
            return csv_name
    return None


def _named_csv_phase(namespace: str, csv_name: str) -> tuple[str | None, str | None]:
    """Return (csv_name, phase) for a specific CSV resource."""
    r = oc_run(
        ["get", "csv", csv_name, "-n", namespace, "-o", "json"],
        capture_output=True,
        text=True,
        check=False,
        timeout=60,
    )
    if r.returncode != 0:
        return csv_name, None
    try:
        item = json.loads(r.stdout or "{}")
    except json.JSONDecodeError:
        return csv_name, None
    if not isinstance(item, dict):
        return csv_name, None
    phase = ((item.get("status") or {}).get("phase") or "").strip() or None
    return csv_name, phase


def _named_csv_succeeded_version(namespace: str, csv_name: str) -> str | None:
    """Return spec.version when *csv_name* exists and phase is Succeeded."""
    r = oc_run(
        ["get", "csv", csv_name, "-n", namespace, "-o", "json"],
        capture_output=True,
        text=True,
        check=False,
        timeout=60,
    )
    if r.returncode != 0:
        return None
    try:
        item = json.loads(r.stdout or "{}")
    except json.JSONDecodeError:
        return None
    if not isinstance(item, dict):
        return None
    if ((item.get("status") or {}).get("phase") or "").strip() != "Succeeded":
        return None
    ver = ((item.get("spec") or {}).get("version") or "").strip()
    return ver or None


def _bundle_unpack_failure_recoverable(failure: str) -> bool:
    return "DeadlineExceeded" in failure or "deadline" in failure.lower()


def _max_bundle_unpack_recoveries() -> int:
    try:
        return int(os.environ.get("OLM_BUNDLE_UNPACK_RECOVERIES", "3"))
    except ValueError:
        return 3


def _bundle_unpack_stall_sec() -> int:
    try:
        return int(os.environ.get("OLM_BUNDLE_UNPACK_STALL_SEC", "900"))
    except ValueError:
        return 900


def _bundle_unpack_no_job_kick_sec() -> int:
    try:
        return int(os.environ.get("OLM_BUNDLE_UNPACK_NO_JOB_KICK_SEC", "600"))
    except ValueError:
        return 600


def count_olm_bundle_unpack_jobs(
    *,
    marketplace_namespace: str = _MARKETPLACE_NS,
    include_active: bool = True,
) -> int:
    """Count marketplace Jobs that look like OLM bundle-unpack work."""
    r = oc_run(
        ["get", "jobs", "-n", marketplace_namespace, "-o", "json"],
        capture_output=True,
        check=False,
        timeout=120,
    )
    if r.returncode != 0:
        return 0
    try:
        items = json.loads(r.stdout or "{}").get("items") or []
    except json.JSONDecodeError:
        return 0
    count = 0
    for job in items:
        if not isinstance(job, dict):
            continue
        if not include_active and not _job_is_failed(job):
            continue
        if _job_looks_like_bundle_unpack(job):
            count += 1
    return count


def log_marketplace_bundle_unpack_state(
    *,
    marketplace_namespace: str = _MARKETPLACE_NS,
) -> None:
    """Log unpack Jobs and olm.bundle pods for triage when unpack is stuck."""
    print(
        f"Marketplace bundle-unpack state ({marketplace_namespace}):",
        flush=True,
    )
    oc_run(
        ["get", "jobs", "-n", marketplace_namespace, "-o", "wide"],
        capture_output=False,
        check=False,
        timeout=120,
    )
    oc_run(
        [
            "get",
            "pods",
            "-n",
            marketplace_namespace,
            "-l",
            "olm.bundle",
            "-o",
            "wide",
        ],
        capture_output=False,
        check=False,
        timeout=120,
    )


def _subscription_status_last_updated(operator_name: str, operator_namespace: str) -> str | None:
    r = oc_run(
        ["get", "subscription", operator_name, "-n", operator_namespace, "-o", "json"],
        capture_output=True,
        check=False,
        timeout=60,
    )
    if r.returncode != 0:
        return None
    try:
        last = json.loads(r.stdout or "{}").get("status", {}).get("lastUpdated")
    except json.JSONDecodeError:
        return None
    text = str(last or "").strip()
    return text or None


def wait_for_succeeded_csv_version(
    namespace: str,
    olminstall_operator: str,
    *,
    timeout_sec: float | None = None,
    poll_sec: float = 15,
) -> str | None:
    """Poll until the operator CSV reaches Succeeded (install-operator.sh may return early)."""
    if timeout_sec is None:
        try:
            timeout_sec = float(os.environ.get("OPERATOR_CSV_WAIT_SEC", "1200"))
        except ValueError:
            timeout_sec = 1200.0
    deadline = time.monotonic() + timeout_sec
    last_phase: str | None = None
    poll_count = 0
    bundle_unpack_recoveries = 0
    max_bundle_unpack_recoveries = _max_bundle_unpack_recoveries()
    print(
        f"Waiting for CSV {olminstall_operator} Succeeded in {namespace} "
        f"(up to {int(timeout_sec)}s)...",
        flush=True,
    )
    while time.monotonic() < deadline:
        unpack_failure = subscription_bundle_unpack_failed(olminstall_operator, namespace)
        if unpack_failure and _bundle_unpack_failure_recoverable(unpack_failure):
            if bundle_unpack_recoveries < max_bundle_unpack_recoveries:
                bundle_unpack_recoveries += 1
                print(
                    f"OLM bundle unpack DeadlineExceeded during CSV wait for "
                    f"{olminstall_operator} — recovering "
                    f"({bundle_unpack_recoveries}/{max_bundle_unpack_recoveries})...",
                    flush=True,
                )
                recover_bundle_unpack_deadline_exceeded(olminstall_operator, namespace)
                time.sleep(poll_sec)
                continue
        if poll_count % 4 == 0:
            try:
                from install.approve_transitive_installplans import approve_pending_installplans

                approved = approve_pending_installplans("openshift-operators")
                if approved:
                    print(
                        f"Approved {approved} gateway-stack InstallPlan(s) in openshift-operators",
                        flush=True,
                    )
            except Exception as exc:
                print(f"WARN: gateway InstallPlan approval failed ({exc})", flush=True)
        poll_count += 1
        target_csv = _subscription_target_csv(namespace, olminstall_operator)
        if target_csv:
            ver = _named_csv_succeeded_version(namespace, target_csv)
            if ver:
                print(f"✓ Operator CSV Succeeded (version={ver})", flush=True)
                return ver
            csv_name, phase = _named_csv_phase(namespace, target_csv)
        else:
            ver = pick_succeeded_csv_version(namespace, olminstall_operator, timeout=60)
            if ver:
                print(f"✓ Operator CSV Succeeded (version={ver})", flush=True)
                return ver
            csv_name, phase = _operator_csv_phase(namespace, olminstall_operator)
        if phase == "Failed":
            print(f"❌ Operator CSV {csv_name or olminstall_operator} is Failed", flush=True)
            return None
        if phase and phase != last_phase:
            print(f"  Operator CSV {csv_name or olminstall_operator} phase={phase}", flush=True)
            last_phase = phase
        time.sleep(poll_sec)
    print(
        f"❌ Operator CSV {olminstall_operator} did not reach Succeeded within {int(timeout_sec)}s",
        flush=True,
    )
    return None


def _packagemanifest_doc_for_catalog(package_name: str, catalog_name: str) -> dict | None:
    """Return PackageManifest for ``package_name`` from ``catalog_name`` when OLM exposes duplicates."""
    r = oc_run(
        [
            "get",
            "packagemanifest",
            "-l",
            f"catalog={catalog_name}",
            "-n",
            "openshift-marketplace",
            "-o",
            "json",
        ],
        capture_output=True,
        text=True,
        check=False,
        timeout=60,
    )
    if r.returncode == 0:
        try:
            listed = json.loads(r.stdout or "{}")
        except json.JSONDecodeError:
            listed = {}
        if isinstance(listed, dict):
            for item in listed.get("items") or []:
                if not isinstance(item, dict):
                    continue
                if (item.get("metadata") or {}).get("name") == package_name:
                    return item
    r = oc_run(
        [
            "get",
            "packagemanifest",
            package_name,
            "-n",
            "openshift-marketplace",
            "-o",
            "json",
        ],
        capture_output=True,
        text=True,
        check=False,
        timeout=60,
    )
    if r.returncode != 0:
        return None
    try:
        doc = json.loads(r.stdout or "{}")
    except json.JSONDecodeError:
        return None
    if not isinstance(doc, dict):
        return None
    status = doc.get("status") or {}
    if (status.get("catalogSource") or "").strip() != catalog_name:
        return None
    return doc


def packagemanifest_channel_csv(package_name: str, catalog_name: str, channel: str) -> str | None:
    """Return currentCSV for ``channel`` when ``catalogSource`` matches, else None."""
    doc = _packagemanifest_doc_for_catalog(package_name, catalog_name)
    if not doc:
        return None
    status = doc.get("status") or {}
    if (status.get("catalogSource") or "").strip() != catalog_name:
        return None
    for ch in status.get("channels") or []:
        if not isinstance(ch, dict):
            continue
        if (ch.get("name") or "").strip() != channel:
            continue
        csv = (ch.get("currentCSV") or "").strip()
        if csv:
            return csv
    return None


def wait_packagemanifest_ready(
    package_name: str,
    catalog_name: str,
    channel: str,
    deadline_s: float,
) -> str | None:
    """Wait until OLM exposes ``package_name`` on ``channel`` from ``catalog_name``; return currentCSV."""
    iteration = 0
    while time.time() < deadline_s:
        csv = packagemanifest_channel_csv(package_name, catalog_name, channel)
        if csv:
            print(
                f"✓ PackageManifest {package_name}/{channel} currentCSV={csv} "
                f"(catalog {catalog_name})"
            )
            return csv
        iteration += 1
        print(
            f"  Waiting for PackageManifest {package_name}/{channel} from {catalog_name} "
            f"(iter {iteration})"
        )
        time.sleep(15)
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


def ensure_operatorgroup_bundle_unpack_annotations(
    operator_namespace: str,
    *,
    unpack_timeout: str | None = None,
    min_retry_interval: str | None = None,
) -> None:
    """Raise OLM unpack Job ActiveDeadlineSeconds via OperatorGroup annotations.

    Default OLM unpack Jobs use activeDeadlineSeconds=600. Large FBC bundles on
    HyperShift often exceed 10m and leave BundleUnpackFailed/DeadlineExceeded.
    """
    timeout = unpack_timeout or default_bundle_unpack_job_timeout()
    min_retry_interval = min_retry_interval or default_bundle_unpack_min_retry_interval()
    r = oc_run(
        ["get", "operatorgroup", "-n", operator_namespace, "-o", "json"],
        capture_output=True,
        check=False,
        timeout=60,
    )
    if r.returncode != 0:
        print(
            f"WARN: no OperatorGroup in {operator_namespace}; "
            "cannot set bundle-unpack-timeout yet",
            flush=True,
        )
        return
    try:
        items = json.loads(r.stdout or "{}").get("items") or []
    except json.JSONDecodeError:
        items = []
    if not items:
        print(
            f"WARN: no OperatorGroup in {operator_namespace}; "
            "cannot set bundle-unpack-timeout yet",
            flush=True,
        )
        return
    for item in items:
        if not isinstance(item, dict):
            continue
        name = str((item.get("metadata") or {}).get("name") or "").strip()
        if not name:
            continue
        patch = {
            "metadata": {
                "annotations": {
                    _BUNDLE_UNPACK_TIMEOUT_ANN: timeout,
                    _BUNDLE_UNPACK_RETRY_ANN: min_retry_interval,
                }
            }
        }
        pr = oc_run(
            [
                "patch",
                "operatorgroup",
                name,
                "-n",
                operator_namespace,
                "--type=merge",
                "-p",
                json.dumps(patch),
            ],
            capture_output=True,
            check=False,
            timeout=60,
        )
        if pr.returncode == 0:
            print(
                f"✓ OperatorGroup/{name}: {_BUNDLE_UNPACK_TIMEOUT_ANN}={timeout}, "
                f"{_BUNDLE_UNPACK_RETRY_ANN}={min_retry_interval}",
                flush=True,
            )
        else:
            print(
                f"WARN: could not annotate OperatorGroup/{name}: "
                f"{(pr.stderr or pr.stdout or '').strip()}",
                flush=True,
            )


def _job_is_failed(job: dict[str, Any]) -> bool:
    status = job.get("status") or {}
    if int(status.get("failed") or 0) > 0:
        return True
    for cond in status.get("conditions") or []:
        if not isinstance(cond, dict):
            continue
        if (
            str(cond.get("type") or "") == "Failed"
            and str(cond.get("status") or "").lower() == "true"
        ):
            return True
    return False


def _job_looks_like_bundle_unpack(job: dict[str, Any]) -> bool:
    """True for OLM ConfigMap unpack Jobs (extract + pull containers)."""
    pod_spec = ((job.get("spec") or {}).get("template") or {}).get("spec") or {}
    containers = list(pod_spec.get("containers") or []) + list(pod_spec.get("initContainers") or [])
    names = {str(c.get("name") or "") for c in containers if isinstance(c, dict)}
    if "extract" in names and ("pull" in names or "util" in names):
        return True
    for c in containers:
        if not isinstance(c, dict):
            continue
        for env in c.get("env") or []:
            if not isinstance(env, dict):
                continue
            if str(env.get("name") or "") != "CONTAINER_IMAGE":
                continue
            val = str(env.get("value") or "")
            if "rhoai" in val or "odh-operator-bundle" in val or "rhods" in val:
                return True
    return False


def delete_failed_olm_bundle_unpack_jobs(
    *,
    marketplace_namespace: str = _MARKETPLACE_NS,
    include_active: bool = False,
) -> int:
    """Delete Failed (and optionally active) unpack Jobs so OLM can retry."""
    r = oc_run(
        ["get", "jobs", "-n", marketplace_namespace, "-o", "json"],
        capture_output=True,
        check=False,
        timeout=120,
    )
    if r.returncode != 0:
        return 0
    try:
        items = json.loads(r.stdout or "{}").get("items") or []
    except json.JSONDecodeError:
        return 0
    deleted = 0
    for job in items:
        if not isinstance(job, dict):
            continue
        if not include_active and not _job_is_failed(job):
            continue
        if not _job_looks_like_bundle_unpack(job):
            continue
        # Skip completed successful jobs when sweeping active/failed.
        if include_active and not _job_is_failed(job):
            status = job.get("status") or {}
            if int(status.get("succeeded") or 0) > 0:
                continue
        name = str((job.get("metadata") or {}).get("name") or "").strip()
        if not name:
            continue
        kind = "failed" if _job_is_failed(job) else "active"
        print(
            f"Deleting {kind} OLM bundle-unpack Job/{name} in {marketplace_namespace}...",
            flush=True,
        )
        oc_run(
            ["delete", "job", name, "-n", marketplace_namespace, "--ignore-not-found", "--wait=false"],
            capture_output=True,
            check=False,
            timeout=60,
        )
        oc_run(
            [
                "delete",
                "configmap",
                name,
                "-n",
                marketplace_namespace,
                "--ignore-not-found",
                "--wait=false",
            ],
            capture_output=True,
            check=False,
            timeout=60,
        )
        deleted += 1
    if deleted:
        label = "failed/active" if include_active else "failed"
        print(f"✓ Removed {deleted} {label} OLM bundle-unpack Job(s)", flush=True)
    return deleted


def recover_bundle_unpack_deadline_exceeded(
    operator_name: str,
    operator_namespace: str,
) -> int:
    """Clear stale unpack Jobs (failed or stuck-active) and ensure OG unpack timeout."""
    try:
        from install.cluster_registry import ensure_openshift_release_dev_pull_auth

        ensure_openshift_release_dev_pull_auth()
    except Exception as exc:
        print(f"WARN: openshift-release-dev pull-secret heal failed ({exc})", flush=True)
    ensure_operatorgroup_bundle_unpack_annotations(operator_namespace)
    deleted = delete_failed_olm_bundle_unpack_jobs(include_active=True)
    if deleted == 0:
        print(
            f"WARN: BundleUnpack recover for {operator_name} but no unpack Jobs found "
            f"in {_MARKETPLACE_NS}",
            flush=True,
        )
        log_marketplace_bundle_unpack_state()
    return deleted


def kick_subscription_bundle_unpack(
    operator_name: str,
    operator_namespace: str,
    manifest_path: Path,
) -> bool:
    """Re-create the Subscription when OLM reports unpacking but never schedules Jobs."""
    print(
        f"Kick OLM bundle unpack for {operator_name}: delete subscription and re-apply "
        f"{manifest_path}",
        flush=True,
    )
    log_marketplace_bundle_unpack_state()
    oc_run(
        ["delete", "subscription", operator_name, "-n", operator_namespace, "--ignore-not-found"],
        capture_output=True,
        check=False,
        timeout=120,
    )
    _delete_operator_scoped_installplans(operator_namespace, operator_name)
    time.sleep(5)
    oc_run(["apply", "-f", str(manifest_path)], check=True, capture_output=True, timeout=120)
    ensure_operatorgroup_bundle_unpack_annotations(operator_namespace)
    return True


def _subscription_bundle_unpack_condition(
    operator_name: str, operator_namespace: str, *, condition_type: str
) -> dict | None:
    """Return a Subscription status condition of *condition_type*, if present."""
    r = oc_run(
        ["get", "subscription", operator_name, "-n", operator_namespace, "-o", "json"],
        capture_output=True,
        check=False,
        timeout=60,
    )
    if r.returncode != 0:
        return None
    try:
        conditions = json.loads(r.stdout or "{}").get("status", {}).get("conditions") or []
    except json.JSONDecodeError:
        return None
    for cond in conditions:
        if isinstance(cond, dict) and cond.get("type") == condition_type:
            return cond
    return None


def subscription_bundle_unpack_in_progress(operator_name: str, operator_namespace: str) -> bool:
    """True when the subscription status reports BundleUnpacking=True."""
    cond = _subscription_bundle_unpack_condition(
        operator_name, operator_namespace, condition_type="BundleUnpacking"
    )
    return cond is not None and str(cond.get("status", "")).lower() == "true"


def subscription_bundle_unpack_failed(operator_name: str, operator_namespace: str) -> str | None:
    """Return failure reason when OLM reports a terminal bundle unpack failure."""
    failed = _subscription_bundle_unpack_condition(
        operator_name, operator_namespace, condition_type="BundleUnpackFailed"
    )
    if failed is not None and str(failed.get("status", "")).lower() == "true":
        reason = str(failed.get("reason") or "").strip()
        message = str(failed.get("message") or "").strip()
        return message or reason or "BundleUnpackFailed"
    cond = _subscription_bundle_unpack_condition(
        operator_name, operator_namespace, condition_type="BundleUnpacking"
    )
    if cond is None:
        return None
    status = str(cond.get("status", "")).lower()
    if status == "true":
        return None
    reason = str(cond.get("reason") or "").strip()
    message = str(cond.get("message") or "").strip()
    detail = message or reason or status or "unknown"
    if status == "false" and reason.lower() not in ("", "unpacking", "bundleunpacking"):
        return detail
    if reason.lower() in ("unpackfailed", "failed", "error"):
        return detail
    return None


def wait_subscription_bundle_unpacked(
    operator_name: str,
    operator_namespace: str,
    deadline_s: float,
    *,
    subscription_manifest: Path | None = None,
) -> bool:
    """Wait for OLM to finish unpacking FBC bundle related images before InstallPlan."""
    r = oc_run(
        ["get", "subscription", operator_name, "-n", operator_namespace],
        capture_output=True,
        check=False,
        timeout=60,
    )
    if r.returncode != 0:
        print(f"⚠ Subscription {operator_name} not found in {operator_namespace}")
        return False
    ensure_operatorgroup_bundle_unpack_annotations(operator_namespace)
    max_recoveries = _max_bundle_unpack_recoveries()
    recoveries = 0
    no_job_kick_sec = _bundle_unpack_no_job_kick_sec()
    no_jobs_since: float | None = None
    stall_sec = _bundle_unpack_stall_sec()
    last_updated_seen: str | None = None
    stall_since: float | None = None

    def _try_recover(failure: str) -> bool:
        nonlocal recoveries, last_updated_seen, stall_since, no_jobs_since
        if not _bundle_unpack_failure_recoverable(failure):
            return False
        if recoveries >= max_recoveries:
            return False
        recoveries += 1
        print(
            f"OLM bundle unpack DeadlineExceeded for {operator_name} — "
            f"recovering ({recoveries}/{max_recoveries})...",
            flush=True,
        )
        deleted = recover_bundle_unpack_deadline_exceeded(operator_name, operator_namespace)
        if deleted > 0:
            last_updated_seen = None
            stall_since = None
            no_jobs_since = None
            return True
        if subscription_manifest is not None:
            kick_subscription_bundle_unpack(
                operator_name, operator_namespace, subscription_manifest
            )
            last_updated_seen = None
            stall_since = None
            no_jobs_since = None
            return True
        return False

    unpack_failure = subscription_bundle_unpack_failed(operator_name, operator_namespace)
    if unpack_failure:
        if _try_recover(unpack_failure):
            unpack_failure = None
        else:
            print(
                f"❌ OLM bundle unpack failed for {operator_name}: {unpack_failure}",
                file=sys.stderr,
                flush=True,
            )
            return False
    if not subscription_bundle_unpack_in_progress(operator_name, operator_namespace):
        if unpack_failure is None and not subscription_bundle_unpack_failed(
            operator_name, operator_namespace
        ):
            print("✓ OLM bundle unpack not in progress")
            return True
    print(
        f"Waiting for OLM bundle unpack on {operator_name} "
        f"(FBC catalogs can have 100+ related images on HyperShift)..."
    )
    iteration = 0
    while time.time() < deadline_s:
        if subscription_bundle_unpack_in_progress(operator_name, operator_namespace):
            job_count = count_olm_bundle_unpack_jobs(include_active=True)
            if job_count == 0:
                if no_jobs_since is None:
                    no_jobs_since = time.time()
                elif (
                    subscription_manifest is not None
                    and time.time() - no_jobs_since >= no_job_kick_sec
                    and recoveries < max_recoveries
                ):
                    recoveries += 1
                    print(
                        f"OLM bundle unpack in progress for {operator_name} but no unpack "
                        f"Jobs for {no_job_kick_sec}s — kick "
                        f"({recoveries}/{max_recoveries})...",
                        flush=True,
                    )
                    kick_subscription_bundle_unpack(
                        operator_name, operator_namespace, subscription_manifest
                    )
                    last_updated_seen = None
                    stall_since = None
                    no_jobs_since = None
                    time.sleep(15)
                    continue
            else:
                no_jobs_since = None
            updated = _subscription_status_last_updated(operator_name, operator_namespace)
            if updated and updated == last_updated_seen:
                if stall_since is None:
                    stall_since = time.time()
                elif time.time() - stall_since >= stall_sec:
                    if _try_recover(
                        f"bundle unpacking stalled: subscription status lastUpdated "
                        f"unchanged for {stall_sec}s (DeadlineExceeded)"
                    ):
                        last_updated_seen = None
                        stall_since = None
                        time.sleep(15)
                        continue
            else:
                last_updated_seen = updated
                stall_since = None
        if not subscription_bundle_unpack_in_progress(operator_name, operator_namespace):
            unpack_failure = subscription_bundle_unpack_failed(operator_name, operator_namespace)
            if unpack_failure:
                if _try_recover(unpack_failure):
                    time.sleep(15)
                    continue
                print(
                    f"❌ OLM bundle unpack failed for {operator_name}: {unpack_failure}",
                    file=sys.stderr,
                    flush=True,
                )
                return False
            print("✓ OLM bundle unpack complete")
            return True
        iteration += 1
        if iteration % 6 == 0:
            oc_run(
                ["get", "subscription", operator_name, "-n", operator_namespace, "-o", "yaml"],
                capture_output=False,
                check=False,
                timeout=60,
            )
            if subscription_bundle_unpack_in_progress(operator_name, operator_namespace):
                log_marketplace_bundle_unpack_state()
        time.sleep(10)
    log_marketplace_bundle_unpack_state()
    return False


def main() -> int:
    from install.install_phases import run_install

    return run_install()



if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        fail("❌ Interrupted")
    except json.JSONDecodeError as exc:
        fail(f"❌ Invalid JSON in command output: {exc}")
    except subprocess.TimeoutExpired:
        fail("❌ Command timed out (install step limit is 90m)")
    except subprocess.CalledProcessError as exc:
        if exc.stderr:
            print(exc.stderr, file=sys.stderr, end="" if exc.stderr.endswith("\n") else "\n")
        if exc.stdout:
            print(exc.stdout, file=sys.stderr, end="" if exc.stdout.endswith("\n") else "\n")
        fail(f"❌ Command failed (exit {exc.returncode})")
