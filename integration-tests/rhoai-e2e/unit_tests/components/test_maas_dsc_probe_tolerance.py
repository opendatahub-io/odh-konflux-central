#!/usr/bin/env python3
"""DSC condition probes must tolerate a stalled apiserver.

``oc_run`` raises ``AppError`` on timeout even with ``check=False``. Under
concurrent ITS load the shared apiserver can exceed the 30s probe budget. Before
this fix the raised ``AppError`` escaped MaaS prep and the component ended with
no JUnit at all (``infrastructure error - no JUnit file``). The probes now retry
briefly and report "unreadable" so the owning wait loop keeps waiting.
"""

from __future__ import annotations

import unittest
from unittest.mock import patch

from components.maas_billing.common import (_dsc_condition,  # noqa: E402
                                            _dsc_condition_types)
from suite.errors import AppError  # noqa: E402


class _Result:
    def __init__(self, stdout: str) -> None:
        self.returncode = 0
        self.stdout = stdout
        self.stderr = ""


class DscProbeToleranceTest(unittest.TestCase):
    @patch("components.maas_billing.common.time.sleep")
    @patch("components.maas_billing.common.oc_run")
    def test_condition_types_returns_empty_set_on_persistent_timeout(
        self, oc_run: object, _sleep: object
    ) -> None:
        oc_run.side_effect = AppError("oc timed out (30s): get datasciencecluster", 1)
        self.assertEqual(_dsc_condition_types(), set())
        self.assertEqual(oc_run.call_count, 3)

    @patch("components.maas_billing.common.time.sleep")
    @patch("components.maas_billing.common.oc_run")
    def test_condition_returns_empty_tuple_on_persistent_timeout(
        self, oc_run: object, _sleep: object
    ) -> None:
        oc_run.side_effect = AppError("oc timed out (30s): get datasciencecluster", 1)
        self.assertEqual(_dsc_condition("ModelsAsServiceReady"), ("", "", ""))
        self.assertEqual(oc_run.call_count, 3)

    @patch("components.maas_billing.common.time.sleep")
    @patch("components.maas_billing.common.oc_run")
    def test_condition_types_recovers_after_transient_timeout(
        self, oc_run: object, _sleep: object
    ) -> None:
        oc_run.side_effect = [
            AppError("oc timed out (30s): get datasciencecluster", 1),
            _Result("KserveReady ModelsAsServiceReady"),
        ]
        self.assertEqual(
            _dsc_condition_types(), {"KserveReady", "ModelsAsServiceReady"}
        )
        self.assertEqual(oc_run.call_count, 2)

    @patch("components.maas_billing.common.time.sleep")
    @patch("components.maas_billing.common.oc_run")
    def test_condition_parses_tab_fields(self, oc_run: object, _sleep: object) -> None:
        oc_run.return_value = _Result("True\tReconciled\tall good")
        self.assertEqual(
            _dsc_condition("ModelsAsServiceReady"),
            ("True", "Reconciled", "all good"),
        )

    @patch("components.maas_billing.common.time.sleep")
    @patch("components.maas_billing.common.oc_run")
    def test_condition_pads_missing_fields(self, oc_run: object, _sleep: object) -> None:
        oc_run.return_value = _Result("False")
        self.assertEqual(_dsc_condition("ModelsAsServiceReady"), ("False", "", ""))


if __name__ == "__main__":
    unittest.main()
