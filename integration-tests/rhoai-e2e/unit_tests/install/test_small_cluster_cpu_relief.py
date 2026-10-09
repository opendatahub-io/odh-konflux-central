"""Tests for small-cluster CPU relief during DSC wait."""

from __future__ import annotations

import unittest
from unittest import mock

from install import small_cluster_cpu_relief


class TestSmallClusterCpuRelief(unittest.TestCase):
    def setUp(self) -> None:
        small_cluster_cpu_relief._relief_applied = False

    @mock.patch.dict(
        "os.environ",
        {"CLUSTER_SOURCE": "rhoai-e2e-kubeconfig-rh-nightly-pm-nmanos"},
        clear=False,
    )
    @mock.patch.object(small_cluster_cpu_relief, "oc_run")
    def test_proactive_scales_operator(self, oc_run: mock.Mock) -> None:
        oc_run.side_effect = [
            mock.Mock(returncode=0, stdout="3"),
            mock.Mock(returncode=0, stdout=""),
            mock.Mock(returncode=0, stdout="2"),
            mock.Mock(returncode=0, stdout=""),
            mock.Mock(returncode=0, stdout="2"),
            mock.Mock(returncode=0, stdout=""),
        ]
        small_cluster_cpu_relief.maybe_small_cluster_install_prep()
        self.assertTrue(small_cluster_cpu_relief._relief_applied)
        scale_calls = [c for c in oc_run.call_args_list if c[0][0][:2] == ["scale", "deployment"]]
        self.assertGreaterEqual(len(scale_calls), 1)

    @mock.patch.dict("os.environ", {}, clear=True)
    @mock.patch.object(small_cluster_cpu_relief, "oc_run")
    def test_skipped_without_cluster_marker(self, oc_run: mock.Mock) -> None:
        small_cluster_cpu_relief.maybe_small_cluster_install_prep()
        oc_run.assert_not_called()


if __name__ == "__main__":
    unittest.main()
