"""Pytest plugin: patch opendatahub-tests OGX server_config for EA.2 (rh-dev only).

Upstream ``build_ogx_server_config`` hardcodes ``distribution.name: rh``; EA.2
webhook only accepts ``rh-dev``. Loaded via ``-p ogx_ea_distribution_plugin`` for ogx smokes.
"""

from __future__ import annotations

_OGX_EA_DISTRIBUTION = "rh-dev"


def apply_ogx_ea_distribution_patch() -> bool:
    """Monkeypatch ``build_ogx_server_config`` so OGXServer uses rh-dev on EA.2 clusters."""
    try:
        import tests.ogx.server_config as server_config
    except ImportError:
        return False
    if getattr(server_config.build_ogx_server_config, "_ogx_ea_patched", False):
        _sync_conftest_build_ogx_server_config(server_config.build_ogx_server_config)
        return True

    _orig = server_config.build_ogx_server_config

    def _build_ogx_server_config(*args, **kwargs):
        cfg = _orig(*args, **kwargs)
        dist = cfg.get("distribution") or {}
        if dist.get("name") == "rh":
            cfg = {**cfg, "distribution": {**dist, "name": _OGX_EA_DISTRIBUTION}}
        return cfg

    _build_ogx_server_config._ogx_ea_patched = True  # type: ignore[attr-defined]
    server_config.build_ogx_server_config = _build_ogx_server_config
    _sync_conftest_build_ogx_server_config(_build_ogx_server_config)
    return True


def _sync_conftest_build_ogx_server_config(patched_fn) -> None:
    """Fix ``from server_config import build_ogx_server_config`` in tests.ogx.conftest."""
    try:
        import tests.ogx.conftest as ogx_conftest
    except ImportError:
        return
    if getattr(ogx_conftest, "build_ogx_server_config", None) is not patched_fn:
        ogx_conftest.build_ogx_server_config = patched_fn


def pytest_configure(config) -> None:  # noqa: ARG001
    if apply_ogx_ea_distribution_patch():
        print("✓ ogx_ea_distribution_plugin: build_ogx_server_config → rh-dev", flush=True)
