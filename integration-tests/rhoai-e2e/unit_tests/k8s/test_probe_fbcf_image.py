"""Unit tests for cluster FBC catalog image resolution."""

from __future__ import annotations

import json
from unittest.mock import patch

from k8s import probe_fbcf_image as pfi

def test_resolve_fbcf_image_uses_snapshot_for_install_path() -> None:
    taskruns: list = []
    image = "quay.io/example/catalog@sha256:abc"
    assert (
        pfi.resolve_fbcf_image(
            taskruns,
            extract_task_result=image,
            product="rhoai",
        )
        == image
    )

def test_resolve_fbcf_image_probes_cluster_for_existing() -> None:
    taskruns: list = []
    sub_json = json.dumps({"spec": {"source": "rhoai-catalog-dev", "sourceNamespace": "openshift-marketplace"}})
    with patch.object(pfi, "_probe_with_kubeconfig_file", return_value="quay.io/rhoai/catalog:v1") as mock_probe:
        out = pfi.resolve_fbcf_image(
            taskruns,
            extract_task_result="n/a",
            product="",
            tests_shared_kubeconfig="/tmp/kubeconfig",
            operator_namespace="redhat-ods-operator",
            operator_name="rhods-operator",
        )
    assert out == "quay.io/rhoai/catalog:v1"
    mock_probe.assert_called_once()

def test_resolve_fbcf_image_existing_falls_back_to_na() -> None:
    with patch.object(pfi, "_probe_with_kubeconfig_file", return_value=""):
        assert (
            pfi.resolve_fbcf_image(
                [],
                extract_task_result="n/a",
                product="",
            )
            == "n/a"
        )

def test_catalog_image_from_subscription_reads_catalogsource() -> None:
    sub_json = json.dumps({"spec": {"source": "my-catalog"}})
    with patch("install.install_and_verify.oc_run") as mock_oc:
        mock_oc.side_effect = [
            type("R", (), {"returncode": 0, "stdout": sub_json})(),
            type("R", (), {"returncode": 0, "stdout": "quay.io/cat:latest"})(),
        ]
        assert pfi._catalog_image_from_subscription("redhat-ods-operator", "rhods-operator") == "quay.io/cat:latest"
