"""Tests for in-pipeline external cluster idle step."""

from __future__ import annotations

import unittest
from unittest import mock

from install import assert_external_cluster_idle as step


class AssertExternalClusterIdleStepTest(unittest.TestCase):
    def test_skips_non_external_cluster_source(self) -> None:
        with mock.patch.dict("os.environ", {"CLUSTER_SOURCE": "EPHC"}, clear=False):
            self.assertEqual(step.main(), 0)

    def test_fails_when_wait_raises(self) -> None:
        env = {
            "CLUSTER_SOURCE": "rhoai-e2e-kubeconfig-rh-nightly-pm",
            "PIPELINE_RUN_NAME": "pr-self",
        }
        with mock.patch.dict("os.environ", env, clear=False):
            with mock.patch.object(step, "namespace_from_env", return_value="rhoai-tenant"):
                with mock.patch.object(step, "pipeline_run_name_from_env", return_value="pr-self"):
                    with mock.patch.object(
                        step,
                        "wait_for_external_cluster_idle",
                        side_effect=step.AppError("timed out", 1),
                    ):
                        self.assertEqual(step.main(), 1)

    def test_ok_when_idle(self) -> None:
        env = {
            "CLUSTER_SOURCE": "rhoai-e2e-kubeconfig-rh-nightly-pm",
            "PIPELINE_RUN_NAME": "pr-self",
        }
        with mock.patch.dict("os.environ", env, clear=False):
            with mock.patch.object(step, "namespace_from_env", return_value="rhoai-tenant"):
                with mock.patch.object(step, "pipeline_run_name_from_env", return_value="pr-self"):
                    with mock.patch.object(step, "wait_for_external_cluster_idle"):
                        self.assertEqual(step.main(), 0)
