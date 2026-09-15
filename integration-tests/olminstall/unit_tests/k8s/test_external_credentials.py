"""Tests for Konflux external-cluster htpasswd credentials (RHOAIENG-57718)."""

from __future__ import annotations

import json
from pathlib import Path
from unittest import mock

import pytest

from k8s.external_credentials import (
    external_credentials_secret_name,
    load_external_cluster_credentials,
    refresh_working_kubeconfig_from_credentials,
    seed_working_kubeconfig,
    update_external_kubeconfig_secret,
    write_minimal_kubeconfig,
)
from suite.errors import AppError


def test_external_credentials_secret_name_maps_kubeconfig_secret() -> None:
    assert (
        external_credentials_secret_name("olminstall-kubeconfig-rh-nightly-pm")
        == "olminstall-external-rh-nightly-pm-credentials"
    )


def test_external_credentials_secret_name_override() -> None:
    assert external_credentials_secret_name("ignored", override="custom-secret") == "custom-secret"


def test_external_credentials_secret_name_non_external() -> None:
    assert external_credentials_secret_name("EPHC") == ""
    assert external_credentials_secret_name("rhoai-external-quay-secret") == ""


def test_load_external_cluster_credentials_ok() -> None:
    import base64

    payload = {
        "data": {
            "HTPASSWD_USER": base64.b64encode(b"dev").decode(),
            "HTPASSWD_PASS": base64.b64encode(b"secret").decode(),
            "API_SERVER": base64.b64encode(b"https://api.example:6443").decode(),
        }
    }
    with mock.patch("k8s.external_credentials.run_cmd") as run_cmd:
        run_cmd.return_value.returncode = 0
        run_cmd.return_value.stdout = json.dumps(payload)
        creds = load_external_cluster_credentials(namespace="rhoai-tenant", secret_name="olminstall-external-x-credentials")
    assert creds is not None
    assert creds.username == "dev"
    assert creds.password == "secret"
    assert creds.api_server == "https://api.example:6443"


def test_load_external_cluster_credentials_missing_secret() -> None:
    with mock.patch("k8s.external_credentials.run_cmd") as run_cmd:
        run_cmd.return_value.returncode = 1
        assert load_external_cluster_credentials(namespace="ns", secret_name="missing") is None


def test_write_minimal_kubeconfig_insecure_when_no_ca(tmp_path: Path) -> None:
    path = tmp_path / "kubeconfig"
    write_minimal_kubeconfig(path=path, api_server="https://api.test:6443")
    text = path.read_text(encoding="utf-8")
    assert "insecure-skip-tls-verify: true" in text
    assert path.stat().st_mode & 0o777 == 0o600


