#!/usr/bin/env python3
"""Unit tests for deps-only MaaS functional gate (no cluster)."""

from __future__ import annotations

import os
import unittest
from unittest.mock import patch

from components.maas_billing.common import deps_only_install_dependencies_smoke

class DepsOnlyMaaSFunctionalGateTest(unittest.TestCase):
    @patch("components.maas_billing.common.dsc_crd_available", return_value=False)
    @patch.dict(os.environ, {"PRODUCT": ""}, clear=False)
    def test_existing_without_dsc_uses_functional_gate(self, _dsc) -> None:
        self.assertTrue(deps_only_install_dependencies_smoke())

    @patch("components.maas_billing.common.dsc_crd_available", return_value=True)
    @patch.dict(os.environ, {"PRODUCT": "", "INSTALL_DEPENDENCIES": "true"}, clear=False)
    def test_install_dependencies_with_dsc_still_uses_functional_gate(self, _dsc) -> None:
        self.assertTrue(deps_only_install_dependencies_smoke())

    @patch("components.maas_billing.common.dsc_crd_available", return_value=True)
    @patch.dict(os.environ, {"PRODUCT": ""}, clear=False)
    def test_existing_with_dsc_without_install_dependencies_uses_dsc_gate(self, _dsc) -> None:
        self.assertFalse(deps_only_install_dependencies_smoke())

if __name__ == "__main__":
    raise SystemExit(unittest.main())
