"""Unit tests for combined smoke/tier1 pytest marker selection."""

from __future__ import annotations

import unittest

from suite.component_phases import combine_pytest_markers, parse_component_test_phases

class ComponentTestPhasesTest(unittest.TestCase):
    def test_parse_component_test_phases_filters_and_orders(self) -> None:
        self.assertEqual(parse_component_test_phases("bvt,smoke,tier1"), ("smoke", "tier1"))

    def test_combine_single_smoke_marker(self) -> None:
        self.assertEqual(
            combine_pytest_markers({"smoke": "smoke"}, ("smoke",)),
            "smoke",
        )

    def test_combine_smoke_and_tier1_one_pytest_expression(self) -> None:
        self.assertEqual(
            combine_pytest_markers({"smoke": "smoke", "tier1": "tier1"}, ("smoke", "tier1")),
            "smoke or tier1",
        )

if __name__ == "__main__":
    raise SystemExit(unittest.main())
