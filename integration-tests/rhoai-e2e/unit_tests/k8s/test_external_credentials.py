"""Tests for Konflux external-cluster htpasswd credentials (RHOAIENG-57718)."""

from __future__ import annotations

from suite.constants import DEFAULT_APP, DEFAULT_NAMESPACE
import json
from pathlib import Path
from unittest import mock

import pytest

from k8s.external_credentials import (
    ExternalClusterCredentials,
    companion_kubeconfig_secret_for_install_cluster,
    external_credentials_secret_name,
    load_external_cluster_credentials,
    refresh_working_kubeconfig_from_credentials,
    resolve_external_cluster_credentials,
    resolve_external_login_source,
    seed_working_kubeconfig,
    update_external_kubeconfig_secret,
    write_minimal_kubeconfig,
)
from suite.errors import AppError


def test_external_credentials_secret_name_maps_kubeconfig_secret() -> None:
    assert (
        external_credentials_secret_name("rhoai-e2e-kubeconfig-rh-nightly-pm")
        == "rhoai-e2e-external-rh-nightly-pm-credentials"
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
        creds = load_external_cluster_credentials(namespace=DEFAULT_NAMESPACE, secret_name="rhoai-e2e-external-x-credentials")
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


def test_refresh_s3_install_uses_companion_kubeconfig_secret(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    work = tmp_path / "kubeconfig"
    kube_yaml = "\n".join(
        [
            "apiVersion: v1",
            "kind: Config",
            "clusters:",
            "  - name: c",
            "    cluster:",
            "      server: https://api.nmanos-ocp422.example:6443",
            "contexts:",
            "  - name: ctx",
            "    context:",
            "      cluster: c",
            "      user: u",
            "current-context: ctx",
            "users:",
            "  - name: u",
            "    user:",
            "      token: valid",
        ]
    ) + "\n"
    with (
        mock.patch.dict("os.environ", {"EXTERNAL_LOGIN_SOURCE": "tenant"}, clear=False),
        mock.patch(
            "k8s.external_credentials.load_kubeconfig_from_tenant_secret",
            return_value=kube_yaml,
        ),
        mock.patch(
            "k8s.external_kubeconfig.verify_external_cluster_login",
            return_value="cluster-admin",
        ),
        mock.patch(
            "k8s.external_credentials.resolve_external_cluster_credentials",
        ) as resolve,
    ):
        used, source = refresh_working_kubeconfig_from_credentials(
            namespace=DEFAULT_NAMESPACE,
            cluster_source="rhoai-e2e-s3-nmanos-ocp422",
            bootstrap_path=tmp_path / "missing" / "kubeconfig",
            work_path=work,
        )
    assert used is True
    assert "companion tenant kubeconfig Secret" in source
    assert work.read_text(encoding="utf-8") == kube_yaml
    resolve.assert_not_called()


def test_resolve_external_login_source_defaults_to_vault() -> None:
    assert resolve_external_login_source({}) == "vault"
    assert resolve_external_login_source({"EXTERNAL_LOGIN_SOURCE": "tenant"}) == "tenant"
    assert resolve_external_login_source({"EXTERNAL_LOGIN_SOURCE": "konflux"}) == "tenant"


def test_resolve_external_cluster_credentials_vault_prefers_s3(tmp_path: Path) -> None:
    s3_creds = ExternalClusterCredentials(
        username="rosa-admin",
        password="from-s3",
        api_server="https://api.test:6443",
    )
    tenant_creds = ExternalClusterCredentials(
        username="dev",
        password="secret",
        api_server="https://api.test:6443",
    )
    with (
        mock.patch(
            "k8s.external_credentials._rosa_admin_credentials_from_install_zip",
            return_value=(s3_creds, "ROSA HCP install-data S3 (rosa-admin)"),
        ) as s3_mock,
        mock.patch(
            "k8s.external_credentials._tenant_htpasswd_credentials",
            return_value=(tenant_creds, "tenant Secret 'x'"),
        ) as tenant_mock,
    ):
        creds, source = resolve_external_cluster_credentials(
            namespace=DEFAULT_NAMESPACE,
            cluster_source="rhoai-e2e-kubeconfig-rh-nightly-pm",
            bootstrap_path=tmp_path / "bootstrap",
        )
    assert creds == s3_creds
    assert "S3" in source
    s3_mock.assert_called_once()
    tenant_mock.assert_not_called()


def test_resolve_external_cluster_credentials_tenant_prefers_htpasswd(tmp_path: Path) -> None:
    s3_creds = ExternalClusterCredentials(
        username="rosa-admin",
        password="from-s3",
        api_server="https://api.test:6443",
    )
    tenant_creds = ExternalClusterCredentials(
        username="dev",
        password="secret",
        api_server="https://api.test:6443",
    )
    with (
        mock.patch.dict("os.environ", {"EXTERNAL_LOGIN_SOURCE": "tenant"}, clear=False),
        mock.patch(
            "k8s.external_credentials._rosa_admin_credentials_from_install_zip",
            return_value=(s3_creds, "ROSA HCP install-data S3 (rosa-admin)"),
        ),
        mock.patch(
            "k8s.external_credentials._tenant_htpasswd_credentials",
            return_value=(tenant_creds, "tenant Secret 'rhoai-e2e-external-rh-nightly-pm-credentials'"),
        ) as tenant_mock,
    ):
        creds, source = resolve_external_cluster_credentials(
            namespace=DEFAULT_NAMESPACE,
            cluster_source="rhoai-e2e-kubeconfig-rh-nightly-pm",
            bootstrap_path=tmp_path / "bootstrap",
        )
    assert creds == tenant_creds
    assert "tenant Secret" in source
    tenant_mock.assert_called_once()


def test_resolve_external_cluster_credentials_vault_falls_back_with_override(tmp_path: Path) -> None:
    tenant_creds = ExternalClusterCredentials(
        username="dev",
        password="secret",
        api_server="https://api.test:6443",
    )
    with (
        mock.patch.dict(
            "os.environ",
            {"EXTERNAL_CREDENTIALS_SECRET": "rhoai-e2e-external-rh-nightly-pm-credentials"},
            clear=False,
        ),
        mock.patch(
            "k8s.external_credentials._rosa_admin_credentials_from_install_zip",
            return_value=(None, ""),
        ),
        mock.patch(
            "k8s.external_credentials._tenant_htpasswd_credentials",
            return_value=(tenant_creds, "tenant Secret 'rhoai-e2e-external-rh-nightly-pm-credentials'"),
        ) as tenant_mock,
    ):
        creds, source = resolve_external_cluster_credentials(
            namespace=DEFAULT_NAMESPACE,
            cluster_source="rhoai-e2e-kubeconfig-rh-nightly-pm",
            bootstrap_path=tmp_path / "bootstrap",
        )
    assert creds == tenant_creds
    tenant_mock.assert_called_once()


def test_companion_kubeconfig_secret_for_install_cluster() -> None:
    assert (
        companion_kubeconfig_secret_for_install_cluster("nmanos-ocp422")
        == "rhoai-e2e-kubeconfig-nmanos-ocp422"
    )


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
            cluster_source="rhoai-e2e-kubeconfig-rh-nightly-pm",
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
            return_value=(creds, "tenant Secret 'rhoai-e2e-external-rh-nightly-pm-credentials'"),
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
            cluster_source="rhoai-e2e-kubeconfig-rh-nightly-pm",
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
            return_value=(creds, "tenant Secret 'rhoai-e2e-external-rh-nightly-pm-credentials'"),
        ),
        mock.patch("k8s.external_credentials.seed_working_kubeconfig"),
        mock.patch("steps.tekton_util.materialize_htpasswd_kubeconfig_login", return_value=False),
    ):
        with pytest.raises(AppError, match="oc login failed"):
            refresh_working_kubeconfig_from_credentials(
                namespace="ns",
                cluster_source="rhoai-e2e-kubeconfig-rh-nightly-pm",
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
                namespace=DEFAULT_NAMESPACE,
                secret_name="rhoai-e2e-kubeconfig-rh-nightly-pm",
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
                namespace=DEFAULT_NAMESPACE,
                secret_name="rhoai-e2e-kubeconfig-rh-nightly-pm",
                kubeconfig_path=str(kubeconfig),
            )
