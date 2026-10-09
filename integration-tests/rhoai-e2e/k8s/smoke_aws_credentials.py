"""Patch tenant shift-left smoke Secrets for external-cluster TLS trust."""

from __future__ import annotations

import base64
import json
from pathlib import Path

from suite.errors import AppError
from .oc_util import run_cmd
from .smoke_trusted_ca import _combined_smoke_ca_pem, ensure_trusted_ca_for_smoke_s3, fetch_ingress_router_ca_pem

MLFLOW_ENVFILE_SECRET = "envfile-mlflow"

def _decode_secret_data(doc: dict[str, object]) -> dict[str, str]:
    raw = doc.get("data")
    if not isinstance(raw, dict):
        return {}
    out: dict[str, str] = {}
    for key, val in raw.items():
        if not isinstance(key, str) or not isinstance(val, str):
            continue
        out[key] = base64.b64decode(val).decode("utf-8", errors="replace").strip()
    return out


def _secret_value_empty(data: dict[str, str], key: str) -> bool:
    return not (data.get(key) or "").strip()


def _encode_secret_patch(values: dict[str, str]) -> dict[str, str]:
    return {
        key: base64.b64encode(value.encode("utf-8")).decode("ascii")
        for key, value in values.items()
        if value.strip()
    }


def backfill_shift_left_smoke_secret_from_mlflow(
    *,
    tenant_namespace: str,
    secret_name: str,
    mlflow_secret_name: str = MLFLOW_ENVFILE_SECRET,
) -> bool:
    """Copy model-serving S3 creds from envfile-mlflow when shift-left keys are empty placeholders."""
    name = (secret_name or "").strip()
    mlflow_name = (mlflow_secret_name or "").strip()
    if not name or not mlflow_name:
        return False

    shift_get = run_cmd(
        ["oc", "get", "secret", name, "-n", tenant_namespace, "-o", "json"],
        capture=True,
        check=False,
    )
    if shift_get.returncode != 0:
        raise AppError(f"Smoke secret {tenant_namespace}/{name} not found", 1)
    shift_data = _decode_secret_data(json.loads(shift_get.stdout or "{}"))
    if not _secret_value_empty(shift_data, "AWS_ACCESS_KEY_ID"):
        return False

    mlflow_get = run_cmd(
        ["oc", "get", "secret", mlflow_name, "-n", tenant_namespace, "-o", "json"],
        capture=True,
        check=False,
    )
    if mlflow_get.returncode != 0:
        print(
            f"WARN: cannot backfill {name!r} from {mlflow_name!r}: secret not found",
            flush=True,
        )
        return False
    mlflow_data = _decode_secret_data(json.loads(mlflow_get.stdout or "{}"))

    patch_values: dict[str, str] = {}
    for key in ("AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY"):
        if _secret_value_empty(shift_data, key):
            val = (mlflow_data.get(key) or "").strip()
            if val:
                patch_values[key] = val

    region = (mlflow_data.get("AWS_DEFAULT_REGION") or "").strip()
    bucket = (mlflow_data.get("BUCKET") or "").strip()
    endpoint = (mlflow_data.get("ENDPOINT") or "").strip()
    if region:
        for target in ("CI_S3_BUCKET_REGION", "MODELS_S3_BUCKET_REGION"):
            if _secret_value_empty(shift_data, target):
                patch_values[target] = region
    if bucket:
        for target in ("CI_S3_BUCKET_NAME", "MODELS_S3_BUCKET_NAME"):
            if _secret_value_empty(shift_data, target):
                patch_values[target] = bucket
    if endpoint:
        for target in ("CI_S3_BUCKET_ENDPOINT", "MODELS_S3_BUCKET_ENDPOINT"):
            if _secret_value_empty(shift_data, target):
                patch_values[target] = endpoint

    if not patch_values:
        return False

    proc = run_cmd(
        [
            "oc",
            "patch",
            "secret",
            name,
            "-n",
            tenant_namespace,
            "--type=merge",
            "-p",
            json.dumps({"data": _encode_secret_patch(patch_values)}),
        ],
        capture=True,
        check=False,
    )
    if proc.returncode != 0:
        err = (proc.stderr or proc.stdout or "").strip()
        raise AppError(f"Failed to backfill {name!r} from {mlflow_name!r}: {err}", 1)
    print(
        f"Patched smoke secret {name}: backfilled model-serving S3 creds from {mlflow_name} "
        f"({', '.join(sorted(patch_values))})",
        flush=True,
    )
    return True


def ensure_router_ca_in_smoke_secret(
    *,
    tenant_namespace: str,
    secret_name: str,
    target_kubeconfig: Path,
) -> bool:
    """Ensure tenant smoke Secret includes AWS_CA_BUNDLE (ingress router PEM) for TLS trust."""
    name = (secret_name or "").strip()
    if not name:
        return False
    router_pem = fetch_ingress_router_ca_pem(target_kubeconfig)
    pem = _combined_smoke_ca_pem(router_pem)
    if not pem.strip():
        print(
            f"WARN: could not read router-ca from external cluster; "
            f"{name!r} left unchanged (port-forward TLS may fail)",
            flush=True,
        )
        return False

    get = run_cmd(
        ["oc", "get", "secret", name, "-n", tenant_namespace, "-o", "json"],
        capture=True,
        check=False,
    )
    if get.returncode != 0:
        raise AppError(f"Smoke secret {tenant_namespace}/{name} not found", 1)
    doc = json.loads(get.stdout or "{}")
    data = doc.get("data") if isinstance(doc, dict) else None
    if not isinstance(data, dict):
        data = {}

    existing_raw = data.get("AWS_CA_BUNDLE")
    if existing_raw:
        existing = base64.b64decode(existing_raw).decode("utf-8", errors="replace").strip()
        if existing == pem.strip():
            print(f"Smoke secret {name}: AWS_CA_BUNDLE already matches smoke CA bundle")
            return True

    proc = run_cmd(
        [
            "oc",
            "patch",
            "secret",
            name,
            "-n",
            tenant_namespace,
            "--type=merge",
            "-p",
            json.dumps(
                {
                    "data": {
                        "AWS_CA_BUNDLE": base64.b64encode(pem.encode("utf-8")).decode("ascii"),
                    }
                }
            ),
        ],
        capture=True,
        check=False,
    )
    if proc.returncode != 0:
        err = (proc.stderr or proc.stdout or "").strip()
        raise AppError(f"Failed to update AWS_CA_BUNDLE in {name!r}: {err}", 1)
    print(f"Patched smoke secret {name}: AWS_CA_BUNDLE (router + public CAs for smoke S3)")
    try:
        ensure_trusted_ca_for_smoke_s3(target_kubeconfig=target_kubeconfig)
    except AppError as exc:
        print(f"WARN: {exc}", flush=True)
    return True
