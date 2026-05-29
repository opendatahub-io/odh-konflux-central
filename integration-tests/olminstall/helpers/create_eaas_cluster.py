#!/usr/bin/env python3
"""Provision an ephemeral EaaS HyperShift cluster via ClusterTemplateInstance.

Env: INSTANCE_TYPE, VERSION, KUBECONFIG_VALUE, TENANT, TIMEOUT (default 30m),
     ICS_VALUE, CLUSTER_NAME_RESULT_PATH (Tekton step result file).
"""

from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile
import time
from pathlib import Path

_OLMINSTALL = Path(__file__).resolve().parent.parent
if str(_OLMINSTALL) not in sys.path:
    sys.path.insert(0, str(_OLMINSTALL))

from helpers.tekton_util import require_env, run, write_result

# Extra seconds beyond CTI `timeout` for API/controller lag before we declare poll timeout.
_POLL_BUFFER_S = 90


def _parse_timeout_to_seconds(raw: str, *, fallback: int = 1800) -> int:
    """Parse TIMEOUT-style values: plain seconds, or ``60s`` / ``30m`` / ``1h`` (case-insensitive)."""
    s = raw.strip()
    if not s:
        return fallback
    lower = s.lower()
    try:
        if lower.endswith("h"):
            return max(1, int(float(lower[:-1].strip()) * 3600))
        if lower.endswith("m"):
            return max(1, int(float(lower[:-1].strip()) * 60))
        if lower.endswith("s"):
            return max(1, int(float(lower[:-1].strip())))
        return max(1, int(float(s)))
    except (ValueError, OverflowError):
        return fallback


def _format_wait_hint(seconds: int) -> str:
    if seconds >= 3600 and seconds % 3600 == 0:
        return f"{seconds // 3600}h"
    if seconds >= 60 and seconds % 60 == 0:
        return f"{seconds // 60}m"
    return f"{seconds}s"


def _emit_cti_yaml(
    path: Path,
    *,
    tenant: str,
    instance_type: str,
    version: str,
    timeout: str,
    ics_value: str,
) -> None:
    """Write ClusterTemplateInstance YAML without ``yq`` (``konflux-test`` ships kislyuk ``yq``, not Go ``yq``)."""
    ics_body = ics_value.strip("\n")
    ics_lines = "\n".join("        " + line for line in ics_body.splitlines()) if ics_body else ""
    block = f"      value: |\n{ics_lines}\n" if ics_lines else '      value: ""\n'
    text = (
        "---\n"
        "apiVersion: clustertemplate.openshift.io/v1alpha1\n"
        "kind: ClusterTemplateInstance\n"
        "metadata:\n"
        "  generateName: cluster-\n"
        "  labels:\n"
        f"    eaas.konflux-ci.dev/tenant: {json.dumps(tenant)}\n"
        "spec:\n"
        "  clusterTemplateRef: hypershift-aws-cluster\n"
        "  parameters:\n"
        f"    - name: instanceType\n      value: {json.dumps(instance_type)}\n"
        f"    - name: version\n      value: {json.dumps(version)}\n"
        f"    - name: timeout\n      value: {json.dumps(timeout)}\n"
        "    - name: imageContentSources\n"
        f"{block}"
        "    - name: fips\n      value: \"false\"\n"
    )
    path.write_text(text, encoding="utf-8")


def _write_secure_kubeconfig(content: str) -> Path:
    """Write kubeconfig to a unique temp file with mode 0o600 (avoids /tmp/kubeconfig races)."""
    fd, path_str = tempfile.mkstemp(prefix="kubeconfig-", suffix=".yaml")
    try:
        os.write(fd, content.encode("utf-8"))
        os.fchmod(fd, 0o600)
    finally:
        os.close(fd)
    return Path(path_str)


def main() -> int:
    instance_type = require_env("INSTANCE_TYPE")
    version = require_env("VERSION")
    kubeconfig_value = require_env("KUBECONFIG_VALUE")
    tenant = require_env("TENANT")
    result_path = require_env("CLUSTER_NAME_RESULT_PATH")
    timeout = os.environ.get("TIMEOUT", "30m").strip() or "30m"
    ics_value = os.environ.get("ICS_VALUE", "").strip()

    kubeconfig = _write_secure_kubeconfig(kubeconfig_value)
    os.environ["KUBECONFIG"] = str(kubeconfig)

    # konflux-test image provides ``oc``; ``kubectl`` may be absent.
    kube = shutil.which("kubectl") or shutil.which("oc") or "oc"

    try:
        cti_path = Path("cti.yaml")
        _emit_cti_yaml(
            cti_path,
            tenant=tenant,
            instance_type=instance_type,
            version=version,
            timeout=timeout,
            ics_value=ics_value,
        )

        print(f"Creating ephemeral cluster (OCP {version}, {instance_type})...")
        print(cti_path.read_text(encoding="utf-8"))

        result = run([kube, "create", "-f", str(cti_path), "-o=jsonpath={.metadata.name}"], capture=True)
        cti_name = result.stdout.strip()
        if result.returncode != 0 or not cti_name:
            tail = ((result.stderr or "") + (result.stdout or "")).strip()[:4000]
            print(
                f"❌ ClusterTemplateInstance create failed or name missing (exit {result.returncode}): "
                f"{tail or '(no output)'}",
                file=sys.stderr,
            )
            return 1
        print(f"Created ClusterTemplateInstance {cti_name}")
        write_result(result_path, cti_name)

        wait_s = _parse_timeout_to_seconds(timeout)
        deadline = time.time() + wait_s + _POLL_BUFFER_S
        print(
            f"Waiting for cluster to be ready (CTI timeout≈{_format_wait_hint(wait_s)}, "
            f"poll deadline +{_POLL_BUFFER_S}s buffer for controller overhead)..."
        )
        while time.time() < deadline:
            phase_r = run(
                [kube, "get", "cti", cti_name, "-o", "jsonpath={.status.phase}"],
                check=False, capture=True,
            )
            phase = phase_r.stdout.strip() if phase_r.returncode == 0 else "Unknown"
            msg_r = run(
                [kube, "get", "cti", cti_name, "-o", "jsonpath={.status.message}"],
                check=False, capture=True,
            )
            msg = msg_r.stdout.strip() if msg_r.returncode == 0 else ""
            suffix = f"  ({msg})" if msg else ""
            print(f"  phase={phase}{suffix}")

            if phase == "Ready":
                print(f"Successfully provisioned {cti_name}")
                return 0
            if phase in ("Failed", "Error"):
                print("Cluster provisioning failed")
                run([kube, "get", "cti", cti_name, "-o", "yaml"], check=False)
                return 1
            time.sleep(30)

        print("Timed out waiting for cluster")
        run([kube, "get", "cti", cti_name, "-o", "yaml"], check=False)
        return 1
    finally:
        # Every ``run()`` above blocks until its subprocess exits; nothing keeps the file open.
        kubeconfig.unlink(missing_ok=True)


if __name__ == "__main__":
    raise SystemExit(main())
