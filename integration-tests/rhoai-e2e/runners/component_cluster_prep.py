#!/usr/bin/env python3
"""Cluster prep before component pytest (LDAP, MaaS DSC sync/waits).

Dependency operators (Kuadrant/Authorino, RHCL) are installed by Tekton ``install-dep-operators``.
This module runs the rest of smoke cluster prep (DSC, MaaS gateway, LDAP, dashboard route, …).

With ``--install-dependencies`` / ``INSTALL_DEPENDENCIES=true``, both phases run in
``install-dep-operators`` (deps step, then ``prepare-component-cluster``). The legacy
``prepare-components-prerequisites`` step in ``opendatahub-tests-prepare`` is skipped.

Env:
    COMPONENTS_CSV              -- selected catalog ids (comma-separated)
    COMPONENT_TEST_PLAN_JSON   -- optional plan JSON (preferred in Tekton)
    ODS_INSTALL_REPO_URL        -- ods-install clone URL (default: Red Hat internal)
    ODS_INSTALL_REPO_REVISION   -- branch/SHA for ods-install
    ODS_INSTALL_DIR             -- use existing clone instead of fetching
    AUTHORINO_NAMESPACE         -- override Authorino namespace (default: auto-detect)
    KUBECONFIG                  -- target cluster
"""

from __future__ import annotations

from _bootstrap import ensure_rhoai_e2e_path

ensure_rhoai_e2e_path()

from components.maas_billing.gateway import (  # noqa: E402
    _gateway_yaml,
)
from components.maas_billing.common import (  # noqa: E402
    _maas_smoke_ready,
)
from components.maas_billing.uwm import _user_workload_monitoring_yaml  # noqa: E402
from runners.orchestrator import (  # noqa: E402
    main,
    prepare_cluster_for_components,
    stage_git_for_prereqs,
    stage_oc_for_pytest,
)
from runners.selection import (  # noqa: E402
    _selected_component_ids,
    selected_component_ids,
)

__all__ = [
    "_gateway_yaml",
    "_maas_smoke_ready",
    "_selected_component_ids",
    "_user_workload_monitoring_yaml",
    "main",
    "prepare_cluster_for_components",
    "selected_component_ids",
    "stage_git_for_prereqs",
    "stage_oc_for_pytest",
]

if __name__ == "__main__":
    raise SystemExit(main())
