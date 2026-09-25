#!/usr/bin/env python3
"""Unit tests for smoke DSCI ServiceMesh gating."""

from __future__ import annotations

import os
import unittest
from unittest.mock import patch

from install.dsc_install import (
    _dsc_smoke_managed_components,
    _install_requires_dashboard_gateway,
    _smoke_components_need_servicemesh,
    smoke_components_use_kserve_raw_deployment,
)

class SmokeServiceMeshTest(unittest.TestCase):
    def test_kserve_components_need_servicemesh(self) -> None:
        self.assertTrue(_smoke_components_need_servicemesh("model_server"))
        self.assertTrue(_smoke_components_need_servicemesh("maas_billing,model_runtime"))

    def test_trustyai_needs_servicemesh(self) -> None:
        self.assertTrue(_smoke_components_need_servicemesh("ai_safety"))

    def test_dashboard_cypress_needs_servicemesh(self) -> None:
        self.assertTrue(_smoke_components_need_servicemesh("dashboard_cypress"))

    def test_workbenches_only_does_not_need_servicemesh(self) -> None:
        self.assertFalse(_smoke_components_need_servicemesh("workbenches,model_registry"))

    def test_ai_safety_uses_kserve_raw_deployment_on_ephc(self) -> None:
        self.assertTrue(smoke_components_use_kserve_raw_deployment("ai_safety"))
        self.assertTrue(smoke_components_use_kserve_raw_deployment("workbenches,ai_safety"))

    @patch.dict(os.environ, {"PRODUCT": "rhoai"}, clear=False)
    def test_rhoai_install_requires_dashboard_gateway(self) -> None:
        self.assertTrue(_install_requires_dashboard_gateway())
        managed = _dsc_smoke_managed_components("model_registry", defer_for_install=True)
        self.assertIn("dashboard", managed)

    @patch.dict(os.environ, {"PRODUCT": ""}, clear=False)
    def test_existing_product_does_not_require_dashboard_gateway(self) -> None:
        self.assertFalse(_install_requires_dashboard_gateway())

if __name__ == "__main__":
    raise SystemExit(unittest.main())
