"""Cluster registry mirror + pull-secret prep for RHOAI/ODH rhoai-e2e."""

from __future__ import annotations

import base64
import json
import os
from typing import Any

from k8s.oc_util import run_oc
from suite.errors import AppError

OLM_BUNDLE_UNPACK_UTILITY_IMAGE = (
    "quay.io/openshift-release-dev/ocp-v4.0-art-dev"
)
_OPENSHIFT_RELEASE_DEV_QUAY = "quay.io/openshift-release-dev"
_CLOUD_OPENSHIFT = "cloud.openshift.com"


def _auth_username(entry: dict[str, Any] | None) -> str:
    if not isinstance(entry, dict):
        return ""
    auth = entry.get("auth")
    if not auth:
        return ""
    try:
        raw = base64.standard_b64decode(str(auth)).decode("utf-8", errors="replace")
    except Exception:
        return ""
    return raw.split(":", 1)[0].strip()


def _is_rhoai_scoped_quay_user(username: str) -> bool:
    u = username.lower()
    return "rhoai" in u


def _is_openshift_release_dev_user(username: str) -> bool:
    return username.startswith("openshift-release-dev+")


def quay_auth_covers_openshift_release_dev(auths: dict[str, Any]) -> bool:
    """True when pull-secret can authorize OLM util image pulls from quay.io/openshift-release-dev."""
    for key in (
        f"{_OPENSHIFT_RELEASE_DEV_QUAY}/ocp-v4.0-art-dev",
        _OPENSHIFT_RELEASE_DEV_QUAY,
    ):
        if _is_openshift_release_dev_user(_auth_username(auths.get(key))):
            return True
        # Any non-empty auth under the scoped host is better than a RHOAI-only bare quay.io.
        if (auths.get(key) or {}).get("auth") and not _is_rhoai_scoped_quay_user(
            _auth_username(auths.get(key))
        ):
            return True
    bare = _auth_username(auths.get("quay.io"))
    return bool(bare) and not _is_rhoai_scoped_quay_user(bare)


def ensure_openshift_release_dev_pull_auth() -> bool:
    """Ensure pull-secret can pull OLM unpack util images from quay.io/openshift-release-dev.

    A RHOAI robot under bare ``quay.io`` does not authorize ``openshift-release-dev``
    repositories. Prefer a scoped ``quay.io/openshift-release-dev`` entry; when missing,
    copy credentials from ``cloud.openshift.com`` when that entry is an openshift-release-dev+
    robot (standard console.redhat.com pull-secret layout).
    """
    raw = run_oc(["get", "secret/pull-secret", "-n", "openshift-config", "-o", "json"]).stdout
    pull_data = json.loads(raw)
    b64 = pull_data["data"][".dockerconfigjson"]
    existing = json.loads(base64.standard_b64decode(b64))
    auths = dict(existing.get("auths") or {})
    if quay_auth_covers_openshift_release_dev(auths):
        print(
            f"✓ Global pull-secret can authorize OLM unpack util "
            f"({OLM_BUNDLE_UNPACK_UTILITY_IMAGE})",
            flush=True,
        )
        return False

    cloud = auths.get(_CLOUD_OPENSHIFT) if isinstance(auths.get(_CLOUD_OPENSHIFT), dict) else None
    cloud_user = _auth_username(cloud)
    if not cloud or not cloud.get("auth") or not _is_openshift_release_dev_user(cloud_user):
        bare_user = _auth_username(auths.get("quay.io"))
        print(
            f"WARN: pull-secret cannot authorize {OLM_BUNDLE_UNPACK_UTILITY_IMAGE} "
            f"(quay.io user={bare_user or 'missing'}; "
            f"cloud.openshift.com user={cloud_user or 'missing'})",
            flush=True,
        )
        return False

    print(
        f"Healing pull-secret: adding {_OPENSHIFT_RELEASE_DEV_QUAY} auth from "
        f"{_CLOUD_OPENSHIFT} ({cloud_user}) so OLM can pull unpack util images...",
        flush=True,
    )
    overlay = {
        "auths": {
            _OPENSHIFT_RELEASE_DEV_QUAY: {"auth": cloud["auth"]},
            f"{_OPENSHIFT_RELEASE_DEV_QUAY}/ocp-v4.0-art-dev": {"auth": cloud["auth"]},
        }
    }
    merged = merge_docker_auths(existing, overlay)
    merged_raw = json.dumps(merged, separators=(",", ":")).encode()
    patch_b64 = base64.standard_b64encode(merged_raw).decode("ascii")
    obj = dict(pull_data)
    obj.setdefault("data", {})[".dockerconfigjson"] = patch_b64
    _strip_secret_metadata(obj)
    run_oc(
        ["apply", "-f", "-"],
        stdin_text=json.dumps(obj),
        check=True,
        capture_output=True,
        timeout=120,
    )
    print(
        f"✓ Added {_OPENSHIFT_RELEASE_DEV_QUAY} auth for OLM bundle-unpack util pulls",
        flush=True,
    )
    return True


