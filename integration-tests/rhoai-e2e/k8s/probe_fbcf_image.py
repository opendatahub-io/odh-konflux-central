"""Resolve installed operator catalog image from cluster Subscription/CatalogSource."""

from __future__ import annotations

import json
import os
import tempfile
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

from steps.prepare_diagnostics_kubeconfig import _fetch_external_kubeconfig, _namespace
from runners.report.pipelinerun_summary import task_result


def _catalog_image_from_subscription(operator_namespace: str, operator_name: str) -> str:
    from install.install_and_verify import oc_run

    op_ns = (operator_namespace or "").strip()
    op_name = (operator_name or "").strip()
    if not op_ns or not op_name:
        return ""

    sub_r = oc_run(
        ["get", "subscription", op_name, "-n", op_ns, "-o", "json"],
        check=False,
        timeout=30,
    )
    if sub_r.returncode != 0 or not (sub_r.stdout or "").strip():
        return ""
    try:
        spec = (json.loads(sub_r.stdout).get("spec") or {})
    except json.JSONDecodeError:
        return ""

    catalog_name = (spec.get("source") or "").strip()
    catalog_ns = (spec.get("sourceNamespace") or "openshift-marketplace").strip()
    if not catalog_name:
        return ""

    img_r = oc_run(
        [
            "get",
            "catalogsource",
            catalog_name,
            "-n",
            catalog_ns,
            "-o",
            "jsonpath={.spec.image}",
        ],
        check=False,
        timeout=30,
    )
    return (img_r.stdout or "").strip() if img_r.returncode == 0 else ""


@contextmanager
def _kubeconfig_env(path: Path) -> Iterator[None]:
    prev = os.environ.get("KUBECONFIG")
    os.environ["KUBECONFIG"] = str(path)
    try:
        yield
    finally:
        if prev is None:
            os.environ.pop("KUBECONFIG", None)
        else:
            os.environ["KUBECONFIG"] = prev


def _probe_with_kubeconfig_file(
    kubeconfig: Path,
    operator_namespace: str,
    operator_name: str,
) -> str:
    if not kubeconfig.is_file():
        return ""
    with _kubeconfig_env(kubeconfig):
        return _catalog_image_from_subscription(operator_namespace, operator_name)


def _probe_from_external_secret(
    secret_name: str,
    operator_namespace: str,
    operator_name: str,
) -> str:
    ns = _namespace()
    if not (secret_name and ns and operator_namespace):
        return ""
    try:
        content = _fetch_external_kubeconfig(secret_name, ns)
    except (OSError, ValueError, RuntimeError):
        return ""
    tmp_fd, tmp_path = tempfile.mkstemp(suffix="-kubeconfig")
    os.close(tmp_fd)
    tmp = Path(tmp_path)
    try:
        tmp.write_text(content, encoding="utf-8")
        try:
            return _probe_with_kubeconfig_file(tmp, operator_namespace, operator_name)
        except Exception:
            return ""
    finally:
        tmp.unlink(missing_ok=True)


def resolve_fbcf_image(
    taskruns: list[dict[str, Any]],
    *,
    extract_task_result: str = "",
    product: str = "",
    external_kubeconfig_secret: str = "",
    tests_shared_kubeconfig: str = "",
    operator_namespace: str = "",
    operator_name: str = "rhods-operator",
) -> str:
    """Snapshot image for install runs; cluster catalog for existing-product runs."""
    from_task = (
        (extract_task_result or "").strip()
        or task_result(taskruns, "extract-fbcf-image", "FBCF_IMAGE")
        or ""
    ).strip()
    if from_task and from_task not in ("n/a", "(unknown)"):
        return from_task

    op_ns = (operator_namespace or "").strip()
    op_name = (operator_name or "").strip() or "rhods-operator"
    image = ""
    secret = (external_kubeconfig_secret or "").strip()
    if secret:
        image = _probe_from_external_secret(secret, op_ns, op_name)
    if not image:
        kc_path = (tests_shared_kubeconfig or "").strip()
        if kc_path:
            image = _probe_with_kubeconfig_file(Path(kc_path), op_ns, op_name)

    if image:
        return image
    from suite.constants import is_test_only_product

    if is_test_only_product(product):
        return from_task if from_task and from_task != "(unknown)" else "n/a"
    return from_task if from_task and from_task != "n/a" else "(unknown)"
