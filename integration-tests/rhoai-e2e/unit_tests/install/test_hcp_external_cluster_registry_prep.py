"""External HyperShift cluster registry prep routing."""

from __future__ import annotations

import os
import unittest
from unittest import mock


class HcpExternalClusterRegistryPrepTests(unittest.TestCase):
    def test_delegates_to_cluster_registry(self) -> None:
        from install import hcp_external_cluster_registry_prep as prep

        with (
            mock.patch.object(prep, "is_hypershift_managed_cluster", return_value=True),
            mock.patch.object(prep, "ensure_cluster_registry_for_rhoai") as registry,
            mock.patch.object(prep.Path, "is_file", return_value=False),
            mock.patch.object(prep, "load_quay_dockerconfig", return_value={"auths": {}}),
            mock.patch.dict(os.environ, {"PRODUCT": "rhoai", "QUAY_PULL_SECRET_NAME": "quay"}, clear=False),
        ):
            prep.ensure_external_cluster_hcp_registry_prep()
        registry.assert_called_once()

    def test_non_hypershift_is_noop(self) -> None:
        from install import hcp_external_cluster_registry_prep as prep

        with (
            mock.patch.object(prep, "is_hypershift_managed_cluster", return_value=False),
            mock.patch.object(prep, "ensure_cluster_registry_for_rhoai") as registry,
        ):
            prep.ensure_external_cluster_hcp_registry_prep()
        registry.assert_not_called()


if __name__ == "__main__":
    unittest.main()
