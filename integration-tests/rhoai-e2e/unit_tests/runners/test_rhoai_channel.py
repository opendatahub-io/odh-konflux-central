#!/usr/bin/env python3
"""Unit tests for RHOAI OLM channel auto-selection."""

from __future__ import annotations

import unittest

from runners.cli.rhoai_channel import resolve_rhoai_update_channel

class RhoaiChannelTest(unittest.TestCase):
    def test_version_35_maps_to_stable_35(self) -> None:
        self.assertEqual(resolve_rhoai_update_channel(version="3.5"), "stable-3.5")

    def test_version_350_ea_maps_to_beta(self) -> None:
        self.assertEqual(resolve_rhoai_update_channel(version="3.5.0-ea.2"), "beta")

    def test_version_35_ea_maps_to_beta(self) -> None:
        self.assertEqual(resolve_rhoai_update_channel(version="3.5-ea.2"), "beta")

    def test_version_34_maps_to_stable_34(self) -> None:
        self.assertEqual(resolve_rhoai_update_channel(version="3.4"), "stable-3.4")

    def test_version_33_maps_to_stable_33(self) -> None:
        self.assertEqual(resolve_rhoai_update_channel(version="3.3"), "stable-3.3")

    def test_version_225_maps_to_stable(self) -> None:
        self.assertEqual(resolve_rhoai_update_channel(version="2.25"), "stable")

    def test_version_40_maps_to_stable_40(self) -> None:
        self.assertEqual(resolve_rhoai_update_channel(version="4.0"), "stable-4.0")

    def test_resolved_app_without_version_maps_to_beta(self) -> None:
        self.assertEqual(
            resolve_rhoai_update_channel(resolved_app="rhoai-v3-5-ea-2"),
            "beta",
        )
        self.assertEqual(
            resolve_rhoai_update_channel(resolved_app="rhoai-v3-4-foo"),
            "beta",
        )

    def test_explicit_version_overrides_resolved_app(self) -> None:
        self.assertEqual(
            resolve_rhoai_update_channel(version="3.4", resolved_app="rhoai-v3-5-ea-2"),
            "stable-3.4",
        )

if __name__ == "__main__":
    raise SystemExit(unittest.main())
