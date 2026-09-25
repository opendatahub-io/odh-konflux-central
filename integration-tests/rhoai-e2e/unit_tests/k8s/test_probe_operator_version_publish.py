"""Tests for operator version probe/publish helpers."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest import mock

from k8s.probe_operator_version import (
    publish_operator_version_results,
    resolve_display_operator_version,
    resolve_operator_version,
)


class ResolveDisplayOperatorVersionTest(unittest.TestCase):
    def test_prefers_installed_csv(self) -> None:
        op, rhoai = resolve_display_operator_version(
            installed_version="3.5.0-ea.2",
            rhoai_version_param="unspecified (default)",
            fbcf_image="quay.io/rhoai/rhoai-fbc-fragment@sha256:abc",
        )
        self.assertEqual(op, "3.5.0-ea.2")
        self.assertEqual(rhoai, "3.5.0-ea.2")

    def test_falls_back_to_catalog_param_and_image(self) -> None:
        image = (
            "quay.io/rhoai/rhoai-fbc-fragment:"
            "ocp-4.21-rhoai-3.5-ea.2-64185b995fe93f3ae9d014f1124714ba96b5"
        )
        op, rhoai = resolve_display_operator_version(
            installed_version="",
            rhoai_version_param="",
            fbcf_image=image,
        )
        self.assertEqual(op, "3.5-ea.2")
        self.assertEqual(rhoai, "3.5-ea.2")


class PublishOperatorVersionResultsTest(unittest.TestCase):
    @mock.patch("k8s.probe_operator_version.write_result")
    def test_writes_tekton_results(self, write_mock: mock.MagicMock) -> None:
        with mock.patch.dict(
            "os.environ",
            {
                "OPERATOR_VERSION_PATH": "/tekton/results/operator-version",
                "RHOAI_VERSION_PATH": "/tekton/results/rhoai-version",
            },
            clear=False,
        ):
            resolved = publish_operator_version_results(
                installed_version="2.13.0-rc.2",
                patch_pipelinerun=False,
            )
        self.assertEqual(resolved, "2.13.0-rc.2")
        write_mock.assert_any_call("/tekton/results/operator-version", "2.13.0-rc.2")
        write_mock.assert_any_call("/tekton/results/rhoai-version", "2.13.0-rc.2")


class ResolveOperatorVersionTaskOrderTest(unittest.TestCase):
    def test_reads_verify_operator_ready_before_cluster_probe(self) -> None:
        taskruns = [
            {
                "metadata": {"labels": {"tekton.dev/pipelineTask": "verify-operator-ready"}},
                "status": {"results": [{"name": "OPERATOR_VERSION", "value": "3.5.0-ea.2"}]},
            }
        ]
        ver = resolve_operator_version(
            taskruns,
            external_kubeconfig_secret="rhoai-e2e-kubeconfig-test",
            operator_namespace="redhat-ods-operator",
            poll_collect_diagnostics=False,
        )
        self.assertEqual(ver, "3.5.0-ea.2")


if __name__ == "__main__":
    unittest.main()
