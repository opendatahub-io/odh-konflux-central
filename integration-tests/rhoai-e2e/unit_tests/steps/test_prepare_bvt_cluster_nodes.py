"""BVT cluster node precheck before cluster_health pytest."""

from __future__ import annotations

import unittest
from unittest.mock import patch

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


if __name__ == "__main__":
    unittest.main()
