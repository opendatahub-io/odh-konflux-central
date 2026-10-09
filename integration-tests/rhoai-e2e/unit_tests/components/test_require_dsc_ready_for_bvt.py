"""BVT must wait for DashboardReady (and settle) before operator_health pytest."""

from __future__ import annotations

import unittest
from unittest.mock import patch

from components.maas_billing.wait import (
    require_dsc_aggregate_ready_for_bvt,
    require_dsc_ready_for_bvt,
)


class _Clock:
    def __init__(self, start: float = 1_000.0) -> None:
        self.t = start

    def time(self) -> float:
        return self.t

    def sleep(self, seconds: float) -> None:
        self.t += seconds


class RequireDscReadyForBvtTest(unittest.TestCase):
    def test_waits_until_dashboard_ready_true(self) -> None:
        clock = _Clock()
        ready_seq = iter(
            [
                ("True", "Ready", ""),
                ("True", "Ready", ""),
                ("True", "Ready", ""),
            ]
        )
        dash_seq = iter(
            [
                ("False", "DeploymentsNotReady", "0/1 deployments ready"),
                ("True", "Reconciled", ""),
                ("True", "Reconciled", ""),
            ]
        )

        def _cond(name: str) -> tuple[str, str, str]:
            if name == "Ready":
                return next(ready_seq)
            if name == "DashboardReady":
                return next(dash_seq)
            return ("", "", "")

        with patch.dict("os.environ", {"BVT_DSC_READY_SETTLE_SEC": "0"}, clear=False):
            with patch("components.maas_billing.wait.time.time", clock.time):
                with patch("components.maas_billing.wait.time.sleep", clock.sleep):
                    with patch(
                        "components.maas_billing.wait._dsc_condition_types",
                        return_value={"Ready", "DashboardReady"},
                    ):
                        with patch("components.maas_billing.wait._dsc_condition", side_effect=_cond):
                            with patch(
                                "components.maas_billing.wait._dashboard_deploy_available",
                                return_value=True,
                            ):
                                require_dsc_ready_for_bvt(timeout_sec=60)

    def test_skips_dashboard_condition_when_absent(self) -> None:
        clock = _Clock()
        with patch.dict("os.environ", {"BVT_DSC_READY_SETTLE_SEC": "0"}, clear=False):
            with patch("components.maas_billing.wait.time.time", clock.time):
                with patch("components.maas_billing.wait.time.sleep", clock.sleep):
                    with patch(
                        "components.maas_billing.wait._dsc_condition_types",
                        return_value={"Ready"},
                    ):
                        with patch(
                            "components.maas_billing.wait._dsc_condition",
                            return_value=("True", "Ready", ""),
                        ):
                            with patch(
                                "components.maas_billing.wait._dashboard_deploy_available",
                                return_value=True,
                            ):
                                require_dsc_ready_for_bvt(timeout_sec=30)

    def test_resets_settle_when_dashboard_flips_false(self) -> None:
        clock = _Clock()
        dash_states = [
            ("True", "Reconciled", ""),
            ("False", "DeploymentsNotReady", "0/1 deployments ready"),
            ("True", "Reconciled", ""),
            ("True", "Reconciled", ""),
            ("True", "Reconciled", ""),
        ]
        dash_iter = iter(dash_states)

        with patch.dict("os.environ", {"BVT_DSC_READY_SETTLE_SEC": "20"}, clear=False):
            with patch("components.maas_billing.wait.time.time", clock.time):
                with patch("components.maas_billing.wait.time.sleep", clock.sleep):
                    with patch(
                        "components.maas_billing.wait._dsc_condition_types",
                        return_value={"Ready", "DashboardReady"},
                    ):
                        with patch(
                            "components.maas_billing.wait._dsc_condition",
                            side_effect=lambda name: (
                                ("True", "Ready", "")
                                if name == "Ready"
                                else next(dash_iter)
                            ),
                        ):
                            with patch(
                                "components.maas_billing.wait._dashboard_deploy_available",
                                return_value=True,
                            ):
                                require_dsc_ready_for_bvt(timeout_sec=120)
        self.assertGreaterEqual(clock.t, 1_000.0 + 20)

    def test_times_out_when_dashboard_stays_not_ready(self) -> None:
        clock = _Clock()
        with patch.dict("os.environ", {"BVT_DSC_READY_SETTLE_SEC": "0"}, clear=False):
            with patch("components.maas_billing.wait.time.time", clock.time):
                with patch("components.maas_billing.wait.time.sleep", clock.sleep):
                    with patch(
                        "components.maas_billing.wait._dsc_condition_types",
                        return_value={"Ready", "DashboardReady"},
                    ):
                        with patch(
                            "components.maas_billing.wait._dsc_condition",
                            side_effect=lambda name: (
                                ("False", "NotReady", "modelsasservice")
                                if name == "Ready"
                                else ("False", "DeploymentsNotReady", "0/1 deployments ready")
                            ),
                        ):
                            with patch(
                                "components.maas_billing.wait._dashboard_deploy_available",
                                return_value=False,
                            ):
                                with self.assertRaises(RuntimeError) as ctx:
                                    require_dsc_ready_for_bvt(timeout_sec=25)
        self.assertIn("DashboardReady", str(ctx.exception))

    def test_passes_when_dashboard_ready_even_if_aggregate_ready_false(self) -> None:
        clock = _Clock()
        with patch.dict("os.environ", {"BVT_DSC_READY_SETTLE_SEC": "0"}, clear=False):
            with patch("components.maas_billing.wait.time.time", clock.time):
                with patch("components.maas_billing.wait.time.sleep", clock.sleep):
                    with patch(
                        "components.maas_billing.wait._dsc_condition_types",
                        return_value={"Ready", "DashboardReady", "ModelsAsServiceReady"},
                    ):
                        with patch(
                            "components.maas_billing.wait._dsc_condition",
                            side_effect=lambda name: (
                                ("False", "NotReady", "modelsasservice")
                                if name == "Ready"
                                else ("True", "Reconciled", "")
                            ),
                        ):
                            with patch(
                                "components.maas_billing.wait._dashboard_deploy_available",
                                return_value=True,
                            ):
                                require_dsc_ready_for_bvt(timeout_sec=30)


