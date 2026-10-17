"""Cluster label helpers (Jenkins getClusterNameFromUrl parity)."""

from __future__ import annotations

import tempfile
from pathlib import Path

import pytest

from install.kubeconfig_cluster_label import (
    _cluster_label_from_cluster_source,
    _cluster_label_from_kubeconfig_yaml,
    _sanitize_cluster_label,
    cluster_label_from_kubeconfig,
    cluster_lock_key_from_kubeconfig,
    cluster_name_from_url,
    normalize_api_server_host,
    resolve_cypress_cluster_label,
)

@pytest.mark.parametrize(
    ("url", "expected"),
    [
        (
            "https://api.rmanos-konfluxl-dunx.p3.openshiftapps.com:6443",
            "rmanos-konfluxl-dunx",
        ),
        (
            "https://console-openshift-console.apps.ods-qe-psi-09.osp.rh-ods.com/",
            "ods-qe-psi-09",
        ),
        ("https://api.ods-qe-psi-09.osp.rh-ods.com", "ods-qe-psi-09"),
        ("", ""),
        ("https://example.com/nope", ""),
    ],
)
def test_cluster_name_from_url(url: str, expected: str) -> None:
    assert cluster_name_from_url(url) == expected


@pytest.mark.parametrize(
    ("api_server", "expected"),
    [
        ("https://API.ods-qe-psi-23.osp.rh-ods.com:6443", "api.ods-qe-psi-23.osp.rh-ods.com"),
        ("https://api.rmanos-konfluxl-dunx.p3.openshiftapps.com:6443", "api.rmanos-konfluxl-dunx.p3.openshiftapps.com"),
        ("", ""),
    ],
)
def test_normalize_api_server_host(api_server: str, expected: str) -> None:
    assert normalize_api_server_host(api_server) == expected


def test_cluster_lock_key_from_kubeconfig_yaml() -> None:
    kubeconfig = """\
apiVersion: v1
kind: Config
clusters:
- name: default/api-ods-qe-psi-23-osp-rh-ods-com:6443/kube:admin
  cluster:
    server: https://api.ods-qe-psi-23.osp.rh-ods.com:6443
contexts:
- name: default/api-ods-qe-psi-23-osp-rh-ods-com:6443/kube:admin
  context:
    cluster: default/api-ods-qe-psi-23-osp-rh-ods-com:6443/kube:admin
    user: default/api-ods-qe-psi-23-osp-rh-ods-com:6443/kube:admin
current-context: default/api-ods-qe-psi-23-osp-rh-ods-com:6443/kube:admin
"""
    with tempfile.NamedTemporaryFile(mode="w", suffix=".kubeconfig", delete=False) as tf:
        tf.write(kubeconfig)
        path = Path(tf.name)
    try:
        assert (
            cluster_lock_key_from_kubeconfig(path)
            == "api.ods-qe-psi-23.osp.rh-ods.com"
        )
    finally:
        path.unlink(missing_ok=True)


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        (
            "api-rmanos-konfluxl-dunx-p3-openshiftapps-com:443",
            "rmanos-konfluxl-dunx",
        ),
        (
            "default/api-ods-qe-psi-09-osp-rh-ods-com:6443/kube:admin",
            "ods-qe-psi-09",
        ),
        (
            "https://api.rmanos-konfluxl-dunx.p3.openshiftapps.com:6443",
            "rmanos-konfluxl-dunx",
        ),
    ],
)
def test_sanitize_cluster_label(raw: str, expected: str) -> None:
    assert _sanitize_cluster_label(raw) == expected


def test_cluster_label_from_kubeconfig_yaml_prefers_current_context_cluster() -> None:
    """Use current-context cluster server, not clusters[0] when they differ."""
    kubeconfig = """\
apiVersion: v1
kind: Config
clusters:
- name: wrong-cluster
  cluster:
    server: https://api.wrong-cluster.p3.openshiftapps.com:6443
- name: default/api-ods-qe-psi-23-osp-rh-ods-com:6443/kube:admin
  cluster:
    server: https://api.ods-qe-psi-23.osp.rh-ods.com:6443
contexts:
- name: default/api-ods-qe-psi-23-osp-rh-ods-com:6443/kube:admin
  context:
    cluster: default/api-ods-qe-psi-23-osp-rh-ods-com:6443/kube:admin
    user: default/api-ods-qe-psi-23-osp-rh-ods-com:6443/kube:admin
current-context: default/api-ods-qe-psi-23-osp-rh-ods-com:6443/kube:admin
"""
    with tempfile.NamedTemporaryFile(mode="w", suffix=".kubeconfig", delete=False) as tf:
        tf.write(kubeconfig)
        path = Path(tf.name)
    try:
        assert _cluster_label_from_kubeconfig_yaml(path) == "ods-qe-psi-23"
    finally:
        path.unlink(missing_ok=True)


def test_cluster_label_from_kubeconfig_yaml_api_server() -> None:
    kubeconfig = """\
apiVersion: v1
kind: Config
clusters:
- name: default/api-nmanos-konflux1-p3-openshiftapps-com:6443/kube:admin
  cluster:
    server: https://api.nmanos-konflux1.p3.openshiftapps.com:6443
contexts:
- name: default/api-nmanos-konflux1-p3-openshiftapps-com:6443/kube:admin
  context:
    cluster: default/api-nmanos-konflux1-p3-openshiftapps-com:6443/kube:admin
    user: default/api-nmanos-konflux1-p3-openshiftapps-com:6443/kube:admin
current-context: default/api-nmanos-konflux1-p3-openshiftapps-com:6443/kube:admin
"""
    with tempfile.NamedTemporaryFile(mode="w", suffix=".kubeconfig", delete=False) as tf:
        tf.write(kubeconfig)
        path = Path(tf.name)
    try:
        assert _cluster_label_from_kubeconfig_yaml(path) == "nmanos-konflux1"
    finally:
        path.unlink(missing_ok=True)


def test_resolve_cypress_cluster_label_cluster_source_with_owner_suffix() -> None:
    assert (
        resolve_cypress_cluster_label(
            "/no/such/kubeconfig",
            cluster_source="rhoai-e2e-kubeconfig-nmanos-konflux1-nmanos",
            dashboard_url="https://rh-ai.apps.rosa.nmanos-konflux1.example.com",
        )
        == "nmanos-konflux1"
    )


def test_cluster_label_from_cluster_source_sanitizes_body() -> None:
    assert (
        _cluster_label_from_cluster_source(
            "rhoai-e2e-kubeconfig-default/api-ods-qe-psi-23-osp-rh-ods-com:6443/kube:admin-nmanos"
        )
        == "ods-qe-psi-23"
    )
    assert _cluster_label_from_cluster_source("EPHC") == ""


def test_resolve_cypress_cluster_label_cluster_source_without_dashboard_url() -> None:
    with tempfile.NamedTemporaryFile(mode="w", suffix=".kubeconfig", delete=False) as tf:
        tf.write(
            "clusters:\n- cluster:\n    server: https://api.nmanos-konflux1.p3.openshiftapps.com:6443\n"
        )
        path = Path(tf.name)
    try:
        assert (
            resolve_cypress_cluster_label(
                path,
                cluster_source="rhoai-e2e-kubeconfig-nmanos-konflux1-nmanos",
            )
            == "nmanos-konflux1"
        )
    finally:
        path.unlink(missing_ok=True)
