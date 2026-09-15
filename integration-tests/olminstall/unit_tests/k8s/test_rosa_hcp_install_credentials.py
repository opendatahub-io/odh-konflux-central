"""Tests for ROSA HCP install-data S3 rosa-admin fallback."""

from __future__ import annotations

import io
import zipfile
from pathlib import Path
from unittest import mock

from k8s.external_credentials import ExternalClusterCredentials
from k8s.rosa_hcp_install_credentials import (
    _extract_zip_member,
    _s3_download_bytes,
    load_rosa_admin_credentials_from_install_zip,
    resolve_install_cluster_name,
)


def _bootstrap_kubeconfig(path: Path, server: str) -> None:
    path.write_text(
        "\n".join(
            [
                "apiVersion: v1",
                "kind: Config",
                "clusters:",
                "  - name: c",
                "    cluster:",
                f"      server: {server}",
                "contexts:",
                "  - name: ctx",
                "    context:",
                "      cluster: c",
                "      user: u",
                "current-context: ctx",
                "users:",
                "  - name: u",
                "    user: {}",
            ]
        )
        + "\n",
        encoding="utf-8",
    )


def test_resolve_install_cluster_name_from_api_url(tmp_path: Path) -> None:
    bootstrap = tmp_path / "kubeconfig"
    _bootstrap_kubeconfig(
        bootstrap,
        "https://api.nmanos-test.8xbm.s3.devshift.org:443",
    )
    assert resolve_install_cluster_name(bootstrap) == "nmanos-test"


def test_extract_zip_member_reads_rosa_admin_password() -> None:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as archive:
        archive.writestr("auth/rosa-admin-password", "secret-pass\n")
    assert _extract_zip_member(buf.getvalue(), "auth/rosa-admin-password") == "secret-pass"


def test_extract_zip_member_returns_empty_for_corrupt_archive() -> None:
    assert _extract_zip_member(b"not-a-zip", "auth/rosa-admin-password") == ""


def test_s3_download_bytes_forwards_session_token() -> None:
    captured: dict[str, object] = {}

    class FakeClient:
        def get_object(self, **kwargs):
            return {"Body": io.BytesIO(b"payload")}

    def fake_client(service_name, **kwargs):
        captured.update(kwargs)
        return FakeClient()

    with (
        mock.patch("k8s.rosa_hcp_install_credentials._ensure_boto3"),
        mock.patch("boto3.client", side_effect=fake_client),
    ):
        assert (
            _s3_download_bytes(
                "hcp-clusters-mdata",
                "openshift-cli-installer/nmanos-test.zip",
                {
                    "AWS_ACCESS_KEY_ID": "AKIA_TEST",
                    "AWS_SECRET_ACCESS_KEY": "secret",
                    "AWS_SESSION_TOKEN": "session-token",
                },
            )
            == b"payload"
        )
    assert captured["aws_session_token"] == "session-token"


def test_s3_download_bytes_returns_empty_without_raising_when_boto3_unavailable() -> None:
    with mock.patch(
        "k8s.rosa_hcp_install_credentials._ensure_boto3",
        side_effect=RuntimeError("pip install boto3 failed"),
    ):
        assert (
            _s3_download_bytes(
                "hcp-clusters-mdata",
                "openshift-cli-installer/nmanos-test.zip",
                {
                    "AWS_ACCESS_KEY_ID": "AKIA_TEST",
                    "AWS_SECRET_ACCESS_KEY": "secret",
                },
            )
            == b""
        )


def test_load_rosa_admin_credentials_from_install_zip(tmp_path: Path) -> None:
    bootstrap = tmp_path / "kubeconfig"
    _bootstrap_kubeconfig(
        bootstrap,
        "https://api.nmanos-test.8xbm.s3.devshift.org:443",
    )
    zip_buf = io.BytesIO()
    with zipfile.ZipFile(zip_buf, "w") as archive:
        archive.writestr("auth/rosa-admin-password", "rosa-secret\n")

    with (
        mock.patch(
            "k8s.rosa_hcp_install_credentials.load_hcp_install_aws_credentials",
            return_value={
                "AWS_ACCESS_KEY_ID": "AKIA_TEST",
                "AWS_SECRET_ACCESS_KEY": "secret",
            },
        ),
        mock.patch(
            "k8s.rosa_hcp_install_credentials._s3_download_bytes",
            return_value=zip_buf.getvalue(),
        ),
    ):
        creds = load_rosa_admin_credentials_from_install_zip(bootstrap_path=bootstrap)

    assert creds == ExternalClusterCredentials(
        username="rosa-admin",
        password="rosa-secret",
        api_server="https://api.nmanos-test.8xbm.s3.devshift.org:443",
    )
