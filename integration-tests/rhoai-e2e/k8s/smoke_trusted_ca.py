"""Stage ingress/router CA trust for smoke S3 (MinIO routes on external OpenShift clusters)."""

from __future__ import annotations

import base64
import json
import shutil
from pathlib import Path

from suite.errors import AppError
from .oc_util import run_cmd


def _decode_router_ca_secret_data(data: dict[str, str]) -> str:
    raw = data.get("tls.crt") or data.get("ca.crt")
    if not raw:
        return ""
    return base64.b64decode(raw).decode("utf-8", errors="replace").strip()


def _fetch_router_ca_via_kubernetes(kubeconfig: Path) -> str:
    try:
        from kubernetes import client, config
    except ImportError:
        return ""
    try:
        config.load_kube_config(config_file=str(kubeconfig))
        v1 = client.CoreV1Api()
        sec = v1.read_namespaced_secret("router-ca", "openshift-ingress-operator")
        data = sec.data if sec.data else {}
        return _decode_router_ca_secret_data(data)
    except Exception:
        return ""


def fetch_ingress_router_ca_pem(kubeconfig: Path) -> str:
    """Return PEM for the default ingress/router serving certificate (if present)."""
    if shutil.which("oc"):
        get = run_cmd(
            [
                "oc",
                "--kubeconfig",
                str(kubeconfig),
                "get",
                "secret",
                "router-ca",
                "-n",
                "openshift-ingress-operator",
                "-o",
                "json",
            ],
            capture=True,
            check=False,
        )
        if get.returncode == 0:
            try:
                doc = json.loads(get.stdout or "{}")
            except json.JSONDecodeError:
                doc = {}
            data = doc.get("data") if isinstance(doc, dict) else None
            if isinstance(data, dict):
                pem = _decode_router_ca_secret_data(data)
                if pem:
                    return pem
    return _fetch_router_ca_via_kubernetes(kubeconfig)


# Amazon Root CA 1 (for s3.*.amazonaws.com in ods-ci smoke tests).
_AMAZON_ROOT_CA_1_PEM = """-----BEGIN CERTIFICATE-----
MIIDQTCCAimgAwIBAgITBmyfz5m/jAo54vB4ikPmljZbyjANBgkqhkiG9w0BAQsF
ADA5MQswCQYDVQQGEwJVUzEPMA0GA1UEChMGQW1hem9uMRkwFwYDVQQDExBBbWF6
b24gUm9vdCBDQSAxMB4XDTE1MDUyNjAwMDAwMFoXDTM4MDExNzAwMDAwMFowOTEL
MAkGA1UEBhMCVVMxDzANBgNVBAoTBkFtYXpvbjEZMBcGA1UEAxMQQW1hem9uIFJv
b3QgQ0EgMTCCASIwDQYJKoZIhvcNAQEBBQADggEPADCCAQoCggEBALJ4gHHKeNXj
ca9HgFB0fW7Y14h29Jlo91ghYPl0hAEvrAIthtOgQ3pOsqTQNroBvo3bSMgHFzZM
9O6II8c+6zf1tRn4SWiw3te5djgdYZ6k/oI2peVKVuRF4fn9tBb6dNqcmzU5L/qw
IFAGbHrQgLKm+a/sRxmPUDgH3KKHOVj4utWp+UhnMJbulHheb4mjUcAwhmahRWa6
VOujw5H5SNz/0egwLX0tdHA114gk957EWW67c4cX8jJGKLhD+rcdqsq08p8kDi1L
93FcXmn/6pUCyziKrlA4b9v7LWIbxcceVOF34GfID5yHI9Y/QCB/IIDEgEw+OyQm
jgSubJrIqg0CAwEAAaNCMEAwDwYDVR0TAQH/BAUwAwEB/zAOBgNVHQ8BAf8EBAMC
AYYwHQYDVR0OBBYEFIQYzIU07LwMlJQuCFmcx7IQTgoIMA0GCSqGSIb3DQEBCwUA
A4IBAQCY8jdaQZChGsV2USggNiMOruYou6r4lK5IpDB/G/wkjUu0yKGX9rbxenDI
U5PMCCjjmCXPI6T53iHTfIUJrU6adTrCC2qJeHZERxhlbI1Bjjt/msv0tadQ1wUs
N+gDS63pYaACbvXy8MWy7Vu33PqUXHeeE6V/Uq2V8viTO96LXFvKWlJbYK8U90vv
o/ufQJVtMVT8QtPHRh8jrdkPSHCa2XV4cdFyQzR1bldZwgJcJmApzyMZFo6IQ6XU
5MsI+yMRQ+hDKXJioaldXgjUkK642M4UwtBV8ob2xJNDd2ZhwLnoQdeXeGADbkpy
rqXRfboQnoZsG4q5WTP468SQvvG5
-----END CERTIFICATE-----"""


def _combined_smoke_ca_pem(router_pem: str) -> str:
    """Router CA for cluster routes plus Amazon Root CA 1 for AWS S3 smoke buckets."""
    parts = [router_pem.strip()] if router_pem.strip() else []
    parts.append(_AMAZON_ROOT_CA_1_PEM.strip())
    return "\n".join(parts) + "\n"


def ensure_trusted_ca_for_smoke_s3(*, target_kubeconfig: Path) -> bool:
    """Patch DSCI trustedCABundle so KServe/storage-initializer trusts MinIO routes and AWS S3."""
    router_pem = fetch_ingress_router_ca_pem(target_kubeconfig)
    if not router_pem.strip():
        print(
            "WARN: could not read openshift-ingress-operator/router-ca; "
            "skipping DSCI trustedCABundle patch for smoke S3",
            flush=True,
        )
        return False
    pem = _combined_smoke_ca_pem(router_pem)

    exists = run_cmd(
        [
            "oc",
            "--kubeconfig",
            str(target_kubeconfig),
            "get",
            "dscinitialization",
            "default-dsci",
        ],
        capture=True,
        check=False,
    )
    if exists.returncode != 0:
        print(
            "WARN: DSCInitialization/default-dsci not found; skipping trustedCABundle patch",
            flush=True,
        )
        return False

    patch_doc = {
        "spec": {
            "trustedCABundle": {
                "managementState": "Managed",
                "customCABundle": pem + "\n",
            }
        }
    }
    patch = run_cmd(
        [
            "oc",
            "--kubeconfig",
            str(target_kubeconfig),
            "patch",
            "dscinitialization",
            "default-dsci",
            "--type=merge",
            "-p",
            json.dumps(patch_doc),
        ],
        capture=True,
        check=False,
    )
    if patch.returncode != 0:
        err = (patch.stderr or patch.stdout or "").strip()
        raise AppError(f"Failed to patch DSCI trustedCABundle for smoke S3: {err}", 1)
    print("Patched DSCInitialization/default-dsci trustedCABundle for smoke S3 (router + public CAs)")
    return True
