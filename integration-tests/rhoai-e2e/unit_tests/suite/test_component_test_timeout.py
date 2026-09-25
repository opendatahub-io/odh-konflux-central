"""Unit tests for per-gate component test timeout resolution."""

from __future__ import annotations

import os
import unittest

from suite.component_test_timeout import (
    apply_cluster_source_timeout_cap,
    resolve_component_test_timeout_raw,
)

class ComponentTestTimeoutTest(unittest.TestCase):
    def test_smoke_only_uses_component_default(self) -> None:
        self.assertEqual(
            resolve_component_test_timeout_raw(
                phases=("smoke",),
                component_default="10m",
                catalog_gate_defaults={"tier1": "45m"},
            ),
            "10m",
        )

    def test_tier1_only_uses_catalog_default(self) -> None:
        self.assertEqual(
            resolve_component_test_timeout_raw(
                phases=("tier1",),
                component_default="10m",
                catalog_gate_defaults={"tier1": "45m"},
            ),
            "45m",
        )

    def test_smoke_and_tier1_use_max_duration(self) -> None:
        self.assertEqual(
            resolve_component_test_timeout_raw(
                phases=("smoke", "tier1"),
                component_default="10m",
                catalog_gate_defaults={"tier1": "45m"},
            ),
            "45m",
        )

    def test_component_gate_override_wins_over_catalog(self) -> None:
        self.assertEqual(
            resolve_component_test_timeout_raw(
                phases=("tier1",),
                component_default="10m",
                component_by_gate={"tier1": "90m"},
                catalog_gate_defaults={"tier1": "45m"},
            ),
            "90m",
        )

    def test_cli_override_when_no_catalog_values(self) -> None:
        self.assertEqual(
            resolve_component_test_timeout_raw(
                phases=("smoke",),
                cli_override="30m",
            ),
            "30m",
        )

    def test_cli_override_raises_floor_when_catalog_shorter(self) -> None:
        self.assertEqual(
            resolve_component_test_timeout_raw(
                phases=("smoke",),
                component_by_gate={"smoke": "10m"},
                catalog_gate_defaults={"smoke": "10m"},
                cli_override="30m",
            ),
            "30m",
        )

    def test_cli_override_does_not_cap_longer_catalog_timeout(self) -> None:
        self.assertEqual(
            resolve_component_test_timeout_raw(
                phases=("smoke",),
                component_by_gate={"smoke": "90m"},
                catalog_gate_defaults={"smoke": "30m"},
                cli_override="30m",
            ),
            "90m",
        )


class EaasComponentTimeoutCapTest(unittest.TestCase):
    def test_ephc_caps_platform_smoke(self) -> None:
        with unittest.mock.patch.dict(os.environ, {"CLUSTER_SOURCE": "EPHC"}):
            self.assertEqual(
                apply_cluster_source_timeout_cap(component_id="platform", timeout_raw="45m"),
                "45m",
            )

    def test_ephc_caps_platform_below_catalog(self) -> None:
        with unittest.mock.patch.dict(os.environ, {"CLUSTER_SOURCE": "EPHC"}):
            self.assertEqual(
                apply_cluster_source_timeout_cap(component_id="platform", timeout_raw="60m"),
                "45m",
            )

    def test_ephc_keeps_shorter_catalog_timeout(self) -> None:
        with unittest.mock.patch.dict(os.environ, {"CLUSTER_SOURCE": "EPHC"}):
            self.assertEqual(
                apply_cluster_source_timeout_cap(component_id="mlflow", timeout_raw="5m"),
                "5m",
            )

    def test_external_cluster_unchanged(self) -> None:
        with unittest.mock.patch.dict(os.environ, {"CLUSTER_SOURCE": "rhoai-e2e-kubeconfig-rh-nightly-pm"}):
            self.assertEqual(
                apply_cluster_source_timeout_cap(component_id="platform", timeout_raw="45m"),
                "45m",
            )


class PipelineTaskTimeoutTest(unittest.TestCase):
    def test_platform_smoke_maps_to_tekton_timeout(self) -> None:
        from suite.component_test_timeout import pipeline_task_timeout_from_smoke

        self.assertEqual(pipeline_task_timeout_from_smoke("45m"), "69m0s")

    def test_short_smoke_has_floor(self) -> None:
        from suite.component_test_timeout import pipeline_task_timeout_from_smoke

        self.assertEqual(pipeline_task_timeout_from_smoke("10m"), "25m0s")


if __name__ == "__main__":
    raise SystemExit(unittest.main())
