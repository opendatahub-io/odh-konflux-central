#!/usr/bin/env python3
"""ROSA HCP / HyperShift pull setup via Kyverno (Jenkins addICSP.groovy ROSA_HCP path).

On guest clusters where ImageDigestMirrorSet is admission-blocked, mirror
``registry.redhat.io/rhoai`` → ``quay.io/rhoai`` with Kyverno and propagate
``pull-secret-quay`` to namespaces and Pods instead of ICSP/IDMS.

Jenkins loads Kyverno ClusterPolicy YAML from Vault ``apps/rhods-ci/aws/rosa-hcp``
(``rosahcp-psquay-*``, ``rosahcp-quay-kyverno-authenticate``). Konflux mounts the
same content from tenant secret ``rhoai-e2e-rosa-hcp-kyverno`` (see
``vault_shift_left_konflux_secrets.txt`` block 8).
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

from install.dependency_operators import _namespace_phase, unblock_terminating_namespace
from install.cluster_registry import extract_quay_auth, merge_docker_auths
from k8s.oc_util import run_oc

KYVERNO_INSTALL_URL = "https://github.com/kyverno/kyverno/releases/download/v1.12.4/install.yaml"
PULL_SECRET_NAME = "pull-secret-quay"
KYVERNO_CONFIG_DIR = Path(__file__).resolve().parent.parent / "config" / "kyverno"
DEFAULT_VAULT_POLICY_DIR = Path("/var/secret/rosa-hcp-kyverno")
VAULT_POLICY_FILES: tuple[tuple[str, str], ...] = (
    ("sync-secrets.yaml", "sync-secrets"),
    ("add-imagepullsecrets.yaml", "add-imagepullsecrets"),
    ("kyverno-authenticate.yaml", "replace-image-registry"),
)
LEGACY_IN_REPO_POLICY_NAMES = ("sync-secrets", "add-imagepullsecrets", "replace-rhoai-registry")
DEFAULT_TARGET_NAMESPACES = (
    "openshift-marketplace",
    "redhat-ods-operator",
    "redhat-ods-applications",
    "redhat-ods-monitoring",
    "opendatahub",
    "odh-model-registries",
    "rhoai-model-registries",
)


def vault_policy_dir() -> Path | None:
    raw = os.environ.get("ROSA_HCP_KYVERNO_POLICY_DIR", str(DEFAULT_VAULT_POLICY_DIR)).strip()
    if not raw:
        return None
    path = Path(raw)
    if not path.is_dir():
        return None
    if all((path / filename).is_file() for filename, _ in VAULT_POLICY_FILES):
        return path
    return None


def active_kyverno_policy_names() -> tuple[str, ...]:
    if vault_policy_dir():
        return tuple(name for _, name in VAULT_POLICY_FILES)
    return LEGACY_IN_REPO_POLICY_NAMES


def resolve_kyverno_policy_paths() -> list[tuple[Path, str]]:
    vdir = vault_policy_dir()
    if vdir:
        return [(vdir / filename, policy_name) for filename, policy_name in VAULT_POLICY_FILES]
    return [(KYVERNO_CONFIG_DIR / f"{name}.yaml", name) for name in LEGACY_IN_REPO_POLICY_NAMES]


def is_hypershift_managed_cluster() -> bool:
    r = run_oc(
        ["get", "imagedigestmirrorset", "cluster", "-o", "jsonpath={.metadata.labels}"],
        capture_output=True,
        check=False,
        timeout=30,
    )
    if r.returncode != 0:
        return False
    return "hypershift.openshift.io/managed" in (r.stdout or "")


def _cluster_policy_ready(name: str) -> bool:
    r = run_oc(
        ["get", "clusterpolicy", name, "-o", "jsonpath={.status.ready}"],
        capture_output=True,
        check=False,
        timeout=30,
    )
    return r.returncode == 0 and (r.stdout or "").strip().lower() == "true"


def _registry_replace_policy_ready() -> bool:
    """Vault policies use replace-image-registry; in-repo legacy uses replace-rhoai-registry."""
    return _cluster_policy_ready("replace-image-registry") or _cluster_policy_ready(
        "replace-rhoai-registry"
    )


def rosa_hcp_pull_setup_ready() -> bool:
    ns = run_oc(["get", "ns", "kyverno"], capture_output=True, check=False, timeout=30)
    if ns.returncode != 0:
        return False
    for name in active_kyverno_policy_names():
        if name in ("replace-rhoai-registry", "replace-image-registry"):
            if not _registry_replace_policy_ready():
                return False
            continue
        if not _cluster_policy_ready(name):
            return False
    r = run_oc(
        ["get", "secret", PULL_SECRET_NAME, "-n", "openshift-config"],
        capture_output=True,
        check=False,
        timeout=30,
    )
    return r.returncode == 0


def _clean_secret_metadata(secret_json: dict[str, Any]) -> dict[str, Any]:
    obj = dict(secret_json)
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
    obj["metadata"] = md
    return obj


def _apply_secret_to_namespace(secret_json: dict[str, Any], namespace: str) -> None:
    obj = _clean_secret_metadata(secret_json)
    md = dict(obj.get("metadata") or {})
    md["namespace"] = namespace
    obj["metadata"] = md
    run_oc(["apply", "-f", "-"], input_text=json.dumps(obj), check=True, timeout=120)


def _ensure_namespace(name: str) -> None:
    ns_yaml = run_oc(
        ["create", "namespace", name, "--dry-run=client", "-o", "yaml"],
        capture_output=True,
        check=True,
        timeout=60,
    ).stdout
    run_oc(["apply", "-f", "-"], input_text=ns_yaml, check=False, timeout=60)


def ensure_pull_secret_quay(quay_dockerconfig: dict[str, Any], target_namespaces: list[str]) -> None:
    """Create openshift-config/pull-secret-quay and copy to marketplace + operator namespaces."""
    quay_auth = extract_quay_auth(quay_dockerconfig.get("auths") or {})
    if not quay_auth:
        raise RuntimeError("quay.io/rhoai auth missing from dockerconfig")
    creds = merge_docker_auths(quay_dockerconfig, {"auths": {"quay.io": {"auth": quay_auth}}})
    creds_json = json.dumps(creds, separators=(",", ":"))
    manifest = {
        "apiVersion": "v1",
        "kind": "Secret",
        "metadata": {"name": PULL_SECRET_NAME, "namespace": "openshift-config"},
        "type": "kubernetes.io/dockerconfigjson",
        "stringData": {".dockerconfigjson": creds_json},
    }
    run_oc(["apply", "-f", "-"], input_text=json.dumps(manifest), check=True, timeout=120)
    print(f"✓ {PULL_SECRET_NAME} in openshift-config")

    src = json.loads(
        run_oc(
            ["get", "secret", PULL_SECRET_NAME, "-n", "openshift-config", "-o", "json"],
            capture_output=True,
            check=True,
            timeout=60,
        ).stdout
    )
    _apply_secret_to_namespace(src, "openshift-marketplace")
    print(f"✓ {PULL_SECRET_NAME} copied to openshift-marketplace")

    for ns in target_namespaces:
        ns = ns.strip()
        if not ns or ns in ("openshift-config", "openshift-marketplace"):
            continue
        phase = _namespace_phase(ns)
        if phase == "Terminating":
            unblock_terminating_namespace(ns)
            run_oc(
                ["wait", "namespace", ns, "--for=delete", "--timeout=60s"],
                capture_output=True,
                check=False,
                timeout=70,
            )
        elif phase:
            _apply_secret_to_namespace(src, ns)
            continue
        _ensure_namespace(ns)
        run_oc(
            ["wait", "namespace", ns, "--for=jsonpath={.status.phase}=Active", "--timeout=30s"],
            capture_output=True,
            check=False,
            timeout=40,
        )
        _apply_secret_to_namespace(src, ns)
    print(f"✓ {PULL_SECRET_NAME} copied to target namespaces: {', '.join(target_namespaces)}")


def install_kyverno() -> None:
    if run_oc(["get", "ns", "kyverno"], capture_output=True, check=False, timeout=30).returncode == 0:
        print("Kyverno namespace already exists")
    else:
        print("Installing Kyverno v1.12.4...")
        run_oc(
            [
                "apply",
                "--server-side",
                "--force-conflicts",
                "--field-manager=odh-olminstall-kyverno",
                "-f",
                KYVERNO_INSTALL_URL,
            ],
            capture_output=True,
            check=True,
            timeout=300,
        )
    run_oc(
        ["wait", "deployment", "-n", "kyverno", "--all", "--for=condition=Available", "--timeout=300s"],
        capture_output=True,
        check=False,
        timeout=320,
    )
    run_oc(
        ["rollout", "status", "deployment", "-n", "kyverno", "--timeout=120s"],
        capture_output=True,
        check=False,
        timeout=130,
    )
    print("✓ Kyverno available")


def _delete_stale_kyverno_policies() -> None:
    for name in (
        "sync-secrets",
        "add-imagepullsecrets",
        "replace-rhoai-registry",
        "replace-image-registry",
    ):
        run_oc(
            ["delete", "clusterpolicy", name, "--ignore-not-found", "--timeout=60s"],
            capture_output=True,
            check=False,
            timeout=70,
        )


def apply_kyverno_policies() -> None:
    policies = resolve_kyverno_policy_paths()
    for path, _name in policies:
        if not path.is_file():
            raise FileNotFoundError(path)
    source = "Vault tenant secret" if vault_policy_dir() else "in-repo config/kyverno"
    print(f"Applying Kyverno policies from {source}...")
    _delete_stale_kyverno_policies()
    for path, policy_name in policies:
        run_oc(
            [
                "apply",
                "--server-side",
                "--force-conflicts",
                "--field-manager=odh-olminstall-kyverno",
                "-f",
                str(path),
            ],
            capture_output=True,
            check=True,
            timeout=120,
        )
        print(f"✓ ClusterPolicy {policy_name}")
    run_oc(
        ["wait", "clusterpolicy.kyverno.io", "--all", "--for=condition=Ready", "--timeout=120s"],
        capture_output=True,
        check=False,
        timeout=130,
    )
    run_oc(
        [
            "wait",
            "mutatingwebhookconfiguration/kyverno-resource-mutating-webhook-cfg",
            "--for=jsonpath={.webhooks[0].clientConfig.service.name}=kyverno-svc",
            "--timeout=60s",
        ],
        capture_output=True,
        check=False,
        timeout=70,
    )
    print("✓ Kyverno cluster policies applied")


def load_quay_dockerconfig() -> dict[str, Any]:
    candidates = [
        os.environ.get("ROSA_HCP_QUAY_AUTH_PATH", "").strip(),
        str(DEFAULT_VAULT_POLICY_DIR / ".dockerconfigjson"),
        os.environ.get("QUAY_PULL_SECRET_PATH", "/var/secret/quay/.dockerconfigjson").strip(),
    ]
    for path_s in candidates:
        if not path_s:
            continue
        path = Path(path_s)
        if path.is_file():
            return json.loads(path.read_text(encoding="utf-8"))
    raise RuntimeError(
        "quay dockerconfig not found (set ROSA_HCP_QUAY_AUTH_PATH or QUAY_PULL_SECRET_PATH)"
    )


def ensure_rosa_hcp_pull_setup(
    quay_dockerconfig: dict[str, Any] | None = None,
    *,
    target_namespaces: list[str] | None = None,
    post_sleep_s: int = 60,
) -> None:
    """Idempotent Jenkins ROSA_HCP ICSP substitute for HyperShift guest clusters."""
    if rosa_hcp_pull_setup_ready():
        print("✓ ROSA HCP Kyverno pull setup already ready")
        return
    if not is_hypershift_managed_cluster():
        print("⚠ Not a HyperShift-managed cluster; skipping ROSA HCP Kyverno setup")
        return
    if vault_policy_dir() is None:
        print(
            "⚠ ROSA HCP Kyverno tenant secret not mounted "
            f"({DEFAULT_VAULT_POLICY_DIR}); using in-repo policies (legacy)"
        )
    quay = quay_dockerconfig if quay_dockerconfig is not None else load_quay_dockerconfig()
    namespaces = list(target_namespaces or DEFAULT_TARGET_NAMESPACES)
    op_ns = os.environ.get("OPERATOR_NAMESPACE", "").strip()
    if op_ns and op_ns not in namespaces:
        namespaces.append(op_ns)
    print("HyperShift cluster: applying ROSA HCP Kyverno pull setup (Jenkins addICSP path)...")
    ensure_pull_secret_quay(quay, namespaces)
    install_kyverno()
    apply_kyverno_policies()
    if post_sleep_s > 0:
        print(f"Waiting {post_sleep_s}s for Kyverno secret sync...")
        time.sleep(post_sleep_s)
    if not rosa_hcp_pull_setup_ready():
        print("⚠ ROSA HCP Kyverno setup applied but readiness check incomplete; continuing")
    else:
        print("✓ ROSA HCP Kyverno pull setup ready")
    from install.rosa_hcp_imagestream_mirror import ensure_rosa_hcp_imagestream_mirror

    ensure_rosa_hcp_imagestream_mirror()


def main() -> int:
    extra = os.environ.get("ROSA_HCP_TARGET_NAMESPACES", "").strip()
    targets = [n for n in extra.split() if n] if extra else None
    try:
        ensure_rosa_hcp_pull_setup(target_namespaces=targets)
    except (RuntimeError, subprocess.CalledProcessError, FileNotFoundError) as exc:
        print(f"❌ ROSA HCP pull setup failed: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
