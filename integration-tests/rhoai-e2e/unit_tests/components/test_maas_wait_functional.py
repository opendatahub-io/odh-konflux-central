#!/usr/bin/env python3
"""Tests for MaaS wait early-exit when functionally ready."""

from __future__ import annotations

import unittest
from unittest.mock import patch

from components.maas_billing.wait import _wait_for_maas_smoke_ready  # noqa: E402

class MaasWaitFunctionalReadyTest(unittest.TestCase):
    @patch("components.maas_billing.wait.maas_smoke_acceptable_for_run")
    @patch("components.maas_billing.wait.time.sleep")
    @patch("components.maas_billing.common.deps_only_install_dependencies_smoke", return_value=False)
    @patch("components.maas_billing.wait._dsc_condition_types", return_value=set())
    def test_exits_when_models_as_service_true_but_dsc_blocked_by_ogx(
        self,
        _types: object,
        _deps_only: object,
        _sleep: object,
        acceptable: object,
    ) -> None:
        acceptable.return_value = (
            True,
            "functional MaaS ready (DSC Ready=False: ogx reconciling)",
        )
        _wait_for_maas_smoke_ready(timeout_sec=60)

    @patch("components.maas_billing.wait.time.sleep")
    @patch("components.maas_billing.wait.maas_smoke_acceptable_for_run")
    @patch("components.maas_billing.common.deps_only_install_dependencies_smoke", return_value=False)
    @patch("components.maas_billing.wait._dsc_condition_types", return_value={"MaaSPrerequisitesAvailable"})
    def test_exits_when_functional_ready_but_maas_prereq_lagging(
        self,
        _types: object,
        _deps_only: object,
        acceptable: object,
        _sleep: object,
    ) -> None:
        acceptable.return_value = (True, "functional MaaS ready with gateway annotations")
        _wait_for_maas_smoke_ready(timeout_sec=60)
        acceptable.assert_called()

