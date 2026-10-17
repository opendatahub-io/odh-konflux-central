"""Tests for ogx_ea_distribution_plugin."""

from __future__ import annotations

import sys
import types

import pytest


def test_ogx_ea_plugin_patches_rh_distribution(monkeypatch: pytest.MonkeyPatch) -> None:
    server_config = types.ModuleType("tests.ogx.server_config")

    def _orig_build(**_kwargs):
        return {"distribution": {"name": "rh"}, "workload": {}}

    server_config.build_ogx_server_config = _orig_build

    ogx_pkg = types.ModuleType("tests.ogx")
    tests_pkg = types.ModuleType("tests")
    tests_pkg.ogx = ogx_pkg
    ogx_pkg.server_config = server_config

    monkeypatch.setitem(sys.modules, "tests", tests_pkg)
    monkeypatch.setitem(sys.modules, "tests.ogx", ogx_pkg)
    monkeypatch.setitem(sys.modules, "tests.ogx.server_config", server_config)

    import ogx_ea_distribution_plugin as plugin

    assert plugin.apply_ogx_ea_distribution_patch() is True
    plugin.pytest_configure(None)

    cfg = server_config.build_ogx_server_config()
    assert cfg["distribution"]["name"] == "rh-dev"


def test_apply_ogx_ea_distribution_patch_idempotent(monkeypatch: pytest.MonkeyPatch) -> None:
    server_config = types.ModuleType("tests.ogx.server_config")

    def _orig_build(**_kwargs):
        return {"distribution": {"name": "rh"}, "workload": {}}

    server_config.build_ogx_server_config = _orig_build

    ogx_conftest = types.ModuleType("tests.ogx.conftest")
    ogx_conftest.build_ogx_server_config = _orig_build

    ogx_pkg = types.ModuleType("tests.ogx")
    tests_pkg = types.ModuleType("tests")
    tests_pkg.ogx = ogx_pkg
    ogx_pkg.server_config = server_config
    ogx_pkg.conftest = ogx_conftest

    monkeypatch.setitem(sys.modules, "tests", tests_pkg)
    monkeypatch.setitem(sys.modules, "tests.ogx", ogx_pkg)
    monkeypatch.setitem(sys.modules, "tests.ogx.server_config", server_config)
    monkeypatch.setitem(sys.modules, "tests.ogx.conftest", ogx_conftest)

    import ogx_ea_distribution_plugin as plugin

    assert plugin.apply_ogx_ea_distribution_patch() is True
    assert plugin.apply_ogx_ea_distribution_patch() is True
    assert ogx_conftest.build_ogx_server_config is server_config.build_ogx_server_config
    assert ogx_conftest.build_ogx_server_config()["distribution"]["name"] == "rh-dev"
