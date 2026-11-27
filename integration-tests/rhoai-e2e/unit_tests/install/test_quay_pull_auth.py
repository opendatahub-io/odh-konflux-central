"""Tests for Vault-first quay pull credential resolution."""

from __future__ import annotations

import json
from pathlib import Path
from unittest import mock

import pytest

from install.quay_pull_auth import load_quay_pull_dockerconfig, resolve_quay_pull_secret_source


@pytest.mark.parametrize(
    ("env", "expected"),
    [
        ({}, "vault"),
        ({"SECRET_SOURCE": "tenant"}, "tenant"),
        ({"QUAY_PULL_SECRET_SOURCE": "tenant"}, "tenant"),
        ({"QUAY_PULL_SECRET_SOURCE": "vault", "SECRET_SOURCE": "tenant"}, "vault"),
    ],
)
def test_resolve_quay_pull_secret_source(env: dict[str, str], expected: str) -> None:
    assert resolve_quay_pull_secret_source(env) == expected


def test_load_quay_vault_first(tmp_path: Path) -> None:
    mount = tmp_path / "quay.json"
    mount.write_text(
        json.dumps({"auths": {"quay.io/rhoai": {"auth": "b64stale"}}}),
        encoding="utf-8",
    )
    vault_doc = {"auths": {"quay.io/rhoai": {"auth": "b64fresh"}}}
    with mock.patch(
        "install.quay_pull_auth.fetch_quay_dockerconfig_from_vault",
        return_value=vault_doc,
    ):
        assert (
            load_quay_pull_dockerconfig(
                environ={"SECRET_SOURCE": "vault", "QUAY_PULL_SECRET_PATH": str(mount)}
            )
            == vault_doc
        )


def test_load_quay_tenant_mode_prefers_mount(tmp_path: Path) -> None:
    mount = tmp_path / "quay.json"
    mount.write_text(
        json.dumps({"auths": {"quay.io/rhoai": {"auth": "tenant"}}}),
        encoding="utf-8",
    )
    with mock.patch(
        "install.quay_pull_auth.fetch_quay_dockerconfig_from_vault",
        return_value={"auths": {"quay.io/rhoai": {"auth": "vault"}}},
    ):
        doc = load_quay_pull_dockerconfig(
            environ={
                "SECRET_SOURCE": "tenant",
                "QUAY_PULL_SECRET_PATH": str(mount),
            }
        )
    assert doc == {"auths": {"quay.io/rhoai": {"auth": "tenant"}}}


def test_load_quay_vault_mode_skips_stale_tenant_quay(tmp_path: Path) -> None:
    stale = tmp_path / "quay.json"
    stale.write_text(
        json.dumps({"auths": {"quay.io/rhoai": {"auth": "stale"}}}),
        encoding="utf-8",
    )
    with mock.patch(
        "install.quay_pull_auth.fetch_quay_dockerconfig_from_vault",
        return_value=None,
    ):
        assert (
            load_quay_pull_dockerconfig(
                environ={
                    "SECRET_SOURCE": "vault",
                    "QUAY_PULL_SECRET_PATH": str(stale),
                }
            )
            is None
        )


def test_load_quay_vault_mode_allows_stale_tenant_when_opted_in(tmp_path: Path) -> None:
    stale = tmp_path / "quay.json"
    stale.write_text(
        json.dumps({"auths": {"quay.io/rhoai": {"auth": "stale"}}}),
        encoding="utf-8",
    )
    with mock.patch(
        "install.quay_pull_auth.fetch_quay_dockerconfig_from_vault",
        return_value=None,
    ):
        doc = load_quay_pull_dockerconfig(
            environ={
                "SECRET_SOURCE": "vault",
                "QUAY_PULL_SECRET_PATH": str(stale),
                "QUAY_PULL_ALLOW_TENANT_FALLBACK": "1",
            }
        )
    assert doc == {"auths": {"quay.io/rhoai": {"auth": "stale"}}}