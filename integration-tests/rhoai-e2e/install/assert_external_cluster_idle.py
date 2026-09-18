#!/usr/bin/env python3
"""Wait until no other rhoai-e2e PipelineRun holds the same external cluster (resource-lock)."""

from __future__ import annotations

import os
import sys

from steps.tekton_incluster import namespace_from_env, pipeline_run_name_from_env
from k8s.external_kubeconfig import wait_for_external_cluster_idle
from suite.constants import DEFAULT_CLUSTER_IDLE_POLL_SEC, DEFAULT_CLUSTER_IDLE_WAIT_SEC
from suite.errors import AppError
from suite.its_trigger_params import is_external_cluster_source


_TRUTHY = frozenset({"1", "true", "yes"})


def _env_bool(name: str) -> bool:
    return os.environ.get(name, "").strip().lower() in _TRUTHY


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError:
        return default


def main() -> int:
    cluster_source = os.environ.get("CLUSTER_SOURCE", "").strip()
    if not is_external_cluster_source(cluster_source):
        return 0
    try:
        wait_for_external_cluster_idle(
            namespace=namespace_from_env(required=True),
            cluster_source=cluster_source,
            cluster_id=(os.environ.get("CLUSTER_ID", "") or "").strip(),
            exclude_pipelinerun=pipeline_run_name_from_env(required=True),
            force=_env_bool("FORCE_CLUSTER_RUN"),
            timeout_sec=_env_int("CLUSTER_IDLE_WAIT_SEC", DEFAULT_CLUSTER_IDLE_WAIT_SEC),
            poll_interval_sec=_env_int("CLUSTER_IDLE_POLL_SEC", DEFAULT_CLUSTER_IDLE_POLL_SEC),
        )
    except AppError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    print(f"External cluster idle check OK for CLUSTER_SOURCE={cluster_source!r}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
