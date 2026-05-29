#!/usr/bin/env python3
"""
Merge quay.io/rhoai credentials into the EaaS cluster's global pull secret,
pre-create an imagePullSecret in openshift-marketplace for OLM pods, and
register an additional-pull-secret in kube-system for HyperShift node sync.

Internal Tekton pipeline step — not meant to be called directly.
From a laptop, trigger tests via: python3 …/olm_pipeline.py

In Tekton the quay secret is volume-mounted at /var/secret/quay/.dockerconfigjson.
"""

from __future__ import annotations

import base64
import json
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

QUAY_SECRET_PATH = Path("/var/secret/quay/.dockerconfigjson")
OC_PATH = shutil.which("oc")


def run_oc(
    args: list[str],
    *,
    check: bool = True,
    input_text: str | None = None,
    capture_output: bool = True,
    timeout: int = 60,
) -> subprocess.CompletedProcess[str]:
    if not OC_PATH:
        raise RuntimeError("'oc' binary not found in PATH")
    return subprocess.run(
        [OC_PATH, *args],
        check=check,
        input=input_text,
        capture_output=capture_output,
        text=True,
        timeout=timeout,
    )


def extract_quay_auth(auths: dict[str, Any]) -> str | None:
    for key in ("quay.io", "quay.io/rhoai", "quay.io/rhoai/rhoai-fbc-fragment"):
        ent = auths.get(key) or {}
        auth = ent.get("auth")
        if auth:
            return str(auth)
    for k, v in auths.items():
        if k.startswith("quay.io/rhoai/") and isinstance(v, dict) and v.get("auth"):
            return str(v["auth"])
    return None


def merge_docker_auths(existing: dict[str, Any], overlay: dict[str, Any]) -> dict[str, Any]:
    e_auth = dict(existing.get("auths") or {})
    o_auth = dict(overlay.get("auths") or {})
    out = dict(existing)
    out["auths"] = {**e_auth, **o_auth}
    return out


def dockerconfig_pull_secret_apply_manifest(name: str, namespace: str, dockerconfig_json: str) -> str:
    """JSON manifest using stringData so credentials are not passed via CLI argv."""
    obj = {
        "apiVersion": "v1",
        "kind": "Secret",
        "metadata": {"name": name, "namespace": namespace},
        "type": "kubernetes.io/dockerconfigjson",
        "stringData": {".dockerconfigjson": dockerconfig_json},
    }
    return json.dumps(obj)


def main() -> int:
    if not QUAY_SECRET_PATH.is_file():
        print(f"❌ Quay secret not mounted at {QUAY_SECRET_PATH}")
        return 1

    quay = json.loads(QUAY_SECRET_PATH.read_text(encoding="utf-8"))
    auths = quay.get("auths") or {}
    quay_auth = extract_quay_auth(auths)
    if not quay_auth:
        print(f"❌ No quay.io/rhoai auth token found in {QUAY_SECRET_PATH}")
        return 1

    quay = merge_docker_auths(quay, {"auths": {"quay.io": {"auth": quay_auth}}})

    print("Patching cluster global pull secret with quay.io/rhoai credentials...")
    raw = run_oc(["get", "secret/pull-secret", "-n", "openshift-config", "-o", "json"]).stdout
    pull_data = json.loads(raw)
    b64 = pull_data["data"][".dockerconfigjson"]
    existing = json.loads(base64.standard_b64decode(b64))
    merged = merge_docker_auths(existing, quay)
    merged_raw = json.dumps(merged, separators=(",", ":")).encode()
    patch_b64 = base64.standard_b64encode(merged_raw).decode("ascii")
    obj = dict(pull_data)
    obj.setdefault("data", {})[".dockerconfigjson"] = patch_b64
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
    run_oc(
        ["apply", "-f", "-"],
        input_text=json.dumps(obj),
        check=True,
        capture_output=True,
        timeout=120,
    )
    print("✓ Global pull secret patched")

    print("Creating additional-pull-secret in kube-system (triggers HyperShift HCCO node sync)...")
    rhoai_entries = {k: v for k, v in (quay.get("auths") or {}).items() if k.startswith("quay.io/rhoai")}
    rhoai_auths = dict(rhoai_entries)
    rhoai_auths.setdefault("quay.io/rhoai", {"auth": quay_auth})
    rhoai_creds = {"auths": rhoai_auths}
    creds_json = json.dumps(rhoai_creds, separators=(",", ":"))
    run_oc(
        ["apply", "-f", "-"],
        input_text=dockerconfig_pull_secret_apply_manifest("additional-pull-secret", "kube-system", creds_json),
        check=True,
    )
    print("✓ additional-pull-secret created in kube-system")

    print("Creating rhoai-quay-pull imagePullSecret in openshift-marketplace for OLM SA-level pulls...")
    quay_json = json.dumps(quay, separators=(",", ":"))
    run_oc(
        ["apply", "-f", "-"],
        input_text=dockerconfig_pull_secret_apply_manifest("rhoai-quay-pull", "openshift-marketplace", quay_json),
        check=True,
    )

    ls = run_oc(["get", "sa", "-n", "openshift-marketplace", "--no-headers", "-o", "custom-columns=:metadata.name"], check=False)
    if ls.returncode == 0:
        failures: list[str] = []
        successes = 0
        for line in ls.stdout.splitlines():
            name = line.strip()
            if name:
                cp = run_oc(
                    ["secrets", "link", name, "rhoai-quay-pull", "-n", "openshift-marketplace", "--for=pull"],
                    check=False,
                )
                if cp.returncode != 0:
                    failures.append(f"{name}: {(cp.stderr or cp.stdout or '').strip()}")
                else:
                    successes += 1
        if failures:
            print("❌ Failed linking rhoai-quay-pull to some SAs:", file=sys.stderr)
            for item in failures:
                print(f"  {item}", file=sys.stderr)
            return 1
        if successes > 0:
            print("✓ rhoai-quay-pull linked to all SAs in openshift-marketplace")
        else:
            print("⚠ No service accounts found in openshift-marketplace")
    else:
        print("⚠ Failed to list service accounts in openshift-marketplace")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except RuntimeError as exc:
        print(f"ERROR {exc}", file=sys.stderr)
        raise SystemExit(1) from exc
    except subprocess.TimeoutExpired as exc:
        print(f"ERROR oc command timed out: {exc}", file=sys.stderr)
        raise SystemExit(124) from exc
    except subprocess.CalledProcessError as exc:
        print(exc.stderr or exc.stdout or str(exc), file=sys.stderr)
        raise SystemExit(exc.returncode or 1) from exc
