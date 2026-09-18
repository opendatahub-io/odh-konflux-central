"""Tests for shared external kubeconfig Secret cleanup guards."""

from __future__ import annotations

import unittest
from unittest import mock

from k8s.external_kubeconfig import (
    _pipelinerun_cluster_source_param,
    list_active_pipelineruns_for_cluster_source,
)

class ExternalKubeconfigSecretCleanupTest(unittest.TestCase):
    def test_cluster_source_param(self) -> None:
        item = {
            "spec": {
                "params": [
                    {"name": "CLUSTER_SOURCE", "value": "rhoai-e2e-kubeconfig-nmanos-konflux1-nmanos"},
                ]
            }
        }
        self.assertEqual(
            _pipelinerun_cluster_source_param(item),
            "rhoai-e2e-kubeconfig-nmanos-konflux1-nmanos",
        )

    def test_list_active_excludes_completed_and_other_secrets(self) -> None:
        payload = {
            "items": [
                {
                    "metadata": {"name": "rhoai-e2e-pr-done"},
                    "spec": {"params": [{"name": "CLUSTER_SOURCE", "value": "sec-a"}]},
                    "status": {"completionTime": "2026-06-28T12:00:00Z"},
                },
                {
                    "metadata": {"name": "rhoai-e2e-pr-other-secret"},
                    "spec": {"params": [{"name": "CLUSTER_SOURCE", "value": "sec-b"}]},
                    "status": {},
                },
                {
                    "metadata": {"name": "rhoai-e2e-pr-active"},
                    "spec": {"params": [{"name": "CLUSTER_SOURCE", "value": "sec-a"}]},
                    "status": {"conditions": [{"type": "Succeeded", "status": "Unknown", "reason": "Running"}]},
                },
            ]
        }
        with mock.patch(
            "k8s.external_kubeconfig.run_cmd",
            return_value=mock.Mock(returncode=0, stdout=__import__("json").dumps(payload)),
        ):
            with mock.patch(
                "k8s.external_kubeconfig.resolve_cluster_id_for_external_cluster",
                side_effect=lambda *, cluster_source, **_: (
                    "sec-b" if cluster_source == "sec-b" else "sec-a"
                ),
            ):
                # Avoid secret lookups via the shared run_cmd mock (PLR list JSON).
                with mock.patch(
                    "k8s.external_kubeconfig.resolve_cluster_lock_key_for_external_cluster",
                    return_value="",
                ):
                    active = list_active_pipelineruns_for_cluster_source(
                        namespace="rhoai-tenant",
                        cluster_source="sec-a",
                        exclude_name="rhoai-e2e-pr-self",
                    )
        self.assertEqual(active, ["rhoai-e2e-pr-active"])

    def test_list_active_returns_none_when_oc_fails(self) -> None:
        with mock.patch(
            "k8s.external_kubeconfig.run_cmd",
            return_value=mock.Mock(returncode=1, stdout="", stderr="forbidden"),
        ):
            active = list_active_pipelineruns_for_cluster_source(
                namespace="rhoai-tenant",
                cluster_source="sec-a",
            )
        self.assertIsNone(active)

