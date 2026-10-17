#!/usr/bin/env python3
"""Unit tests for generic --tests test-slice extensions."""

from __future__ import annotations

import unittest

from suite.component_catalog import default_components_smoke_config_path, load_components_smoke_catalog
from suite.errors import AppError
from suite.test_slice_filter import (
    filter_cypress_config_by_slice_ids,
    parse_tests_selection_with_slice_extensions,
)
from suite.tests_config import load_tests_catalog
from suite.tests_plan import validate_and_normalize_tests_csv_cli
from suite.constants import default_tests_config_path

class TestSliceFilterTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.tests_catalog = load_tests_catalog(default_tests_config_path())
        cls.components_catalog = load_components_smoke_catalog(default_components_smoke_config_path())
        cls.dashboard = cls.components_catalog.components["dashboard_cypress"]
        assert cls.dashboard.runner is not None and cls.dashboard.runner.cypress is not None
        cls.cypress_config = cls.dashboard.runner.cypress

    def test_parse_smokeset_only_infers_smoke_phase(self) -> None:
        ext = parse_tests_selection_with_slice_extensions(
            "SmokeSet5",
            phase_ids=frozenset(self.tests_catalog.phase_ids),
            required_phase_ids=self.tests_catalog.required_ids,
            components_catalog=self.components_catalog,
        )
        self.assertEqual(ext.phases, frozenset({"smoke"}))
        self.assertEqual(ext.test_tags, ("SmokeSet5",))
        self.assertEqual(ext.scoped_component_ids, ("dashboard_cypress",))
        self.assertTrue(ext.auto_scope_components)

    def test_parse_smoke_and_smokeset(self) -> None:
        ext = parse_tests_selection_with_slice_extensions(
            "smoke,SmokeSet5",
            phase_ids=frozenset(self.tests_catalog.phase_ids),
            required_phase_ids=self.tests_catalog.required_ids,
            components_catalog=self.components_catalog,
        )
        self.assertEqual(ext.phases, frozenset({"smoke"}))
        self.assertEqual(ext.test_tags, ("SmokeSet5",))

    def test_parse_sanity_set_infers_tier1(self) -> None:
        ext = parse_tests_selection_with_slice_extensions(
            "SanitySet2",
            phase_ids=frozenset(self.tests_catalog.phase_ids),
            required_phase_ids=self.tests_catalog.required_ids,
            components_catalog=self.components_catalog,
        )
        self.assertEqual(ext.phases, frozenset({"tier1"}))
        self.assertEqual(ext.test_tags, ("SanitySet2",))

    def test_unknown_token_raises(self) -> None:
        with self.assertRaises(AppError):
            parse_tests_selection_with_slice_extensions(
                "not-a-real-set",
                phase_ids=frozenset(self.tests_catalog.phase_ids),
                required_phase_ids=self.tests_catalog.required_ids,
                components_catalog=self.components_catalog,
            )

    def test_filter_keeps_matching_smoke_set(self) -> None:
        filtered = filter_cypress_config_by_slice_ids(self.cypress_config, ("smoke",), "SmokeSet5")
        self.assertEqual(len(filtered.gates["smoke"]), 1)
        self.assertEqual(filtered.gates["smoke"][0].results_subdir, "SmokeSet5")

    def test_validate_cli_returns_slice_csv(self) -> None:
        phases, slices, scoped, auto_scope = validate_and_normalize_tests_csv_cli(
            "SmokeSet5",
            self.tests_catalog,
            components_catalog=self.components_catalog,
        )
        self.assertEqual(phases, "smoke")
        self.assertEqual(slices, "SmokeSet5")
        self.assertEqual(scoped, ("dashboard_cypress",))
        self.assertTrue(auto_scope)

