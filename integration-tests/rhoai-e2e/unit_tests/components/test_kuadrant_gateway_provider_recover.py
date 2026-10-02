"""recover_kuadrant_after_gateway_api_provider after cleanup+reinstall."""

from __future__ import annotations

import unittest
from unittest.mock import MagicMock, patch

from components.maas_billing.auth import recover_kuadrant_after_gateway_api_provider


class KuadrantGatewayProviderRecoverTest(unittest.TestCase):
    @patch("helpers.gateway_stack_marker.clear_gateway_stack_incomplete_marker")
    @patch("components.maas_billing.auth.kuadrant_cr_ready", return_value=True)
    def test_already_ready_clears_marker(self, _ready, clear_marker) -> None:
        self.assertTrue(recover_kuadrant_after_gateway_api_provider())
        clear_marker.assert_called_once()

    @patch("components.maas_billing.auth._gateway_api_provider_present", return_value=False)
    @patch("components.maas_billing.auth._kuadrant_ready_status", return_value=("False", "MissingDependency"))
    @patch("components.maas_billing.auth.kuadrant_cr_ready", return_value=False)
    def test_no_gatewayclass_skips_restart(self, _ready, _status, _provider) -> None:
        with patch("components.maas_billing.auth._restart_kuadrant_operator_pods") as restart:
            self.assertFalse(recover_kuadrant_after_gateway_api_provider())
        restart.assert_not_called()

    @patch("helpers.gateway_stack_marker.clear_gateway_stack_incomplete_marker")
    @patch("components.maas_billing.auth._sleep")
    @patch("components.maas_billing.auth._restart_kuadrant_operator_pods")
    @patch("components.maas_billing.auth._gateway_api_provider_present", return_value=True)
    @patch(
        "components.maas_billing.auth._kuadrant_ready_status",
        side_effect=[("False", "MissingDependency"), ("True", "Ready")],
    )
    @patch("components.maas_billing.auth.kuadrant_cr_ready", side_effect=[False, False, True])
    def test_restarts_when_provider_present(
        self,
        _ready,
        _status,
        _provider,
        restart,
        _sleep,
        clear_marker,
    ) -> None:
        self.assertTrue(recover_kuadrant_after_gateway_api_provider(timeout_sec=30))
        restart.assert_called_once()
        clear_marker.assert_called_once()

    @patch("components.maas_billing.auth.run_post_install_rhcl_operator", return_value=False)
    @patch("components.maas_billing.auth._sleep")
    @patch("components.maas_billing.auth._restart_kuadrant_operator_pods")
    @patch("components.maas_billing.auth._gateway_api_provider_present", return_value=True)
    @patch(
        "components.maas_billing.auth._kuadrant_ready_status",
        return_value=("False", "MissingDependency"),
    )
    @patch("components.maas_billing.auth.kuadrant_cr_ready", return_value=False)
    def test_gives_up_when_missing_dependency_persists(
        self,
        _ready,
        _status,
        _provider,
        restart,
        sleep,
        _post_install,
    ) -> None:
        clock_t = {"t": 1_000.0}

        def fake_time() -> float:
            return clock_t["t"]

        def fake_sleep(seconds: float) -> None:
            clock_t["t"] += seconds

        sleep.side_effect = fake_sleep
        with patch("components.maas_billing.auth.time.time", fake_time):
            self.assertFalse(recover_kuadrant_after_gateway_api_provider(timeout_sec=300))
        restart.assert_called_once()
        self.assertLessEqual(clock_t["t"], 1_000.0 + 60)

    def test_gateway_api_provider_present_parses_accepted(self) -> None:
        from components.maas_billing import auth as auth_mod

        with patch.object(
            auth_mod,
            "_oc_run",
            return_value=MagicMock(
                returncode=0,
                stdout="data-science-gateway-class\tTrue\nopenshift-default\tTrue\n",
            ),
        ):
            self.assertTrue(auth_mod._gateway_api_provider_present())


if __name__ == "__main__":
    raise SystemExit(unittest.main())
