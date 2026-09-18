"""Tests for external-cluster single-flight guards."""

from __future__ import annotations

import unittest
from unittest import mock

from k8s.external_kubeconfig import (
    assert_external_cluster_idle,
    list_active_pipelineruns_for_external_cluster,
    wait_for_external_cluster_idle,
)
from suite.errors import AppError


class AssertExternalClusterIdleTest(unittest.TestCase):
    def test_skips_ephc(self) -> None:
        wait_for_external_cluster_idle(
            namespace="rhoai-tenant",
            cluster_source="EPHC",
        )

    def test_force_skips_busy(self) -> None:
        with mock.patch(
            "k8s.external_kubeconfig.list_active_pipelineruns_for_external_cluster",
            return_value=["pr-other"],
        ):
            wait_for_external_cluster_idle(
                namespace="rhoai-tenant",
                cluster_source="rhoai-e2e-kubeconfig-rh-nightly-pm",
                force=True,
            )

    def test_raises_when_busy_immediate(self) -> None:
        with mock.patch(
            "k8s.external_kubeconfig.list_active_pipelineruns_for_external_cluster",
            return_value=["pr-other"],
        ):
            with self.assertRaises(AppError) as ctx:
                assert_external_cluster_idle(
                    namespace="rhoai-tenant",
                    cluster_source="rhoai-e2e-kubeconfig-rh-nightly-pm",
                )
        self.assertIn("busy", str(ctx.exception))
        self.assertIn("--force-cluster-run", str(ctx.exception))

    def test_waits_until_idle(self) -> None:
        with mock.patch(
            "k8s.external_kubeconfig.list_active_pipelineruns_for_external_cluster",
            side_effect=[["pr-other"], []],
        ):
            with mock.patch("k8s.external_kubeconfig.time.sleep"):
                wait_for_external_cluster_idle(
                    namespace="rhoai-tenant",
                    cluster_source="rhoai-e2e-kubeconfig-rh-nightly-pm",
                    timeout_sec=120,
                    poll_interval_sec=5,
                )

    def test_list_active_matches_cluster_id(self) -> None:
        payload = {
            "items": [
                {
                    "metadata": {
                        "name": "e2e-a",
                        "labels": {"rhoai-e2e.cluster": "ods-qe-psi-07"},
                    },
                    "spec": {"params": [{"name": "CLUSTER_SOURCE", "value": "sec-a"}]},
                    "status": {},
                },
                {
                    "metadata": {"name": "rhoai-e2e-b"},
                    "spec": {"params": [{"name": "CLUSTER_SOURCE", "value": "sec-b"}]},
                    "status": {"completionTime": "2026-06-28T12:00:00Z"},
                },
                {
                    "metadata": {"name": "unrelated-pipeline"},
                    "spec": {"params": [{"name": "CLUSTER_SOURCE", "value": "sec-a"}]},
                    "status": {},
                },
            ]
        }
        with mock.patch(
            "k8s.external_kubeconfig._list_rhoai_e2e_pipelinerun_items",
            return_value=payload["items"],
        ):
            with mock.patch(
                "k8s.external_kubeconfig.resolve_cluster_id_for_external_cluster",
                return_value="ods-qe-psi-07",
            ):
                active = list_active_pipelineruns_for_external_cluster(
                    namespace="rhoai-tenant",
                    cluster_source="rhoai-e2e-kubeconfig-ods-qe-psi-07-nmanos",
                    cluster_id="ods-qe-psi-07",
                )
        self.assertEqual(active, ["e2e-a"])

    def test_list_active_matches_cluster_lock_key(self) -> None:
        payload = {
            "items": [
                {
                    "metadata": {
                        "name": "e2e-a",
                        "annotations": {
                            "rhoai-e2e.cluster-key": "api.ods-qe-psi-23.osp.rh-ods.com",
                        },
                    },
                    "spec": {"params": [{"name": "CLUSTER_SOURCE", "value": "sec-a"}]},
                    "status": {},
                },
            ]
        }
        with mock.patch(
            "k8s.external_kubeconfig._list_rhoai_e2e_pipelinerun_items",
            return_value=payload["items"],
        ):
            with mock.patch(
                "k8s.external_kubeconfig.resolve_cluster_id_for_external_cluster",
                return_value="different-label",
            ):
                with mock.patch(
                    "k8s.external_kubeconfig.resolve_cluster_lock_key_for_external_cluster",
                    return_value="api.ods-qe-psi-23.osp.rh-ods.com",
                ):
                    active = list_active_pipelineruns_for_external_cluster(
                        namespace="rhoai-tenant",
                        cluster_source="rhoai-e2e-kubeconfig-other-name",
                        cluster_id="other-label",
                    )
        self.assertEqual(active, ["e2e-a"])
