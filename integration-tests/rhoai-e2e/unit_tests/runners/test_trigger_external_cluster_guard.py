"""Tests for external-cluster trigger prep (Konflux lock query; queue in pipeline)."""

from __future__ import annotations

from suite.constants import DEFAULT_APP, DEFAULT_NAMESPACE
import unittest
from unittest import mock

from k8s.external_kubeconfig import assert_external_cluster_lock_queryable
from suite.errors import AppError


class AssertExternalClusterLockQueryableTest(unittest.TestCase):
    def test_skips_ephc(self) -> None:
        assert_external_cluster_lock_queryable(
            namespace=DEFAULT_NAMESPACE,
            cluster_source="EPHC",
            cluster_id="",
            force=False,
        )

    def test_raises_when_konflux_query_fails(self) -> None:
        with mock.patch(
            "k8s.external_kubeconfig.list_active_pipelineruns_for_external_cluster",
            return_value=None,
        ):
            with self.assertRaises(AppError) as ctx:
                assert_external_cluster_lock_queryable(
                    namespace=DEFAULT_NAMESPACE,
                    cluster_source="rhoai-e2e-kubeconfig-rh-nightly-pm",
                    cluster_id="rh-nightly-pm",
                    force=False,
                )
        self.assertIn("Konflux API query failed", str(ctx.exception))

    def test_allows_busy_cluster_at_trigger_time(self) -> None:
        with mock.patch(
            "k8s.external_kubeconfig.list_active_pipelineruns_for_external_cluster",
            return_value=["e2e-cli-nmanos-rh-nightly-pm-rhoai-smoke-czshr"],
        ):
            assert_external_cluster_lock_queryable(
                namespace=DEFAULT_NAMESPACE,
                cluster_source="rhoai-e2e-kubeconfig-rh-nightly-pm",
                cluster_id="rh-nightly-pm",
                force=False,
            )


if __name__ == "__main__":
    unittest.main()
