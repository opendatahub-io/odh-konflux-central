#!/usr/bin/env python3
"""EPHC and external-cluster MLflow tracking URI patch."""

from __future__ import annotations

import unittest

from components.mlflow.ephc_tracking import (
    mlflow_ephc_incluster_tracking_shell,
    prepend_mlflow_ephc_tracking,
    prepend_mlflow_run_wall_clock,
)
from suite.its_trigger_params import CLUSTER_SOURCE_EPHC


class MlflowEhcTrackingTest(unittest.TestCase):
    def test_shell_exports_force_port_forward(self) -> None:
        shell = mlflow_ephc_incluster_tracking_shell()
        self.assertEqual(shell, "export FORCE_PORT_FORWARD=true")
        self.assertNotIn("CLUSTER_SOURCE", shell)

    def test_injected_script_does_not_expand_unbound_cluster_source(self) -> None:
        shell = mlflow_ephc_incluster_tracking_shell()
        self.assertNotIn("${CLUSTER_SOURCE}", shell)
        self.assertNotIn("${CLUSTER_SOURCE:-}", shell)

    def test_prepend_skipped_without_cluster_source(self) -> None:
        import os

        os.environ.pop("CLUSTER_SOURCE", None)
        cmd = "bash mlflow-tests/images/test-run.sh -m smoke"
        self.assertEqual(prepend_mlflow_ephc_tracking(cmd), cmd)

    def test_prepend_wraps_on_ephc(self) -> None:
        import os

        os.environ["CLUSTER_SOURCE"] = CLUSTER_SOURCE_EPHC
        try:
            out = prepend_mlflow_ephc_tracking("bash mlflow-tests/images/test-run.sh -m smoke")
        finally:
            os.environ.pop("CLUSTER_SOURCE", None)
        self.assertTrue(out.endswith("bash mlflow-tests/images/test-run.sh -m smoke"))
        self.assertIn("FORCE_PORT_FORWARD=true", out)
        self.assertNotIn("CLUSTER_SOURCE", out)

    def test_prepend_wraps_on_external_cluster(self) -> None:
        import os

        os.environ["CLUSTER_SOURCE"] = "olminstall-kubeconfig-rh-nightly-pm"
        try:
            out = prepend_mlflow_ephc_tracking("bash mlflow-tests/images/test-run.sh -m smoke")
        finally:
            os.environ.pop("CLUSTER_SOURCE", None)
        self.assertIn("FORCE_PORT_FORWARD=true", out)
        self.assertTrue(out.endswith("bash mlflow-tests/images/test-run.sh -m smoke"))

    def test_wall_clock_wraps_run_command(self) -> None:
        cmd = "bash mlflow-tests/images/test-run.sh -m smoke"
        out = prepend_mlflow_run_wall_clock(cmd, 900.0)
        self.assertIn("timeout --foreground -s TERM 900s bash -c", out)
        self.assertIn(cmd, out)

    def test_wall_clock_skipped_when_zero(self) -> None:
        cmd = "bash mlflow-tests/images/test-run.sh -m smoke"
        self.assertEqual(prepend_mlflow_run_wall_clock(cmd, 0), cmd)
