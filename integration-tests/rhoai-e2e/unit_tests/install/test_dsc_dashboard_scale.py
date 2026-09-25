"""Tests for rhods-dashboard scale-down during DSC wait on small clusters."""

from __future__ import annotations

import unittest
from unittest import mock

from install import dsc_install


class TestRhodsDashboardScaleDown(unittest.TestCase):
    def setUp(self) -> None:
        dsc_install._rhods_dashboard_scale_down_attempted = False

    @mock.patch.dict("os.environ", {}, clear=True)
    @mock.patch.object(dsc_install, "oc_run")
    def test_scales_when_dashboard_pending_insufficient_cpu(self, oc_run: mock.Mock) -> None:
        oc_run.side_effect = [
            mock.Mock(
                returncode=0,
                stdout=(
                    '{"items":[{"metadata":{"name":"rhods-dashboard-abc"},'
                    '"status":{"conditions":[{"type":"PodScheduled","status":"False",'
                    '"message":"0/2 nodes are available: 2 Insufficient cpu."}]}}]}'
                ),
            ),
            mock.Mock(returncode=0, stdout="2,"),
            mock.Mock(returncode=0, stdout=""),
        ]
        dsc_install._maybe_scale_rhods_dashboard_for_cpu_pressure()
        scale_call = oc_run.call_args_list[-1]
        self.assertEqual(scale_call[0][0][:2], ["scale", "deployment"])
        self.assertTrue(dsc_install._rhods_dashboard_scale_down_attempted)

    @mock.patch.dict("os.environ", {}, clear=True)
    @mock.patch.object(dsc_install, "oc_run")
    def test_scales_when_unavailable_replicas_omitted(self, oc_run: mock.Mock) -> None:
        """readyReplicas/unavailableReplicas absent in status must not skip scale-down."""
        oc_run.side_effect = [
            mock.Mock(
                returncode=0,
                stdout=(
                    '{"items":[{"metadata":{"name":"rhods-dashboard-abc"},'
                    '"status":{"conditions":[{"type":"PodScheduled","status":"False",'
                    '"message":"0/2 nodes are available: 2 Insufficient cpu."}]}}]}'
                ),
            ),
            mock.Mock(returncode=0, stdout="2,0"),
            mock.Mock(returncode=0, stdout=""),
        ]
        dsc_install._maybe_scale_rhods_dashboard_for_cpu_pressure()
        self.assertTrue(dsc_install._rhods_dashboard_scale_down_attempted)

    @mock.patch.dict("os.environ", {"RHOAI_E2E_SKIP_DASHBOARD_SCALE_DOWN": "1"})
    @mock.patch.object(dsc_install, "oc_run")
    def test_skipped_when_env_set(self, oc_run: mock.Mock) -> None:
        dsc_install._maybe_scale_rhods_dashboard_for_cpu_pressure()
        oc_run.assert_not_called()


if __name__ == "__main__":
    unittest.main()
