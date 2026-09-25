"""Remove stale Model Registry smoke fixtures on pooled external clusters."""

from __future__ import annotations

import json
import re

from install.dsc_install import oc_run

_MODEL_REGISTRY_NS = "rhoai-model-registries"
_DB_SECRET_RE = re.compile(r"^db-model-registry\d+$")
_TEST_INSTANCE_RE = re.compile(r"^model-registry-\d+$")
_PROTECTED_DEPLOYS = frozenset({"model-catalog"})


def _delete_matching_resources(namespace: str, *, kind: str, name_re: re.Pattern[str], label: str) -> None:
    listed = oc_run(
        ["get", kind, "-n", namespace, "-o", "json"],
        check=False,
        capture_output=True,
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
        name = str((item.get("metadata") or {}).get("name") or "")
        if name_re.match(name):
            oc_run(
                ["delete", kind, name, "-n", namespace, "--ignore-not-found"],
                check=False,
                capture_output=True,
                timeout=60,
            )
            print(f"✓ Removed stale Model Registry {label} {namespace}/{name}", flush=True)


def _delete_matching_pvcs(namespace: str) -> None:
    _delete_matching_resources(namespace, kind="pvc", name_re=_DB_SECRET_RE, label="PVC")


def _delete_matching_services(namespace: str) -> None:
    _delete_matching_resources(namespace, kind="service", name_re=_DB_SECRET_RE, label="service")


def _delete_matching_secrets(namespace: str) -> None:
    _delete_matching_resources(namespace, kind="secret", name_re=_DB_SECRET_RE, label="secret")


def _delete_test_model_registry_instances(namespace: str) -> None:
    for kind in ("modelregistry", "modelregistryservice"):
        listed = oc_run(
            ["get", kind, "-n", namespace, "-o", "json"],
            check=False,
            capture_output=True,
            timeout=60,
        )
        if listed.returncode != 0:
            continue
        try:
            doc = json.loads(listed.stdout or "{}")
        except json.JSONDecodeError:
            continue
        for item in doc.get("items") or []:
            if not isinstance(item, dict):
                continue
            name = str((item.get("metadata") or {}).get("name") or "")
            if _TEST_INSTANCE_RE.match(name):
                oc_run(
                    ["delete", kind, name, "-n", namespace, "--ignore-not-found"],
                    check=False,
                    capture_output=True,
                    timeout=60,
                )
                print(f"✓ Removed stale Model Registry {kind} {namespace}/{name}", flush=True)


def _delete_test_db_deployments(namespace: str) -> None:
    listed = oc_run(
        ["get", "deploy", "-n", namespace, "-o", "jsonpath={range .items[*]}{.metadata.name}{\"\\n\"}{end}"],
        check=False,
        capture_output=True,
        timeout=60,
    )
    if listed.returncode != 0:
        return
    for name in (listed.stdout or "").splitlines():
        deploy = name.strip()
        if not deploy or deploy in _PROTECTED_DEPLOYS:
            continue
        if deploy.startswith("db-model-registry") or _TEST_INSTANCE_RE.match(deploy):
            oc_run(
                ["delete", "deploy", deploy, "-n", namespace, "--ignore-not-found"],
                check=False,
                capture_output=True,
                timeout=60,
            )
            print(f"✓ Removed stale Model Registry deployment {namespace}/{deploy}", flush=True)


def cleanup_model_registry_smoke_leaks() -> None:
    """Drop leaked MR pytest fixtures so opendatahub-tests can recreate db secrets."""
    _delete_test_model_registry_instances(_MODEL_REGISTRY_NS)
    _delete_test_db_deployments(_MODEL_REGISTRY_NS)
    _delete_matching_services(_MODEL_REGISTRY_NS)
    _delete_matching_pvcs(_MODEL_REGISTRY_NS)
    _delete_matching_secrets(_MODEL_REGISTRY_NS)