class RequireDscAggregateReadyForBvtTest(unittest.TestCase):
    def test_bvt_skips_aggregate_gate_when_dashboard_ready_type(self) -> None:
        from components.maas_billing.wait import bvt_skips_aggregate_dsc_ready_gate

        with patch(
            "components.maas_billing.wait._dsc_condition_types",
            return_value={"Ready", "DashboardReady"},
        ):
            self.assertTrue(bvt_skips_aggregate_dsc_ready_gate())

    def test_maybe_skips_when_dashboard_ready_type_present(self) -> None:
        from components.maas_billing.wait import maybe_require_dsc_aggregate_ready_for_bvt

        with patch(
            "components.maas_billing.wait._dsc_condition_types",
            return_value={"Ready", "DashboardReady"},
        ):
            with patch(
                "components.maas_billing.wait.require_dsc_aggregate_ready_for_bvt",
            ) as aggregate:
                maybe_require_dsc_aggregate_ready_for_bvt(timeout_sec=60)
                aggregate.assert_not_called()

    def test_maybe_requires_when_env_true(self) -> None:
        from components.maas_billing.wait import maybe_require_dsc_aggregate_ready_for_bvt

        with patch.dict("os.environ", {"BVT_DSC_AGGREGATE_READY": "true"}, clear=False):
            with patch(
                "components.maas_billing.wait._dsc_condition_types",
                return_value={"Ready", "DashboardReady"},
            ):
                with patch(
                    "components.maas_billing.wait.require_dsc_aggregate_ready_for_bvt",
                ) as aggregate:
                    maybe_require_dsc_aggregate_ready_for_bvt(timeout_sec=60)
                    aggregate.assert_called_once_with(timeout_sec=60)

    def test_waits_until_aggregate_ready_true(self) -> None:
        clock = _Clock()
        ready_seq = iter([("False", "NotReady", "modelsasservice"), ("True", "Ready", "")])

        with patch("components.maas_billing.wait.time.time", clock.time):
            with patch("components.maas_billing.wait.time.sleep", clock.sleep):
                with patch(
                    "components.maas_billing.wait._nudge_maas_reconcile_during_bvt_wait",
                ):
                    with patch(
                        "components.maas_billing.wait._dsc_condition",
                        side_effect=lambda name: next(ready_seq) if name == "Ready" else ("", "", ""),
                    ):
                        require_dsc_aggregate_ready_for_bvt(timeout_sec=60)

    def test_times_out_when_aggregate_stays_not_ready(self) -> None:
        clock = _Clock()
        with patch("components.maas_billing.wait.time.time", clock.time):
            with patch("components.maas_billing.wait.time.sleep", clock.sleep):
                with patch(
                    "components.maas_billing.wait._nudge_maas_reconcile_during_bvt_wait",
                ):
                    with patch(
                        "components.maas_billing.wait._dsc_condition",
                        return_value=("False", "NotReady", "modelsasservice"),
                    ):
                        with self.assertRaises(RuntimeError) as ctx:
                            require_dsc_aggregate_ready_for_bvt(timeout_sec=25)
        self.assertIn("aggregate Ready", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()
