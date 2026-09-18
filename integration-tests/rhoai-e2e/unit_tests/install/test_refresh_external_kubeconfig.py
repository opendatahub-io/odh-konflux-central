"""Tests for install.refresh_external_kubeconfig Tekton entrypoint."""

from __future__ import annotations

from pathlib import Path
from unittest import mock

import pytest

from install import refresh_external_kubeconfig as mod
from suite.errors import AppError


def test_refresh_external_kubeconfig_skips_non_external(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CLUSTER_SOURCE", "EPHC")
    assert mod.refresh_external_kubeconfig() == 0


def test_refresh_external_kubeconfig_uses_credentials(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bootstrap = tmp_path / "bootstrap" / "kubeconfig"
    bootstrap.parent.mkdir(parents=True)
    bootstrap.write_text("bootstrap", encoding="utf-8")
    work = tmp_path / "kubeconfig"
    work.write_text("refreshed", encoding="utf-8")
    shared = tmp_path / "shared"
    shared.mkdir()

    monkeypatch.setenv("CLUSTER_SOURCE", "rhoai-e2e-kubeconfig-rh-nightly-pm")
    monkeypatch.setenv("NAMESPACE", "rhoai-tenant")
    monkeypatch.setenv("KUBECONFIG_BOOTSTRAP", str(bootstrap))
    monkeypatch.setenv("KUBECONFIG", str(work))
    monkeypatch.setenv("TESTS_SHARED", str(shared))

    with (
        mock.patch.object(
            mod,
            "refresh_working_kubeconfig_from_credentials",
            return_value=(True, "tenant Secret 'rhoai-e2e-external-rh-nightly-pm-credentials'"),
        ),
        mock.patch.object(mod, "verify_external_cluster_login", return_value="dev"),
        mock.patch.object(mod, "update_external_kubeconfig_secret") as update_secret,
        mock.patch.object(mod, "sync_external_kubeconfig_secret_cluster_metadata") as sync_metadata,
    ):
        assert mod.refresh_external_kubeconfig() == 0

    update_secret.assert_called_once()
    sync_metadata.assert_called_once()
    assert (shared / "credentials" / "kubeconfig").read_text(encoding="utf-8") == work.read_text(encoding="utf-8")


def test_refresh_external_kubeconfig_falls_back_to_bootstrap(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bootstrap = tmp_path / "bootstrap" / "kubeconfig"
    bootstrap.parent.mkdir(parents=True)
    bootstrap.write_text("bootstrap-token", encoding="utf-8")
    work = tmp_path / "kubeconfig"

    monkeypatch.setenv("CLUSTER_SOURCE", "rhoai-e2e-kubeconfig-rh-nightly-pm")
    monkeypatch.setenv("NAMESPACE", "rhoai-tenant")
    monkeypatch.setenv("KUBECONFIG_BOOTSTRAP", str(bootstrap))
    monkeypatch.setenv("KUBECONFIG", str(work))

    with (
        mock.patch.object(mod, "refresh_working_kubeconfig_from_credentials", return_value=(False, "")),
        mock.patch.object(mod, "verify_external_cluster_login", return_value="bootstrap-user"),
        mock.patch.object(mod, "update_external_kubeconfig_secret") as update_secret,
    ):
        assert mod.refresh_external_kubeconfig() == 0

    assert work.read_text(encoding="utf-8") == "bootstrap-token"
    update_secret.assert_not_called()


def test_refresh_external_kubeconfig_missing_bootstrap_and_creds(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    work = tmp_path / "kubeconfig"
    monkeypatch.setenv("CLUSTER_SOURCE", "rhoai-e2e-kubeconfig-rh-nightly-pm")
    monkeypatch.setenv("NAMESPACE", "rhoai-tenant")
    monkeypatch.setenv("KUBECONFIG_BOOTSTRAP", str(tmp_path / "missing" / "kubeconfig"))
    monkeypatch.setenv("KUBECONFIG", str(work))

    with mock.patch.object(mod, "refresh_working_kubeconfig_from_credentials", return_value=(False, "")):
        assert mod.refresh_external_kubeconfig() == 1


def test_refresh_external_kubeconfig_write_back_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    work = tmp_path / "kubeconfig"
    work.write_text("refreshed", encoding="utf-8")
    monkeypatch.setenv("CLUSTER_SOURCE", "rhoai-e2e-kubeconfig-rh-nightly-pm")
    monkeypatch.setenv("NAMESPACE", "rhoai-tenant")
    monkeypatch.setenv("KUBECONFIG", str(work))

    with (
        mock.patch.object(
            mod,
            "refresh_working_kubeconfig_from_credentials",
            return_value=(True, "tenant Secret 'rhoai-e2e-external-rh-nightly-pm-credentials'"),
        ),
        mock.patch.object(mod, "verify_external_cluster_login", return_value="dev"),
        mock.patch.object(
            mod,
            "update_external_kubeconfig_secret",
            side_effect=AppError("apply failed", 1),
        ),
    ):
        assert mod.refresh_external_kubeconfig() == 1


def test_refresh_external_kubeconfig_login_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("CLUSTER_SOURCE", "rhoai-e2e-kubeconfig-rh-nightly-pm")
    monkeypatch.setenv("NAMESPACE", "rhoai-tenant")
    monkeypatch.setenv("KUBECONFIG", str(tmp_path / "kubeconfig"))

    with mock.patch.object(
        mod,
        "refresh_working_kubeconfig_from_credentials",
        side_effect=AppError("htpasswd oc login failed", 1),
    ):
        assert mod.refresh_external_kubeconfig() == 1
