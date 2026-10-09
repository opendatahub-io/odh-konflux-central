"""Tests for cluster label resolution in build_runtime_metadata."""

from __future__ import annotations

import unittest
from unittest.mock import patch

from runners.report.pipelinerun_metadata import (
    build_runtime_metadata,
    cluster_label_from_cluster_source,
    resolve_runtime_openshift_version,
)
from suite.constants import ANNOTATION_CLUSTER, DEFAULT_APP, DEFAULT_NAMESPACE

class ClusterLabelFromSourceTest(unittest.TestCase):
    def test_external_secret_strips_prefix(self) -> None:
        self.assertEqual(
            cluster_label_from_cluster_source("rhoai-e2e-kubeconfig-nmanos-konflux1"),
            "nmanos-konflux1",
        )

    def test_ephc_returns_empty(self) -> None:
        self.assertEqual(cluster_label_from_cluster_source("EPHC"), "")

class BuildRuntimeMetadataClusterTest(unittest.TestCase):
    @patch("runners.report.pipelinerun_metadata.resolve_artifacts_url_for_ui", return_value="")
    @patch("runners.report.pipelinerun_metadata.resolve_operator_version", return_value="")
    def test_taskrun_cluster_name_wins(self, _op, _art) -> None:
        prj = {
            "metadata": {"annotations": {}, "labels": {}},
            "spec": {"params": [{"name": "CLUSTER_SOURCE", "value": "rhoai-e2e-kubeconfig-x"}]},
        }
        taskruns = [
            {
                "metadata": {"labels": {"tekton.dev/pipelineTask": "external-cluster-ready"}},
                "status": {"results": [{"name": "clusterName", "value": "ods-qe-psi-07"}]},
            }
        ]
        ann, _labels = build_runtime_metadata(
            pipeline_run="pr-1",
            namespace=DEFAULT_NAMESPACE,
            tests_csv="bvt,smoke",
            prj=prj,
            taskruns=taskruns,
        )
        self.assertEqual(ann.get(ANNOTATION_CLUSTER), "ods-qe-psi-07")

    def test_resolve_openshift_version_from_external_cluster_ready(self) -> None:
        prj = {"spec": {"params": [{"name": "OCP_VERSION", "value": "4.20"}]}}
        taskruns = [
            {
                "metadata": {"labels": {"tekton.dev/pipelineTask": "external-cluster-ready"}},
                "status": {
                    "results": [
                        {"name": "OPENSHIFT_VERSION", "value": "4.22"},
                        {"name": "clusterName", "value": "konflux-rhoai1"},
                    ]
                },
            }
        ]
        self.assertEqual(
            resolve_runtime_openshift_version(prj, taskruns),
            "4.22",
        )

    @patch("runners.report.pipelinerun_metadata.resolve_artifacts_url_for_ui", return_value="")
    @patch("runners.report.pipelinerun_metadata.resolve_operator_version", return_value="")
    def test_cluster_source_fallback_when_no_taskrun(self, _op, _art) -> None:
        prj = {
            "metadata": {"annotations": {}, "labels": {}},
            "spec": {
                "params": [
                    {"name": "CLUSTER_SOURCE", "value": "rhoai-e2e-kubeconfig-nmanos-konflux1"},
                ]
            },
        }
        ann, _labels = build_runtime_metadata(
            pipeline_run="pr-1",
            namespace=DEFAULT_NAMESPACE,
            tests_csv="bvt,smoke",
            prj=prj,
            taskruns=[],
        )
        self.assertEqual(ann.get(ANNOTATION_CLUSTER), "nmanos-konflux1")

if __name__ == "__main__":
    raise SystemExit(unittest.main())