def test_seed_working_kubeconfig_copies_bootstrap(tmp_path: Path) -> None:
    bootstrap = tmp_path / "bootstrap" / "kubeconfig"
    bootstrap.parent.mkdir(parents=True)
    bootstrap.write_text(
        "\n".join(
            [
                "apiVersion: v1",
                "kind: Config",
                "clusters:",
                "  - name: c",
                "    cluster:",
                "      server: https://api.test:6443",
                "contexts:",
                "  - name: ctx",
                "    context:",
                "      cluster: c",
                "      user: u",
                "current-context: ctx",
                "users:",
                "  - name: u",
                "    user:",
                "      token: old",
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    work = tmp_path / "work" / "kubeconfig"
    seed_working_kubeconfig(work_path=work, bootstrap_path=bootstrap, api_server="https://api.test:6443")
    assert work.read_text(encoding="utf-8") == bootstrap.read_text(encoding="utf-8")


def test_refresh_working_kubeconfig_from_credentials_no_secret(tmp_path: Path) -> None:
    bootstrap = tmp_path / "bootstrap"
    bootstrap.write_text("bootstrap", encoding="utf-8")
    work = tmp_path / "work"
    with mock.patch(
        "k8s.external_credentials.resolve_external_cluster_credentials",
        return_value=(None, ""),
    ):
        used, source = refresh_working_kubeconfig_from_credentials(
            namespace="ns",
            cluster_source="olminstall-kubeconfig-rh-nightly-pm",
            bootstrap_path=bootstrap,
            work_path=work,
        )
        assert used is False
        assert source == ""


def test_refresh_working_kubeconfig_from_credentials_logs_in(tmp_path: Path) -> None:
    from k8s.external_credentials import ExternalClusterCredentials

    bootstrap = tmp_path / "bootstrap"
    bootstrap.write_text("bootstrap", encoding="utf-8")
    work = tmp_path / "work"
    creds = ExternalClusterCredentials(
        username="dev",
        password="secret",
        api_server="https://api.test:6443",
    )
    with (
        mock.patch(
            "k8s.external_credentials.resolve_external_cluster_credentials",
            return_value=(creds, "tenant Secret 'olminstall-external-rh-nightly-pm-credentials'"),
        ),
        mock.patch("k8s.external_credentials.seed_working_kubeconfig") as seed,
        mock.patch(
            "steps.tekton_util.materialize_htpasswd_kubeconfig_login",
            return_value=True,
        ),
        mock.patch("steps.tekton_util.ensure_kubeconfig_bearer_token"),
    ):
        used, source = refresh_working_kubeconfig_from_credentials(
            namespace="ns",
            cluster_source="olminstall-kubeconfig-rh-nightly-pm",
            bootstrap_path=bootstrap,
            work_path=work,
        )
        assert used is True
        assert "tenant Secret" in source
    seed.assert_called_once()


def test_refresh_working_kubeconfig_from_credentials_login_failure(tmp_path: Path) -> None:
    from k8s.external_credentials import ExternalClusterCredentials

    bootstrap = tmp_path / "bootstrap"
    work = tmp_path / "work"
    creds = ExternalClusterCredentials(username="dev", password="bad", api_server="https://api.test:6443")
    with (
        mock.patch(
            "k8s.external_credentials.resolve_external_cluster_credentials",
            return_value=(creds, "tenant Secret 'olminstall-external-rh-nightly-pm-credentials'"),
        ),
        mock.patch("k8s.external_credentials.seed_working_kubeconfig"),
        mock.patch("steps.tekton_util.materialize_htpasswd_kubeconfig_login", return_value=False),
    ):
        with pytest.raises(AppError, match="oc login failed"):
            refresh_working_kubeconfig_from_credentials(
                namespace="ns",
                cluster_source="olminstall-kubeconfig-rh-nightly-pm",
                bootstrap_path=bootstrap,
                work_path=work,
            )


def test_update_external_kubeconfig_secret_applies(tmp_path: Path) -> None:
    kubeconfig = tmp_path / "kubeconfig"
    kubeconfig.write_text("apiVersion: v1\nkind: Config\n", encoding="utf-8")
    create = mock.Mock(returncode=0, stdout="apiVersion: v1\nkind: Secret\n")
    apply = mock.Mock(returncode=0, stdout="", stderr="")
    with mock.patch("k8s.external_credentials.run_cmd", side_effect=[create, apply]) as run_cmd:
        with mock.patch.dict("os.environ", {"KUBECONFIG": "/credentials/kubeconfig"}, clear=False):
            update_external_kubeconfig_secret(
                namespace="rhoai-tenant",
                secret_name="olminstall-kubeconfig-rh-nightly-pm",
                kubeconfig_path=str(kubeconfig),
            )
    assert run_cmd.call_count == 2
    assert "apply" in run_cmd.call_args_list[1].args[0]
    for call in run_cmd.call_args_list:
        assert call.kwargs.get("env", {}).get("KUBECONFIG") is None


def test_update_external_kubeconfig_secret_apply_failure(tmp_path: Path) -> None:
    kubeconfig = tmp_path / "kubeconfig"
    kubeconfig.write_text("apiVersion: v1\nkind: Config\n", encoding="utf-8")
    create = mock.Mock(returncode=0, stdout="apiVersion: v1\nkind: Secret\n")
    apply = mock.Mock(returncode=1, stdout="", stderr="denied")
    with mock.patch("k8s.external_credentials.run_cmd", side_effect=[create, apply]):
        with pytest.raises(AppError, match="Failed to update external kubeconfig Secret"):
            update_external_kubeconfig_secret(
                namespace="rhoai-tenant",
                secret_name="olminstall-kubeconfig-rh-nightly-pm",
                kubeconfig_path=str(kubeconfig),
            )
