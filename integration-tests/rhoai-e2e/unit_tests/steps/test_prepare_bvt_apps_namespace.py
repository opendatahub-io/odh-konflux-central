#!/usr/bin/env python3
"""Unit tests for BVT application-namespace pod reconciliation."""

from __future__ import annotations

import json
import unittest
from unittest.mock import patch

from steps.prepare_bvt_apps_namespace import (  # noqa: E402
    _migration_version_from_job_name,
    delete_finished_job_pods_for_bvt,
    pause_mlflow_operator_reconcile_for_bvt,
    reconcile_stuck_mlflow_migration_pods_for_bvt,
    resume_apps_cronjobs,
    suspend_apps_cronjobs_for_bvt,
)

class PrepareBvtAppsNamespaceTest(unittest.TestCase):
    def test_migration_version_from_job_name(self) -> None:
        self.assertEqual(_migration_version_from_job_name("mlflow-mg-3120-g1"), "3.12.0")

    def test_skips_when_mlflow_not_available(self) -> None:
        with patch("steps.prepare_bvt_apps_namespace._mlflow_deployment_available", return_value=False):
            with patch("steps.prepare_bvt_apps_namespace.oc_run") as oc_run:
                reconcile_stuck_mlflow_migration_pods_for_bvt()
                oc_run.assert_not_called()

    def test_pause_skips_when_no_mlflow_instance(self) -> None:
        with patch("steps.prepare_bvt_apps_namespace._mlflow_cr_exists", return_value=False):
            with patch(
                "steps.prepare_bvt_apps_namespace._mlflow_deployment_available",
                return_value=False,
            ):
                with patch("steps.prepare_bvt_apps_namespace._scale_mlflow_operator") as scale:
                    prior = pause_mlflow_operator_reconcile_for_bvt()
                    self.assertEqual(prior, 0)
                    scale.assert_not_called()

    def test_pause_waits_for_operator_pods_gone(self) -> None:
        with patch("steps.prepare_bvt_apps_namespace._mlflow_cr_exists", return_value=True):
            with patch(
                "steps.prepare_bvt_apps_namespace._mlflow_deployment_available",
                return_value=True,
            ):
                with patch(
                    "steps.prepare_bvt_apps_namespace._mlflow_operator_replicas",
                    return_value=1,
                ):
                    with patch("steps.prepare_bvt_apps_namespace._scale_mlflow_operator") as scale:
                        with patch(
                            "steps.prepare_bvt_apps_namespace._mlflow_operator_pod_names",
                            side_effect=[
                                ["mlflow-operator-controller-manager-abc"],
                                [],
                            ],
                        ):
                            with patch(
                                "steps.prepare_bvt_apps_namespace._quiesce_mlflow_migration_for_bvt"
                            ):
                                with patch(
                                    "steps.prepare_bvt_apps_namespace._mlflow_status_version",
                                    return_value="3.12.0",
                                ):
                                    with patch("steps.prepare_bvt_apps_namespace.time.sleep"):
                                        prior = pause_mlflow_operator_reconcile_for_bvt()
                                        self.assertEqual(prior, 1)
                                        scale.assert_called_once_with(
                                            "redhat-ods-applications", 0
                                        )

    def test_deletes_stuck_migration_pods_jobs_and_patches_status(self) -> None:
        pods = {
            "items": [
                {"metadata": {"name": "mlflow-mg-3120-g1-b6sld"}, "status": {"phase": "Pending"}},
                {"metadata": {"name": "mlflow-abc"}, "status": {"phase": "Running"}},
            ]
        }
        status_version_calls = {"n": 0}

        def _oc_run(args, **kwargs):
            cmd = list(args)
            if cmd[:3] == ["get", "deploy", "mlflow"]:
                return type("R", (), {"returncode": 0, "stdout": "True", "stderr": ""})()
            if cmd[:3] == ["get", "mlflow", "mlflow"]:
                status_version_calls["n"] += 1
                version = "" if status_version_calls["n"] < 4 else "3.12.0"
                return type("R", (), {"returncode": 0, "stdout": version, "stderr": ""})()
            if cmd[:3] == ["get", "pods", "-n"]:
                return type("R", (), {"returncode": 0, "stdout": json.dumps(pods), "stderr": ""})()
            if cmd[:3] == ["get", "jobs", "-n"]:
                return type(
                    "R",
                    (),
                    {"returncode": 0, "stdout": "mlflow-mg-3120-g1\nother-job\n", "stderr": ""},
                )()
            return type("R", (), {"returncode": 0, "stdout": "", "stderr": ""})()

        with patch("steps.prepare_bvt_apps_namespace.time.sleep"):
            with patch("steps.prepare_bvt_apps_namespace.oc_run", side_effect=_oc_run) as oc_run:
                reconcile_stuck_mlflow_migration_pods_for_bvt()
                delete_cmds = [list(c.args[0]) for c in oc_run.call_args_list if c.args[0][0] == "delete"]
                patch_cmds = [list(c.args[0]) for c in oc_run.call_args_list if c.args[0][0] == "patch"]
                self.assertIn(
                    ["delete", "pod", "mlflow-mg-3120-g1-b6sld", "-n", "redhat-ods-applications", "--ignore-not-found"],
                    delete_cmds,
                )
                self.assertIn(
                    ["delete", "job", "mlflow-mg-3120-g1", "-n", "redhat-ods-applications", "--ignore-not-found"],
                    delete_cmds,
                )
                self.assertTrue(any(cmd[1:4] == ["mlflow", "mlflow", "--type=merge"] for cmd in patch_cmds))

    def test_delete_finished_job_pods_skips_running(self) -> None:
        pods = {
            "items": [
                {
                    "metadata": {
                        "name": "maas-api-key-cleanup-1",
                        "ownerReferences": [{"kind": "Job", "name": "maas-api-key-cleanup-1"}],
                    },
                    "status": {"phase": "Succeeded"},
                },
                {
                    "metadata": {
                        "name": "maas-api-8c7f",
                        "ownerReferences": [{"kind": "ReplicaSet", "name": "maas-api-8c7f"}],
                    },
                    "status": {"phase": "Running"},
                },
                {
                    "metadata": {"name": "orphan-succeeded"},
                    "status": {"phase": "Succeeded"},
                },
            ]
        }

        def _oc_run(args, **kwargs):
            cmd = list(args)
            if cmd[:3] == ["get", "pods", "-n"]:
                return type("R", (), {"returncode": 0, "stdout": json.dumps(pods), "stderr": ""})()
            return type("R", (), {"returncode": 0, "stdout": "", "stderr": ""})()

        with patch("steps.prepare_bvt_apps_namespace.oc_run", side_effect=_oc_run) as oc_run:
            deleted = delete_finished_job_pods_for_bvt()
            self.assertEqual(deleted, ["maas-api-key-cleanup-1"])
            delete_cmds = [list(c.args[0]) for c in oc_run.call_args_list if c.args[0][0] == "delete"]
            self.assertEqual(
                delete_cmds,
                [
                    [
                        "delete",
                        "pod",
                        "maas-api-key-cleanup-1",
                        "-n",
                        "redhat-ods-applications",
                        "--ignore-not-found",
                    ]
                ],
            )

    def test_suspend_cronjobs_then_delete_finished_pods(self) -> None:
        pods = {
            "items": [
                {
                    "metadata": {
                        "name": "maas-api-key-cleanup-old",
                        "ownerReferences": [{"kind": "Job"}],
                    },
                    "status": {"phase": "Succeeded"},
                }
            ]
        }
        calls: list[list[str]] = []

        def _oc_run(args, **kwargs):
            cmd = list(args)
            calls.append(cmd)
            if cmd[:3] == ["get", "cronjob", "-n"]:
                return type(
                    "R",
                    (),
                    {"returncode": 0, "stdout": "maas-api-key-cleanup\nalready-off\n", "stderr": ""},
                )()
            if cmd[:3] == ["get", "cronjob", "maas-api-key-cleanup"]:
                return type("R", (), {"returncode": 0, "stdout": "false", "stderr": ""})()
            if cmd[:3] == ["get", "cronjob", "already-off"]:
                return type("R", (), {"returncode": 0, "stdout": "true", "stderr": ""})()
            if cmd[:3] == ["get", "pods", "-n"]:
                return type("R", (), {"returncode": 0, "stdout": json.dumps(pods), "stderr": ""})()
            return type("R", (), {"returncode": 0, "stdout": "", "stderr": ""})()

        with patch("steps.prepare_bvt_apps_namespace.oc_run", side_effect=_oc_run):
            suspended = suspend_apps_cronjobs_for_bvt()
            self.assertEqual(suspended, ["maas-api-key-cleanup"])
            patch_cmds = [c for c in calls if c[:2] == ["patch", "cronjob"]]
            self.assertEqual(len(patch_cmds), 1)
            self.assertEqual(patch_cmds[0][2], "maas-api-key-cleanup")
            self.assertIn('"suspend": true', patch_cmds[0][-1])
            delete_cmds = [c for c in calls if c[0] == "delete"]
            self.assertEqual(delete_cmds[0][2], "maas-api-key-cleanup-old")

        with patch("steps.prepare_bvt_apps_namespace.oc_run") as oc_run:
            resume_apps_cronjobs(["maas-api-key-cleanup"])
            patched = list(oc_run.call_args_list[0].args[0])
            self.assertEqual(patched[2], "maas-api-key-cleanup")
            self.assertIn('"suspend": false', patched[-1])