def preflight_openshift_release_dev_pull(*, strict: bool) -> None:
    """OLM bundle-unpack uses quay.io/openshift-release-dev via the cluster pull secret."""
    ensure_openshift_release_dev_pull_auth()
    auths = load_global_pull_secret_auths()
    if quay_auth_covers_openshift_release_dev(auths):
        print(f"✓ Global pull-secret has credential for {OLM_BUNDLE_UNPACK_UTILITY_IMAGE}")
        return
    bare_user = _auth_username(auths.get("quay.io"))
    msg = (
        "Cluster openshift-config/pull-secret cannot pull "
        f"{OLM_BUNDLE_UNPACK_UTILITY_IMAGE} "
        f"(bare quay.io user={bare_user or 'missing'}). "
        "A RHOAI-only robot under quay.io does not authorize openshift-release-dev. "
        "Merge a valid pull-secret from console.redhat.com (keep cloud.openshift.com / "
        "openshift-release-dev+ credentials; do not put RHOAI robot auth under bare quay.io)."
    )
    if strict:
        raise AppError(f"❌ {msg}", code=1)
    print(f"WARN: {msg}")


def extract_quay_auth(auths: dict[str, Any]) -> str | None:
    for key in ("quay.io/rhoai", "quay.io/rhoai/rhoai-fbc-fragment"):
        ent = auths.get(key) or {}
        auth = ent.get("auth")
        if auth:
            return str(auth)
    for k, v in auths.items():
        if k.startswith("quay.io/rhoai/") and isinstance(v, dict) and v.get("auth"):
            return str(v["auth"])
    # Legacy mounted secrets may still list bare quay.io — use only for token discovery,
    # never write it back under quay.io in the global pull secret.
    ent = auths.get("quay.io") or {}
    auth = ent.get("auth")
    if auth:
        return str(auth)
    return None


def rhoai_scoped_dockerconfig(quay: dict[str, Any]) -> dict[str, Any]:
    """Return dockerconfig with only quay.io/rhoai* auths (never bare quay.io)."""
    auths = quay.get("auths") or {}
    rhoai_entries = {
        k: v
        for k, v in auths.items()
        if k == "quay.io/rhoai" or k.startswith("quay.io/rhoai/")
    }
    quay_auth = extract_quay_auth(auths)
    if quay_auth:
        rhoai_entries.setdefault("quay.io/rhoai", {"auth": quay_auth})
    return {"auths": rhoai_entries}


def merge_docker_auths(existing: dict[str, Any], overlay: dict[str, Any]) -> dict[str, Any]:
    e_auth = dict(existing.get("auths") or {})
    o_auth = dict(overlay.get("auths") or {})
    out = dict(existing)
    out["auths"] = {**e_auth, **o_auth}
    return out


def dockerconfig_pull_secret_apply_manifest(name: str, namespace: str, dockerconfig_json: str) -> str:
    obj = {
        "apiVersion": "v1",
        "kind": "Secret",
        "metadata": {"name": name, "namespace": namespace},
        "type": "kubernetes.io/dockerconfigjson",
        "stringData": {".dockerconfigjson": dockerconfig_json},
    }
    return json.dumps(obj)


def _strip_secret_metadata(obj: dict[str, Any]) -> dict[str, Any]:
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


