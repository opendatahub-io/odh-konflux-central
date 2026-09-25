"""Pipeline unit tests for integration-tests/rhoai-e2e (no cluster)."""

from __future__ import annotations

import subprocess
from unittest import mock

import pytest

pytest_plugins = ["unit_tests.runners.rhoai_e2e_cli_fixtures"]

# Env keys that leak across tests when patch.dict(..., clear=False) is overused.
_LEAKY_ENV_KEYS = (
    "PRODUCT",
    "CLUSTER_SOURCE",
    "INSTALL_DEPENDENCIES",
    "RUN_COMPONENT_CLUSTER_PREP_IN_DEP_OPERATORS",
    "DASHBOARD_SOURCE_REF_OVERRIDE",
    "DASHBOARD_SOURCE_REPO_OVERRIDE",
    "DASHBOARD_DEPLOYMENT_WAIT_SEC",
    "DASHBOARD_ROUTE_VERIFY_TIMEOUT_SEC",
    "VERIFY_GATEWAY_CLASS_WAIT_SEC",
)

_OC_PATCHER_ATTR = "_rhoai_e2e_no_live_oc_patchers"


def _fake_oc_run(*args, **kwargs):
    cmd = args[0] if args else []
    if not isinstance(cmd, list):
        cmd = [str(cmd)]
    return subprocess.CompletedProcess(cmd, 1, "", "")


def pytest_collection_modifyitems(items: list[pytest.Item]) -> None:
    for item in items:
        item.add_marker(pytest.mark.unit)


@pytest.hookimpl(tryfirst=True)
def pytest_runtest_setup(item: pytest.Item) -> None:
    """Block accidental live ``oc`` from LDAP BYOIDC probes; tests patch ``oc_run`` when needed."""
    patcher = mock.patch("install.ldap.oc_run", side_effect=_fake_oc_run)
    patcher.start()
    setattr(item, _OC_PATCHER_ATTR, [patcher])


@pytest.hookimpl(trylast=True)
def pytest_runtest_teardown(item: pytest.Item, nextitem: pytest.Item | None) -> None:
    for patcher in getattr(item, _OC_PATCHER_ATTR, []):
        patcher.stop()


@pytest.fixture(autouse=True)
def _reset_leaky_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep PRODUCT/CLUSTER_SOURCE defaults stable across the suite (and xdist workers)."""
    for key in _LEAKY_ENV_KEYS:
        monkeypatch.delenv(key, raising=False)
