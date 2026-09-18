"""Tests for cluster OCP minor detection."""

from __future__ import annotations

import unittest
from unittest.mock import MagicMock, patch

from k8s.cluster_ocp_version import (
    cluster_ocp_minor_from_kubeconfig,
    ocp_minor_from_version_string,
)


class ClusterOcpVersionTests(unittest.TestCase):
    def test_ocp_minor_from_version_string(self) -> None:
        self.assertEqual(ocp_minor_from_version_string("4.21.10"), "4.21")
        self.assertEqual(ocp_minor_from_version_string("4.21"), "4.21")

    @patch("k8s.cluster_ocp_version.run_cmd")
    def test_clusterversion_desired(self, run_cmd: MagicMock) -> None:
        run_cmd.return_value = MagicMock(returncode=0, stdout="4.21.10")
        from pathlib import Path

        with patch.object(Path, "is_file", return_value=True):
            self.assertEqual(
                cluster_ocp_minor_from_kubeconfig(Path("/tmp/kubeconfig")),
                "4.21",
            )