def load_global_pull_secret_auths() -> dict[str, Any]:
    raw = run_oc(["get", "secret/pull-secret", "-n", "openshift-config", "-o", "json"]).stdout
    pull_data = json.loads(raw)
    b64 = pull_data["data"][".dockerconfigjson"]
    existing = json.loads(base64.standard_b64decode(b64))
    return dict(existing.get("auths") or {})


def merge_rhoai_into_global_pull_secret(quay: dict[str, Any]) -> None:
    """Merge only quay.io/rhoai* keys into openshift-config/pull-secret."""
    print("Patching cluster global pull secret with quay.io/rhoai credentials...")
    raw = run_oc(["get", "secret/pull-secret", "-n", "openshift-config", "-o", "json"]).stdout
    pull_data = json.loads(raw)
    b64 = pull_data["data"][".dockerconfigjson"]
    existing = json.loads(base64.standard_b64decode(b64))
    overlay = rhoai_scoped_dockerconfig(quay)
    if not overlay.get("auths"):
        raise AppError("No quay.io/rhoai credentials to merge into global pull secret", code=1)
    merged = merge_docker_auths(existing, overlay)
    merged_raw = json.dumps(merged, separators=(",", ":")).encode()
    patch_b64 = base64.standard_b64encode(merged_raw).decode("ascii")
    obj = dict(pull_data)
    obj.setdefault("data", {})[".dockerconfigjson"] = patch_b64
    _strip_secret_metadata(obj)
    run_oc(
        ["apply", "-f", "-"],
        stdin_text=json.dumps(obj),
        check=True,
        capture_output=True,
        timeout=120,
    )
    print("✓ Global pull secret patched (quay.io/rhoai* only)")


def ensure_additional_pull_secret(quay: dict[str, Any]) -> None:
    auths = quay.get("auths") or {}
    quay_auth = extract_quay_auth(auths)
    if not quay_auth:
        raise AppError("No quay.io/rhoai auth for additional-pull-secret", code=1)
    print("Creating additional-pull-secret in kube-system (triggers HyperShift HCCO node sync)...")
    rhoai_entries = {k: v for k, v in auths.items() if k.startswith("quay.io/rhoai")}
    rhoai_auths = dict(rhoai_entries)
    rhoai_auths.setdefault("quay.io/rhoai", {"auth": quay_auth})
    creds_json = json.dumps({"auths": rhoai_auths}, separators=(",", ":"))
    run_oc(
        ["apply", "-f", "-"],
        stdin_text=dockerconfig_pull_secret_apply_manifest("additional-pull-secret", "kube-system", creds_json),
        check=True,
    )
    print("✓ additional-pull-secret created in kube-system")


def full_pull_setup_requested(product: str, quay_path: str) -> bool:
    from suite.constants import is_test_only_product

    if is_test_only_product(product):
        return False
    if not os.environ.get("QUAY_PULL_SECRET_NAME", "").strip():
        return False
    return bool(quay_path) and os.path.isfile(quay_path)


def ensure_cluster_registry_for_rhoai(
    quay: dict[str, Any] | None,
    *,
    product: str,
    quay_path: str = "",
) -> None:
    """Idempotent registry prep: IDMS/Kyverno, safe pull-secret merge, unpack preflight."""
    from install.rosa_hcp_imagestream_mirror import ensure_rosa_hcp_imagestream_mirror
    from install.rosa_hcp_pull_setup import ensure_rosa_hcp_pull_setup, is_hypershift_managed_cluster

    product_l = product.strip().lower()
    strict_preflight = product_l in ("rhoai", "odh")
    do_full_pull = quay is not None and full_pull_setup_requested(product_l, quay_path)

    if is_hypershift_managed_cluster():
        if do_full_pull and quay is not None:
            ensure_rosa_hcp_pull_setup(quay)
        else:
            ensure_rosa_hcp_imagestream_mirror()

    if do_full_pull and quay is not None:
        merge_rhoai_into_global_pull_secret(quay)
        ensure_additional_pull_secret(quay)

    from install.install_and_verify import ensure_rhoai_registry_access

    ensure_rhoai_registry_access()
    preflight_openshift_release_dev_pull(strict=strict_preflight)
