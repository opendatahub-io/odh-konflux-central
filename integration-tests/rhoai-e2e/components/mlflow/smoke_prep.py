"""Ensure pooled-cluster MLflow is runnable before mlflow-tests smoke."""

from __future__ import annotations

import time

from install.dsc_install import oc_run

_APPS_NS = "redhat-ods-applications"
_OPERATOR_DEPLOY = "mlflow-operator-controller-manager"
_MLFLOW_DEPLOY = "mlflow"
_READY_WAIT_SEC = 5
_TEST_POSTGRES_RESOURCES = (
    ("deploy", "postgres-deployment"),
    ("pvc", "postgres-pvc"),
    ("secret", "postgres-secret"),
    ("secret", "postgres-tls-certs"),
    ("service", "postgres-service"),
    ("configmap", "postgres-params"),
)


def _deployment_replicas(namespace: str, name: str) -> int | None:
    r = oc_run(
        [
            "get",
            "deploy",
            name,
            "-n",
            namespace,
            "-o",
            "jsonpath={.spec.replicas}",
        ],
        check=False,
        capture_output=True,
        timeout=30,
    )
    if r.returncode != 0:
        return None
    raw = (r.stdout or "").strip()
    if not raw:
        return None
    try:
        return int(raw)
    except ValueError:
        return None


def _scale_deployment(namespace: str, name: str, replicas: int) -> None:
    oc_run(
        [
            "scale",
            "deploy",
            name,
            "-n",
            namespace,
            f"--replicas={replicas}",
        ],
        check=False,
        capture_output=True,
        timeout=60,
    )


def _delete_prior_mlflow_test_stack(namespace: str) -> None:
    """Remove partial mlflow-tests deploy artifacts from a prior failed run."""
    for kind, name in _TEST_POSTGRES_RESOURCES:
        oc_run(
            ["delete", kind, name, "-n", namespace, "--ignore-not-found"],
            check=False,
            capture_output=True,
            timeout=60,
        )


def ensure_mlflow_smoke_ready_on_existing(*, namespace: str = _APPS_NS) -> None:
    """Restore operator and MLflow workload after BVT quiesce on resource-tight clusters."""
    from steps.prepare_bvt_apps_namespace import (
        _mlflow_operator_replicas,
        _quiesce_mlflow_migration_for_bvt,
        _scale_mlflow_operator,
    )

    _delete_prior_mlflow_test_stack(namespace)

    op_replicas = _mlflow_operator_replicas(namespace)
    if op_replicas == 0:
        print(f"Scaling {_OPERATOR_DEPLOY} to 1 before mlflow smoke (was 0)", flush=True)
        _scale_mlflow_operator(namespace, 1)
        time.sleep(_READY_WAIT_SEC)

    _quiesce_mlflow_migration_for_bvt(namespace)

    mlflow_replicas = _deployment_replicas(namespace, _MLFLOW_DEPLOY)
    if mlflow_replicas == 0:
        print(f"Scaling {_MLFLOW_DEPLOY} deployment to 1 before mlflow smoke (was 0)", flush=True)
        _scale_deployment(namespace, _MLFLOW_DEPLOY, 1)
        time.sleep(_READY_WAIT_SEC)

    # deploy.py recreates the CR; drop stale workload so the operator can reconcile cleanly.
    oc_run(
        ["delete", "mlflow", "mlflow", "-n", namespace, "--ignore-not-found"],
        check=False,
        capture_output=True,
        timeout=60,
    )
    oc_run(
        ["delete", "deploy", _MLFLOW_DEPLOY, "-n", namespace, "--ignore-not-found"],
        check=False,
        capture_output=True,
        timeout=60,
    )
