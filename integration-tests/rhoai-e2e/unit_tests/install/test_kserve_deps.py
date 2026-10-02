#!/usr/bin/env python3
"""Unit tests for KServe dependency gating (no cluster)."""

from __future__ import annotations

import unittest
from unittest.mock import patch

from install.kserve_deps import (
    components_csv_requires_kserve_deps,
    ensure_serverless_operator,
)

class KserveDepsTest(unittest.TestCase):
    def test_model_server_uses_raw_deployment_not_serverless(self) -> None:
        self.assertFalse(components_csv_requires_kserve_deps("model_server"))

    def test_workbenches_only_does_not_require_kserve_deps(self) -> None:
        self.assertFalse(components_csv_requires_kserve_deps("workbenches"))

    def test_dashboard_cypress_requires_kserve_deps(self) -> None:
        self.assertTrue(components_csv_requires_kserve_deps("dashboard_cypress"))

    @patch("install.kserve_deps.serverless_operator_ready", return_value=True)
    def test_ensure_serverless_skips_when_ready(self, _ready) -> None:
        ensure_serverless_operator(timeout_sec=60)

    @patch("install.kserve_deps._wait_for_serverless_csv", return_value=True)
    @patch("install.kserve_deps._approve_serverless_installplans", return_value=0)
    @patch("install.kserve_deps.oc_run")
    @patch("install.kserve_deps.serverless_operator_ready", return_value=False)
    def test_ensure_serverless_applies_subscription(
        self, _ready, oc_run, _approve, _wait
    ) -> None:
        oc_run.side_effect = [
            type("R", (), {"returncode": 1, "stdout": "", "stderr": ""})(),
            type("R", (), {"returncode": 0, "stdout": "", "stderr": ""})(),
        ]
        ensure_serverless_operator(timeout_sec=60)
        self.assertEqual(oc_run.call_count, 2)
        _wait.assert_called_once()

    @patch("install.kserve_deps._wait_for_serverless_csv", return_value=True)
    @patch("install.kserve_deps._approve_serverless_installplans", return_value=1)
    @patch("install.kserve_deps.oc_run")
    @patch("install.kserve_deps.serverless_operator_ready", return_value=False)
    def test_ensure_serverless_approves_installplan_after_subscribe(
        self, _ready, oc_run, approve, _wait
    ) -> None:
        oc_run.side_effect = [
            type("R", (), {"returncode": 1, "stdout": "", "stderr": ""})(),
            type("R", (), {"returncode": 0, "stdout": "", "stderr": ""})(),
        ]
        ensure_serverless_operator(timeout_sec=60)
        approve.assert_called_once()
        _wait.assert_called_once()

    @patch("install.kserve_deps.time.sleep")
    @patch("install.kserve_deps._approve_serverless_installplans", return_value=0)
    @patch("install.kserve_deps._named_csv_succeeded_version", return_value="1.37.1")
    @patch(
        "install.kserve_deps._subscription_target_csv",
        return_value="serverless-operator.v1.37.1",
    )
    def test_wait_for_serverless_csv_uses_subscription_namespace(
        self, _target, _ver, _approve, _sleep
    ) -> None:
        from install.kserve_deps import _wait_for_serverless_csv

        self.assertTrue(_wait_for_serverless_csv(timeout_sec=60))
        _target.assert_called_with("openshift-operators", "serverless-operator")
        _ver.assert_called_with("openshift-operators", "serverless-operator.v1.37.1")

    @patch("install.kserve_deps.time.sleep")
    @patch("install.kserve_deps._approve_serverless_installplans", return_value=0)
    @patch("install.kserve_deps._named_csv_succeeded_version", side_effect=[None, "1.37.1"])
    @patch("install.kserve_deps._named_csv_phase", return_value=(None, None))
    @patch(
        "install.kserve_deps._subscription_target_csv",
        return_value="serverless-operator.v1.37.1",
    )
    def test_wait_for_serverless_csv_waits_when_subscription_csv_missing(
        self, _target, _phase, _ver, _approve, _sleep
    ) -> None:
        from install.kserve_deps import _wait_for_serverless_csv

        self.assertTrue(_wait_for_serverless_csv(timeout_sec=60))
        self.assertGreaterEqual(_ver.call_count, 2)
        _ver.assert_any_call("openshift-operators", "serverless-operator.v1.37.1")

if __name__ == "__main__":
    raise SystemExit(unittest.main())
