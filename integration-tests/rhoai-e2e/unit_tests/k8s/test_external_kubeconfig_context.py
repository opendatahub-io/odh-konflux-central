"""Tests for external kubeconfig cluster-admin context auto-selection."""

from __future__ import annotations

from pathlib import Path
from unittest import mock

import pytest

from suite.errors import AppError
from k8s.external_kubeconfig import resolve_external_kubeconfig_context

_MULTI_CONTEXT_KC = """\
apiVersion: v1
kind: Config
clusters:
- cluster:
    server: https://api.example:6443
  name: api
contexts:
- context:
    cluster: api
    user: nmanos
  name: /api/nmanos
- context:
    cluster: api
    user: htpasswd-cluster-admin-user/api
  name: default/api/htpasswd-cluster-admin-user
current-context: /api/nmanos
users:
- name: nmanos
  user:
    token: limited
- name: htpasswd-cluster-admin-user/api
  user:
    token: admin
"""


def _write_kc(tmp_path: Path, text: str = _MULTI_CONTEXT_KC) -> Path:
    path = tmp_path / "kubeconfig"
    path.write_text(text, encoding="utf-8")
    return path


def test_resolve_keeps_current_when_cluster_admin(tmp_path: Path) -> None:
    path = _write_kc(tmp_path)
    with mock.patch(
        "k8s.external_kubeconfig._context_has_cluster_admin",
        side_effect=lambda _p, name: name == "/api/nmanos",
    ):
        resolved, ephemeral = resolve_external_kubeconfig_context(path)
    assert resolved == path
    assert ephemeral is False


def test_resolve_switches_to_admin_context(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    path = _write_kc(tmp_path)

    def _admin(_p: Path, name: str) -> bool:
        return name == "default/api/htpasswd-cluster-admin-user"

    with mock.patch("k8s.external_kubeconfig._context_has_cluster_admin", side_effect=_admin):
        resolved, ephemeral = resolve_external_kubeconfig_context(path)
    assert ephemeral is True
    assert resolved != path
    assert "htpasswd-cluster-admin-user" in resolved.read_text(encoding="utf-8")
    out = capsys.readouterr().out
    assert "using context" in out
    assert "htpasswd-cluster-admin-user" in out
    resolved.unlink(missing_ok=True)


def test_resolve_fails_when_no_admin_context(tmp_path: Path) -> None:
    path = _write_kc(tmp_path)
    with mock.patch("k8s.external_kubeconfig._context_has_cluster_admin", return_value=False):
        with pytest.raises(AppError, match="no authenticated cluster-admin context"):
            resolve_external_kubeconfig_context(path)


def test_resolve_honors_preferred_context(tmp_path: Path) -> None:
    path = _write_kc(tmp_path)
    preferred = "default/api/htpasswd-cluster-admin-user"
    with mock.patch(
        "k8s.external_kubeconfig._context_has_cluster_admin",
        side_effect=lambda _p, name: name == preferred,
    ):
        resolved, ephemeral = resolve_external_kubeconfig_context(path, preferred_context=preferred)
    assert ephemeral is True
    assert preferred in resolved.read_text(encoding="utf-8")
    resolved.unlink(missing_ok=True)


def test_resolve_rejects_unknown_preferred_context(tmp_path: Path) -> None:
    path = _write_kc(tmp_path)
    with pytest.raises(AppError, match="not found"):
        resolve_external_kubeconfig_context(path, preferred_context="missing-context")


def test_cli_rejects_context_without_kubeconfig_path() -> None:
    from runners.cli.cli import make_parser, parse_cli_args

    parser = make_parser("test", "")
    with pytest.raises(AppError, match="--external-kubeconfig-context requires --external-kubeconfig"):
        parse_cli_args(parser, ["--external-kubeconfig-context", "admin"])
