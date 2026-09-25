"""User Workload Monitoring prerequisites for MaaS."""

from __future__ import annotations

import yaml

from install.dsc_install import oc_run

from components.maas_billing.common import _MONITORING_CM, _MONITORING_NS


def _monitoring_config_data() -> dict:
    config: dict = {"enableUserWorkload": True}
    r = oc_run(
        [
            "get",
            "configmap",
            _MONITORING_CM,
            "-n",
            _MONITORING_NS,
            "-o",
            "jsonpath={.data.config\\.yaml}",
        ],
        check=False,
        capture_output=True,
        timeout=30,
    )
    if r.returncode == 0 and (r.stdout or "").strip():
        try:
            existing = yaml.safe_load(r.stdout)
            if isinstance(existing, dict):
                existing["enableUserWorkload"] = True
                config = existing
        except yaml.YAMLError:
            pass
    return config


def _user_workload_monitoring_yaml() -> str:
    config_yaml = yaml.safe_dump(_monitoring_config_data(), default_flow_style=False).rstrip()
    indented = "\n".join(f"    {line}" for line in config_yaml.splitlines())
    return f"""\
apiVersion: v1
kind: ConfigMap
metadata:
  name: {_MONITORING_CM}
  namespace: {_MONITORING_NS}
data:
  config.yaml: |
{indented}
"""


def _user_workload_monitoring_enabled() -> bool:
    r = oc_run(
        [
            "get",
            "configmap",
            _MONITORING_CM,
            "-n",
            _MONITORING_NS,
            "-o",
            "jsonpath={.data.config\\.yaml}",
        ],
        check=False,
        capture_output=True,
        timeout=30,
    )
    if r.returncode != 0:
        return False
    normalized = (r.stdout or "").replace(" ", "")
    return "enableUserWorkload:true" in normalized


def ensure_user_workload_monitoring() -> None:
    """Enable OpenShift User Workload Monitoring (MaaSPrerequisitesAvailable)."""
    if _user_workload_monitoring_enabled():
        print(f"✓ User Workload Monitoring enabled ({_MONITORING_NS}/{_MONITORING_CM})", flush=True)
        return
    print(
        f"Applying {_MONITORING_NS}/{_MONITORING_CM} (enableUserWorkload: true)...",
        flush=True,
    )
    apply = oc_run(
        ["apply", "-f", "-"],
        stdin_text=_user_workload_monitoring_yaml(),
        check=False,
        capture_output=True,
        timeout=60,
    )
    if apply.returncode != 0:
        err = (apply.stderr or apply.stdout or "").strip()
        raise RuntimeError(f"Could not configure User Workload Monitoring: {err or 'unknown error'}")
    if not _user_workload_monitoring_enabled():
        raise RuntimeError(
            f"{_MONITORING_CM} applied but enableUserWorkload is still not true "
            f"in {_MONITORING_NS}"
        )
    print(f"✓ User Workload Monitoring configured ({_MONITORING_NS}/{_MONITORING_CM})", flush=True)
