#!/usr/bin/env python3
"""Resolve target-cluster kubeconfig for collect-diagnostics (EPHC or external).

Writes a Tekton result ``KUBECONFIG_PATH`` (absolute path) for the collect step.

Env:
    CLUSTER_SOURCE  -- tenant Secret name with key ``kubeconfig`` (external cluster)
    TESTS_SHARED_KUBECONFIG       -- kubeconfig staged by opendatahub-tests-prepare (preferred)
    EPHC_KUBECONFIG_REL         -- filename under ``/credentials`` from get-kubeconfig (EPHC)
    KUBECONFIG_PATH_RESULT      -- Tekton result file path (required)
    NAMESPACE                   -- optional; defaults to in-cluster service account namespace
"""

from __future__ import annotations

import base64
import os
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

from _bootstrap import ensure_rhoai_e2e_path

ensure_rhoai_e2e_path()

from runners.report.pipelinerun_summary import _k8s_request, kubernetes_api_base_url  # noqa: E402
from steps.external_kubeconfig_mount import copy_external_kubeconfig_mount  # noqa: E402
from steps.tekton_util import require_env, write_result  # noqa: E402
from suite.conforma_gate import CONFORMA_GATE_SKIP  # noqa: E402
from suite.its_trigger_params import external_kubeconfig_secret_name  # noqa: E402

_CREDENTIALS = Path("/credentials")
_KUBECONFIG = _CREDENTIALS / "kubeconfig"


def _namespace() -> str:
    ns = os.environ.get("NAMESPACE", "").strip()
    if ns:
        return ns
    ns_path = Path("/var/run/secrets/kubernetes.io/serviceaccount/namespace")
    if ns_path.is_file():
        return ns_path.read_text(encoding="utf-8").strip()
    return ""


def _fetch_external_kubeconfig(secret_name: str, namespace: str) -> str:
    token_path = Path("/var/run/secrets/kubernetes.io/serviceaccount/token")
    ca_path = Path("/var/run/secrets/kubernetes.io/serviceaccount/ca.crt")
    base = kubernetes_api_base_url()
    if not (secret_name and namespace and token_path.is_file() and ca_path.is_file() and base):
        raise RuntimeError("cannot read external kubeconfig secret (missing SA or namespace)")
    token = token_path.read_text(encoding="utf-8")
    url = (
        f"{base}/api/v1/namespaces/{urllib.parse.quote(namespace)}"
        f"/secrets/{urllib.parse.quote(secret_name)}"
    )
    doc = _k8s_request("GET", url, token, ca_path)
    data = (doc.get("data") or {}).get("kubeconfig", "")
    if not data:
        raise RuntimeError(f"secret/{secret_name} has no kubeconfig key in namespace {namespace}")
    return base64.b64decode(data).decode("utf-8")


def main() -> int:
    result_path = require_env("KUBECONFIG_PATH_RESULT")
    if (os.environ.get("CONFORMA_GATE") or "").strip().lower() == CONFORMA_GATE_SKIP:
        print("CONFORMA_GATE=skip - no target cluster; diagnostics kubeconfig skipped")
        write_result(result_path, "")
        return 0

    external = external_kubeconfig_secret_name(os.environ.get("CLUSTER_SOURCE", ""))
    ephc_rel = (os.environ.get("EPHC_KUBECONFIG_REL") or "").strip()
    tests_shared_kubeconfig = os.environ.get("TESTS_SHARED_KUBECONFIG", "").strip()

    _CREDENTIALS.mkdir(parents=True, exist_ok=True)

    if tests_shared_kubeconfig:
        src = Path(tests_shared_kubeconfig)
        if src.is_file():
            _KUBECONFIG.write_bytes(src.read_bytes())
            _KUBECONFIG.chmod(0o600)
            print(f"Staged kubeconfig from tests-shared at {_KUBECONFIG}")
            write_result(result_path, str(_KUBECONFIG))
            return 0

    if external:
        if copy_external_kubeconfig_mount(_KUBECONFIG):
            write_result(result_path, str(_KUBECONFIG))
            return 0
        ns = _namespace()
        if not ns:
            print("ERROR: namespace required to read external kubeconfig secret", file=sys.stderr)
            return 1
        try:
            content = _fetch_external_kubeconfig(external, ns)
        except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, OSError, ValueError, RuntimeError) as exc:
            print(f"ERROR: could not load external kubeconfig from secret/{external}: {exc}", file=sys.stderr)
            return 1
        _KUBECONFIG.write_text(content, encoding="utf-8")
        _KUBECONFIG.chmod(0o600)
        print(f"External kubeconfig staged at {_KUBECONFIG} (secret/{external})")
    elif ephc_rel:
        src = _CREDENTIALS / ephc_rel
        if not src.is_file():
            print(f"ERROR: EPHC kubeconfig missing at {src}", file=sys.stderr)
            return 1
        if src.resolve() != _KUBECONFIG.resolve():
            _KUBECONFIG.write_bytes(src.read_bytes())
            _KUBECONFIG.chmod(0o600)
        print(f"EPHC kubeconfig staged at {_KUBECONFIG} (from {ephc_rel})")
    elif _KUBECONFIG.is_file():
        print(f"Using existing kubeconfig at {_KUBECONFIG}")
    else:
        print("ERROR: no target cluster kubeconfig for diagnostics", file=sys.stderr)
        write_result(result_path, "")
        return 1

    write_result(result_path, str(_KUBECONFIG))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
