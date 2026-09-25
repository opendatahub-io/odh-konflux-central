"""BVT cluster node precheck before cluster_health pytest."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from steps import prepare_bvt_cluster_nodes as mod
from steps.prepare_bvt_cluster_nodes import wait_for_schedulable_nodes_for_bvt


class WaitForSchedulableNodesForBvtTest(unittest.TestCase):
    def test_returns_when_all_nodes_schedulable(self) -> None:
        with patch(
            "steps.prepare_bvt_cluster_nodes._unschedulable_node_names",
            side_effect=[["ip-10-0-1-167.us-east-2.compute.internal"], []],
        ), patch("steps.prepare_bvt_cluster_nodes.time.sleep"):
            wait_for_schedulable_nodes_for_bvt(timeout_sec=60, poll_sec=1)

    def test_raises_when_timeout(self) -> None:
        with patch(
            "steps.prepare_bvt_cluster_nodes._unschedulable_node_names",
            return_value=["ip-10-0-1-167.us-east-2.compute.internal"],
        ), patch("steps.prepare_bvt_cluster_nodes.time.sleep"), patch(
            "steps.prepare_bvt_cluster_nodes.time.monotonic",
            side_effect=[0.0, 0.0, 700.0],
        ):
            with self.assertRaisesRegex(RuntimeError, "ip-10-0-1-167"):
                wait_for_schedulable_nodes_for_bvt(timeout_sec=600, poll_sec=1)


class PrepareBvtClusterNodesTest(unittest.TestCase):
    @patch(
        "steps.prepare_bvt_cluster_nodes.wait_for_schedulable_nodes_for_bvt",
        side_effect=RuntimeError("cluster_health precheck timed out waiting for schedulable nodes (120s): node-a"),
    )
    def test_writes_cluster_health_junit_when_precheck_fails(self, _wait: object) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            junit = Path(tmp) / "cluster-health.xml"
            with patch.dict("os.environ", {"ARTIFACTS_DIR": tmp}, clear=False):
                self.assertEqual(mod.prepare_bvt_cluster_nodes(), 1)
            self.assertTrue(junit.is_file())
            self.assertIn("node-a", junit.read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
