"""External kubeconfig preflight helpers (no cluster)."""

from __future__ import annotations

from pathlib import Path
from unittest import mock

import pytest

from suite.errors import AppError
from k8s.external_kubeconfig import (
    default_secret_name,
    external_cluster_has_rhoai_idms,
    verify_external_cluster_login,
    verify_external_cluster_rhoai_idms_mirror,
)

def test_verify_external_cluster_login_ok(tmp_path: Path) -> None:
    kubeconfig = tmp_path / "kubeconfig"
    kubeconfig.write_text("apiVersion: v1\nkind: Config\n", encoding="utf-8")
    with mock.patch("k8s.external_kubeconfig.run_cmd") as run_cmd:
        run_cmd.return_value.returncode = 0
        run_cmd.return_value.stdout = "test-user\n"
        assert verify_external_cluster_login(kubeconfig) == "test-user"

def test_verify_external_cluster_login_anonymous(tmp_path: Path) -> None:
    kubeconfig = tmp_path / "kubeconfig"
    kubeconfig.write_text("apiVersion: v1\nkind: Config\n", encoding="utf-8")
    with (
        mock.patch("k8s.external_kubeconfig.run_cmd") as run_cmd,
        mock.patch(
            "k8s.external_kubeconfig.kubeconfig_cluster_server",
            return_value="https://api.example:6443",
        ),
    ):
        run_cmd.return_value.returncode = 0
        run_cmd.return_value.stdout = "system:anonymous\n"
        with pytest.raises(AppError, match="external cluster https://api.example:6443") as exc_info:
            verify_external_cluster_login(kubeconfig)
        assert exc_info.value.code == 1
        assert "Konflux KUBECONFIG" in str(exc_info.value)

def test_verify_external_cluster_login_missing_file() -> None:
    with pytest.raises(AppError, match="not found"):
        verify_external_cluster_login("/no/such/kubeconfig")

def test_default_secret_name_uses_cluster_label(tmp_path: Path) -> None:
    kubeconfig = tmp_path / "kubeconfig"
    kubeconfig.write_text("apiVersion: v1\nkind: Config\n", encoding="utf-8")
    with mock.patch("k8s.external_kubeconfig.cluster_label_from_kubeconfig", return_value="nmanos-konflux1"):
        assert default_secret_name("nmanos", kubeconfig) == "rhoai-e2e-kubeconfig-nmanos-konflux1-nmanos"

def test_default_secret_name_falls_back_to_run_owner() -> None:
    assert default_secret_name("nmanos") == "rhoai-e2e-kubeconfig-nmanos"

def test_external_cluster_has_rhoai_idms_true(tmp_path: Path) -> None:
    kubeconfig = tmp_path / "kubeconfig"
    kubeconfig.write_text("apiVersion: v1\nkind: Config\n", encoding="utf-8")
    items = {
        "items": [
            {
                "spec": {
                    "imageDigestMirrors": [
                        {"source": "registry.redhat.io/rhoai", "mirrors": ["quay.io/rhoai"]}
                    ]
                }
            }
        ]
    }
    with mock.patch("k8s.external_kubeconfig.run_cmd") as run_cmd:
        run_cmd.return_value.returncode = 0
        run_cmd.return_value.stdout = __import__("json").dumps(items)
        assert external_cluster_has_rhoai_idms(kubeconfig) is True

def test_verify_external_cluster_rhoai_idms_mirror_raises_when_missing(
    tmp_path: Path,
) -> None:
    kubeconfig = tmp_path / "kubeconfig"
    kubeconfig.write_text("apiVersion: v1\nkind: Config\n", encoding="utf-8")
    with (
        mock.patch("k8s.external_kubeconfig.external_cluster_has_rhoai_idms", return_value=False),
        mock.patch("k8s.external_kubeconfig.external_cluster_is_hypershift_managed", return_value=False),
    ):
        with pytest.raises(AppError, match="missing the rhoai IDMS mirror"):
            verify_external_cluster_rhoai_idms_mirror(kubeconfig)

def test_verify_external_cluster_rhoai_idms_mirror_hypershift_ready(
    tmp_path: Path,
) -> None:
    kubeconfig = tmp_path / "kubeconfig"
    kubeconfig.write_text("apiVersion: v1\nkind: Config\n", encoding="utf-8")
    with (
        mock.patch("k8s.external_kubeconfig.external_cluster_has_rhoai_idms", return_value=False),
        mock.patch("k8s.external_kubeconfig.external_cluster_is_hypershift_managed", return_value=True),
        mock.patch("k8s.external_kubeconfig.external_cluster_rosa_hcp_pull_ready", return_value=True),
    ):
        verify_external_cluster_rhoai_idms_mirror(kubeconfig)

def test_verify_external_cluster_rhoai_idms_mirror_hypershift_pending(
    tmp_path: Path,
) -> None:
    kubeconfig = tmp_path / "kubeconfig"
    kubeconfig.write_text("apiVersion: v1\nkind: Config\n", encoding="utf-8")
    with (
        mock.patch("k8s.external_kubeconfig.external_cluster_has_rhoai_idms", return_value=False),
        mock.patch("k8s.external_kubeconfig.external_cluster_is_hypershift_managed", return_value=True),
        mock.patch("k8s.external_kubeconfig.external_cluster_rosa_hcp_pull_ready", return_value=False),
    ):
        verify_external_cluster_rhoai_idms_mirror(kubeconfig)
