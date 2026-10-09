#!/usr/bin/env python3
"""Rewrite workbench ImageStream imports on HyperShift where IDMS is admission-blocked.

Kyverno rewrites Pod image refs (registry.redhat.io/rhoai → quay.io/rhoai) but the
OpenShift ImageStream importer pulls ``spec.tags[].from`` literally. On ROSA HCP the
cluster ImageDigestMirrorSet is empty and cannot be patched, so workbench tags stay
ImportSuccess=False and workbenches smoke hangs until timeout.
"""

from __future__ import annotations

import json
import sys
import time
from typing import Any

from k8s.oc_util import run_oc
from install.rosa_hcp_pull_setup import is_hypershift_managed_cluster

RHOAI_REGISTRY_PREFIX = "registry.redhat.io/rhoai/"
RHOAI_MIRROR_PREFIX = "quay.io/rhoai/"
DEFAULT_IMAGESTREAM_NAMESPACES = ("redhat-ods-applications",)
_IMPORT_POLL_INTERVAL_S = 15
_IMPORT_WAIT_TIMEOUT_S = 600


def mirror_rhoai_image_ref(image_ref: str) -> str | None:
    if not image_ref.startswith(RHOAI_REGISTRY_PREFIX):
        return None
    return RHOAI_MIRROR_PREFIX + image_ref[len(RHOAI_REGISTRY_PREFIX) :]


def _list_imagestreams(namespace: str) -> list[dict[str, Any]]:
    proc = run_oc(
        ["get", "imagestream", "-n", namespace, "-o", "json"],
        capture_output=True,
        check=False,
        timeout=120,
    )
    if proc.returncode != 0:
        return []
    try:
        return list(json.loads(proc.stdout or "{}").get("items") or [])
    except json.JSONDecodeError:
        return []


def _patch_tag_from(namespace: str, name: str, tag_name: str, mirrored: str) -> bool:
    patch = {
        "spec": {
            "tags": [
                {
                    "name": tag_name,
                    "from": {"kind": "DockerImage", "name": mirrored},
                }
            ]
        }
    }
    proc = run_oc(
        [
            "patch",
            "imagestream",
            name,
            "-n",
            namespace,
            "-p",
            json.dumps(patch),
        ],
        capture_output=True,
        check=False,
        timeout=60,
    )
    if proc.returncode != 0:
        err = (proc.stderr or proc.stdout or "").strip()
        print(f"⚠ Could not patch ImageStream/{name}:{tag_name} in {namespace}: {err}", file=sys.stderr)
        return False
    print(f"✓ ImageStream/{name}:{tag_name} → {mirrored}")
    return True


def _tag_import_failed(namespace: str, name: str, tag_name: str) -> bool:
    proc = run_oc(
        [
            "get",
            "imagestream",
            name,
            "-n",
            namespace,
            "-o",
            "jsonpath={.status.tags[?(@.tag=='"
            + tag_name
            + "')].conditions[?(@.type=='ImportSuccess')].status}",
        ],
        capture_output=True,
        check=False,
        timeout=30,
    )
    if proc.returncode != 0:
        return True
    status = (proc.stdout or "").strip()
    if not status:
        return True
    return status != "True"


def _tag_needs_mirror(namespace: str, name: str, tag_name: str, spec_from: str) -> bool:
    if not mirror_rhoai_image_ref(spec_from):
        return False
    if spec_from.startswith(RHOAI_MIRROR_PREFIX):
        return False
    if _tag_import_failed(namespace, name, tag_name):
        return True
    return spec_from.startswith(RHOAI_REGISTRY_PREFIX)


def mirror_rhoai_imagestreams_in_namespace(namespace: str) -> int:
    """Patch failed rhoai registry ImageStream tags to quay.io/rhoai; return patch count."""
    patched = 0
    for item in _list_imagestreams(namespace):
        md = item.get("metadata") or {}
        is_name = md.get("name") or ""
        if not is_name:
            continue
        for tag in item.get("spec", {}).get("tags") or []:
            tag_name = tag.get("name") or ""
            from_obj = tag.get("from") or {}
            if from_obj.get("kind") != "DockerImage":
                continue
            src = (from_obj.get("name") or "").strip()
            mirrored = mirror_rhoai_image_ref(src)
            if not mirrored or mirrored == src:
                continue
            if not _tag_needs_mirror(namespace, is_name, tag_name, src):
                continue
            if _patch_tag_from(namespace, is_name, tag_name, mirrored):
                patched += 1
    return patched


def wait_for_rhoai_imagestream_imports(
    namespaces: tuple[str, ...] | list[str] | None = None,
    *,
    timeout_s: int = _IMPORT_WAIT_TIMEOUT_S,
    poll_s: int = _IMPORT_POLL_INTERVAL_S,
) -> bool:
    """Poll until no ImportSuccess=False tags remain on mirrored ImageStreams."""
    targets = list(namespaces or DEFAULT_IMAGESTREAM_NAMESPACES)
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        pending: list[str] = []
        for ns in targets:
            for item in _list_imagestreams(ns):
                is_name = (item.get("metadata") or {}).get("name") or ""
                for tag in item.get("status", {}).get("tags") or []:
                    tag_name = tag.get("tag") or ""
                    for cond in tag.get("conditions") or []:
                        if cond.get("type") != "ImportSuccess":
                            continue
                        if cond.get("status") == "True":
                            break
                        from_ref = ""
                        for spec_tag in item.get("spec", {}).get("tags") or []:
                            if spec_tag.get("name") == tag_name:
                                from_ref = ((spec_tag.get("from") or {}).get("name") or "")
                                break
                        if from_ref.startswith(RHOAI_MIRROR_PREFIX):
                            pending.append(f"{ns}/{is_name}:{tag_name}")
                        break
        if not pending:
            print("✓ ROSA HCP workbench ImageStream imports ready")
            return True
        print(
            f"Waiting for ImageStream imports ({len(pending)} pending): "
            f"{', '.join(pending[:5])}{'…' if len(pending) > 5 else ''}"
        )
        time.sleep(poll_s)
    print(
        f"⚠ ROSA HCP ImageStream imports still pending after {timeout_s}s",
        file=sys.stderr,
    )
    return False


def ensure_rosa_hcp_imagestream_mirror(
    namespaces: tuple[str, ...] | list[str] | None = None,
    *,
    wait_imports: bool = True,
) -> None:
    """Idempotent HyperShift workbench ImageStream mirror (Jenkins ICSP gap for IS imports)."""
    if not is_hypershift_managed_cluster():
        print("Not HyperShift-managed; skipping ROSA HCP ImageStream mirror")
        return
    targets = list(namespaces or DEFAULT_IMAGESTREAM_NAMESPACES)
    total = 0
    for ns in targets:
        ns_proc = run_oc(["get", "ns", ns], capture_output=True, check=False, timeout=30)
        if ns_proc.returncode != 0:
            print(f"⚠ Namespace {ns} missing; skipping ImageStream mirror")
            continue
        total += mirror_rhoai_imagestreams_in_namespace(ns)
    if total:
        print(f"Patched {total} ImageStream tag(s) to {RHOAI_MIRROR_PREFIX}")
    else:
        print("No failed registry.redhat.io/rhoai ImageStream tags to patch")
    if wait_imports and total:
        wait_for_rhoai_imagestream_imports(targets)


def main() -> int:
    try:
        ensure_rosa_hcp_imagestream_mirror()
    except Exception as exc:
        print(f"❌ ROSA HCP ImageStream mirror failed: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
