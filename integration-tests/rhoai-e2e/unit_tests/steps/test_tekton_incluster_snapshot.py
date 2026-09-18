"""Unit tests for PipelineRun snapshot label/list helpers (oc fallback)."""

from __future__ import annotations

import json
import subprocess
import unittest
from unittest import mock

from steps.tekton_incluster import (
    _list_pipelineruns_for_snapshot_oc,
    _pipeline_run_snapshot_label_oc,
    fetch_snapshot_metadata,
    list_pipelineruns_for_snapshot,
    pipeline_run_snapshot_label,
)


class TektonInclusterSnapshotTest(unittest.TestCase):
    def test_pipeline_run_snapshot_label_oc(self) -> None:
        with mock.patch("steps.tekton_incluster.subprocess.run") as run:
            run.return_value = mock.Mock(returncode=0, stdout="rhoai-snap-1\n", stderr="")
            with mock.patch(
                "steps.tekton_incluster.pipeline_run_name_from_env",
                return_value="e2e-its-abc",
            ):
                with mock.patch(
                    "steps.tekton_incluster.namespace_from_env",
                    return_value="rhoai-tenant",
                ):
                    self.assertEqual(pipeline_run_snapshot_label(), "rhoai-snap-1")

    def test_pipeline_run_snapshot_label_oc_direct(self) -> None:
        with mock.patch("steps.tekton_incluster.subprocess.run") as run:
            run.return_value = mock.Mock(returncode=0, stdout="rhoai-snap-2", stderr="")
            self.assertEqual(
                _pipeline_run_snapshot_label_oc("pr-1", "ns-1"),
                "rhoai-snap-2",
            )
            run.assert_called_once()
            args = run.call_args[0][0]
            self.assertEqual(args[:4], ["oc", "get", "pipelinerun", "pr-1"])

    def test_list_pipelineruns_for_snapshot_oc(self) -> None:
        payload = {
            "items": [
                {
                    "metadata": {
                        "name": "conforma-fbc-xyz",
                        "labels": {"test.appstudio.openshift.io/kind": "enterprise-contract"},
                    }
                }
            ]
        }
        with mock.patch("steps.tekton_incluster.subprocess.run") as run:
            run.return_value = mock.Mock(returncode=0, stdout=json.dumps(payload), stderr="")
            items = _list_pipelineruns_for_snapshot_oc("rhoai-snap-3", "rhoai-tenant")
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["metadata"]["name"], "conforma-fbc-xyz")

    def test_list_pipelineruns_falls_back_to_oc_without_creds(self) -> None:
        payload = {"items": [{"metadata": {"name": "ec-1"}}]}
        with mock.patch(
            "steps.tekton_incluster._pipelinerun_list_credentials",
            return_value=None,
        ):
            with mock.patch("steps.tekton_incluster.subprocess.run") as run:
                run.return_value = mock.Mock(returncode=0, stdout=json.dumps(payload), stderr="")
                items = list_pipelineruns_for_snapshot("snap-4", "rhoai-tenant")
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["metadata"]["name"], "ec-1")


    def test_list_pipelineruns_api_error_oc_success_clears_errors(self) -> None:
        payload = {"items": [{"metadata": {"name": "ec-1"}}]}
        with mock.patch(
            "steps.tekton_incluster._pipelinerun_list_credentials",
            return_value=("token", mock.Mock(), "https://kubernetes.default.svc"),
        ):
            with mock.patch(
                "steps.tekton_incluster.in_cluster_get",
                side_effect=OSError("api down"),
            ):
                with mock.patch("steps.tekton_incluster.subprocess.run") as run:
                    run.return_value = mock.Mock(returncode=0, stdout=json.dumps(payload), stderr="")
                    errors: list[str] = []
                    items = list_pipelineruns_for_snapshot("snap-5", "rhoai-tenant", error_out=errors)
        self.assertEqual(len(items), 1)
        self.assertEqual(errors, [])

    def test_list_pipelineruns_oc_timeout_reports_error(self) -> None:
        with mock.patch(
            "steps.tekton_incluster.subprocess.run",
            side_effect=subprocess.TimeoutExpired(cmd=["oc"], timeout=60),
        ):
            errors: list[str] = []
            items = _list_pipelineruns_for_snapshot_oc("snap-6", "rhoai-tenant", error_out=errors)
        self.assertEqual(items, [])
        self.assertEqual(len(errors), 1)
        self.assertIn("snap-6", errors[0])

    def test_pipeline_run_snapshot_label_oc_timeout_returns_empty(self) -> None:
        with mock.patch(
            "steps.tekton_incluster.subprocess.run",
            side_effect=OSError("oc unavailable"),
        ):
            self.assertEqual(_pipeline_run_snapshot_label_oc("pr-1", "ns-1"), "")

    def test_fetch_snapshot_metadata_oc_timeout_falls_back_to_api(self) -> None:
        api_doc = {
            "metadata": {
                "labels": {"build.appstudio.openshift.io/pipeline": "fbc"},
                "annotations": {"foo": "bar"},
            }
        }
        with mock.patch(
            "steps.tekton_incluster.subprocess.run",
            side_effect=subprocess.TimeoutExpired(cmd=["oc"], timeout=60),
        ):
            with mock.patch(
                "steps.tekton_incluster._pipelinerun_list_credentials",
                return_value=("token", mock.Mock(), "https://kubernetes.default.svc"),
            ):
                with mock.patch(
                    "steps.tekton_incluster.in_cluster_get",
                    return_value=api_doc,
                ) as api_get:
                    labels, annotations = fetch_snapshot_metadata("snap-7", "rhoai-tenant")
        api_get.assert_called_once()
        self.assertEqual(labels["build.appstudio.openshift.io/pipeline"], "fbc")
        self.assertEqual(annotations["foo"], "bar")


if __name__ == "__main__":
    unittest.main()