class _Clock:
    def __init__(self, start: float = 1_000.0) -> None:
        self.t = start

    def time(self) -> float:
        return self.t

    def sleep(self, seconds: float) -> None:
        self.t += seconds


class WaitDashboardPodsReadyForBvtTest(unittest.TestCase):
    def test_waits_until_dashboard_pod_running(self) -> None:
        from steps.prepare_bvt_apps_namespace import wait_dashboard_pods_ready_for_bvt

        pending = {
            "items": [
                {
                    "metadata": {"name": "rhods-dashboard-abc"},
                    "status": {"phase": "Pending"},
                }
            ]
        }
        running = {
            "items": [
                {
                    "metadata": {"name": "rhods-dashboard-abc"},
                    "status": {
                        "phase": "Running",
                        "containerStatuses": [{"ready": True}],
                    },
                }
            ]
        }
        payloads = iter([pending, running])

        def _oc_run(args, **kwargs):
            return type("R", (), {"returncode": 0, "stdout": json.dumps(next(payloads)), "stderr": ""})()

        clock = _Clock()
        with patch("steps.prepare_bvt_apps_namespace.time.time", clock.time):
            with patch("steps.prepare_bvt_apps_namespace.time.sleep", clock.sleep):
                with patch("steps.prepare_bvt_apps_namespace.oc_run", side_effect=_oc_run):
                    wait_dashboard_pods_ready_for_bvt(timeout_sec=60)

    def test_times_out_when_dashboard_stays_pending(self) -> None:
        from steps.prepare_bvt_apps_namespace import wait_dashboard_pods_ready_for_bvt

        pending = {
            "items": [
                {
                    "metadata": {"name": "rhods-dashboard-abc"},
                    "status": {"phase": "Pending"},
                }
            ]
        }

        def _oc_run(args, **kwargs):
            return type("R", (), {"returncode": 0, "stdout": json.dumps(pending), "stderr": ""})()

        clock = _Clock()
        with patch("steps.prepare_bvt_apps_namespace.time.time", clock.time):
            with patch("steps.prepare_bvt_apps_namespace.time.sleep", clock.sleep):
                with patch("steps.prepare_bvt_apps_namespace.oc_run", side_effect=_oc_run):
                    with self.assertRaises(RuntimeError) as ctx:
                        wait_dashboard_pods_ready_for_bvt(timeout_sec=25)
        self.assertIn("rhods-dashboard", str(ctx.exception))
        self.assertIn("Pending", str(ctx.exception))


if __name__ == "__main__":
    raise SystemExit(unittest.main())
