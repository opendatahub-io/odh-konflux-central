#!/usr/bin/env python3
"""Tekton step: resolve distributed-workloads-tests image for golang KFTO/trainer."""

from __future__ import annotations

import os
from pathlib import Path

from _bootstrap import ensure_rhoai_e2e_path

ensure_rhoai_e2e_path()

from k8s.probe_operator_version import resolve_operator_version  # noqa: E402
from suite.its_trigger_params import external_kubeconfig_secret_name  # noqa: E402
from steps.resolve_distributed_workloads_tests_image import main as resolve_main  # noqa: E402
from steps.tekton_incluster import (  # noqa: E402
    list_taskruns_in_cluster,
    namespace_from_env,
    pipeline_run_name_from_env,
)
from steps.tekton_util import require_env  # noqa: E402


def main() -> int:
    require_env("RESULT_PATH")
    pr_name = pipeline_run_name_from_env(required=True)
    ns = namespace_from_env(required=True)
    taskruns = list_taskruns_in_cluster(pr_name, ns)

    ver = resolve_operator_version(
        taskruns,
        pipeline_run=pr_name,
        namespace=ns,
        external_kubeconfig_secret=external_kubeconfig_secret_name(os.environ.get("CLUSTER_SOURCE", "")),
        operator_namespace=os.environ.get("OPERATOR_NAMESPACE", ""),
        operator_name=os.environ.get("OPERATOR_NAME", "rhods-operator"),
        poll_collect_diagnostics=False,
        product=os.environ.get("PRODUCT", ""),
    )
    if ver:
        os.environ["OPERATOR_VERSION"] = ver
        print(f"Using operator version {ver!r} for distributed-workloads-tests image resolve")

    return resolve_main()


if __name__ == "__main__":
    raise SystemExit(main())
