"""Tests for odh-dashboard sourceRef resolution."""

from __future__ import annotations

import os
import unittest
from unittest import mock

from components.dashboard_cypress.source_ref import (
    resolve_dashboard_git_source,
    resolve_dashboard_source_ref,
)

class DashboardSourceRefTest(unittest.TestCase):
    def test_maps_rhoai_341_to_rhoai_34_branch(self) -> None:
        self.assertEqual(
            resolve_dashboard_source_ref("3.4.1", catalog_ref="main", product=""),
            "rhoai-3.4",
        )

    def test_maps_ea_version_to_ea_branch(self) -> None:
        self.assertEqual(
            resolve_dashboard_source_ref("3.5.0-ea.2", catalog_ref="main", product="rhoai"),
            "rhoai-3.5-ea.2",
        )

    def test_maps_rhoai_36_ea_to_ea_branch(self) -> None:
        self.assertEqual(
            resolve_dashboard_source_ref("3.6.0-ea.1", catalog_ref="main", product="rhoai"),
            "rhoai-3.6-ea.1",
        )

    def test_odh_product_uses_odh_prefix(self) -> None:
        self.assertEqual(
            resolve_dashboard_source_ref("3.4.0", catalog_ref="main", product="odh"),
            "odh-3.4",
        )

    def test_unknown_version_uses_catalog_fallback(self) -> None:
        self.assertEqual(
            resolve_dashboard_source_ref("(unknown)", catalog_ref="main", product=""),
            "main",
        )
        self.assertEqual(
            resolve_dashboard_source_ref("", catalog_ref="rhoai-3.3", product=""),
            "rhoai-3.3",
        )

    def test_env_override_wins(self) -> None:
        with mock.patch.dict(os.environ, {"DASHBOARD_SOURCE_REF_OVERRIDE": "feature/foo"}, clear=False):
            self.assertEqual(
                resolve_dashboard_source_ref("3.4.1", catalog_ref="main", product=""),
                "feature/foo",
            )

    def test_rhoai_branch_uses_rhds_repo(self) -> None:
        src = resolve_dashboard_git_source(
            "3.4.1",
            catalog_repo="https://github.com/opendatahub-io/odh-dashboard.git",
            catalog_ref="main",
            product="",
        )
        self.assertEqual(src.ref, "rhoai-3.4")
        self.assertEqual(src.repo, "https://github.com/red-hat-data-services/odh-dashboard.git")

    def test_unknown_version_keeps_catalog_repo(self) -> None:
        src = resolve_dashboard_git_source(
            "(unknown)",
            catalog_repo="https://github.com/opendatahub-io/odh-dashboard.git",
            catalog_ref="main",
            product="",
        )
        self.assertEqual(src.repo, "https://github.com/opendatahub-io/odh-dashboard.git")
        self.assertEqual(src.ref, "main")

