#!/usr/bin/env python3
"""Unit tests for versioned Quay image tag resolution."""

from __future__ import annotations

import unittest
from unittest.mock import patch

from suite.resolve_versioned_image import ea_fallback_tags, prior_minor_ga_tags, resolve_versioned_image

class ResolveVersionedImageTest(unittest.TestCase):
    def test_ea_fallback_tags_prefer_newest_ea_first(self) -> None:
        tags = ea_fallback_tags("3.5.0-ea.2")
        self.assertEqual(tags[0], "3.5-ea.2")
        self.assertEqual(tags[-1], "3.5")

    def test_prior_minor_ga_tags_walks_down_from_installed_minor(self) -> None:
        self.assertEqual(prior_minor_ga_tags("3.6.0-ea.1"), ["3.5", "3.4", "3.3", "3.2", "3.1"])
        self.assertEqual(prior_minor_ga_tags("3.5.1"), ["3.4", "3.3", "3.2", "3.1"])

    @patch("suite.resolve_versioned_image._tag_exists", return_value=False)
    def test_unknown_version_falls_back_to_latest(self, _exists) -> None:
        repo = "quay.io/opendatahub/example-tests"
        self.assertEqual(resolve_versioned_image(repo, "9.9.9-weird"), f"{repo}:latest")

    @patch("suite.resolve_versioned_image._tag_exists")
    def test_resolves_ea_tag_before_ga_minor(self, exists) -> None:
        repo = "quay.io/opendatahub/distributed-workloads-tests"

        def _side_effect(_repo: str, tag: str) -> bool:
            return tag == "3.5-ea.2"

        exists.side_effect = _side_effect
        self.assertEqual(
            resolve_versioned_image(repo, "3.5.0-ea.2"),
            f"{repo}:3.5-ea.2",
        )

    @patch("suite.resolve_versioned_image._tag_exists")
    def test_ea_prefers_latest_over_shorthand_ea_tag(self, exists) -> None:
        repo = "quay.io/opendatahub/opendatahub-tests"

        def _side_effect(_repo: str, tag: str) -> bool:
            return tag in {"3.6ea1", "latest"}

        exists.side_effect = _side_effect
        self.assertEqual(
            resolve_versioned_image(repo, "3.6.0-ea.1"),
            f"{repo}:latest",
        )

    @patch("suite.resolve_versioned_image._tag_exists")
    def test_ea_prefers_prior_minor_ga_before_latest(self, exists) -> None:
        repo = "quay.io/opendatahub/opendatahub-tests"

        def _side_effect(_repo: str, tag: str) -> bool:
            return tag in {"latest", "3.5"}

        exists.side_effect = _side_effect
        self.assertEqual(
            resolve_versioned_image(repo, "3.6.0-ea.1"),
            f"{repo}:3.5",
        )

    @patch("suite.resolve_versioned_image._tag_exists")
    def test_ea_falls_back_to_latest_when_no_prior_ga(self, exists) -> None:
        repo = "quay.io/opendatahub/opendatahub-tests"

        def _side_effect(_repo: str, tag: str) -> bool:
            return tag == "latest"

        exists.side_effect = _side_effect
        self.assertEqual(
            resolve_versioned_image(repo, "3.6.0-ea.1"),
            f"{repo}:latest",
        )

    @patch("suite.resolve_versioned_image._tag_exists")
    def test_non_ea_still_uses_prior_minor_ga_before_latest(self, exists) -> None:
        repo = "quay.io/opendatahub/opendatahub-tests"

        def _side_effect(_repo: str, tag: str) -> bool:
            return tag == "3.5"

        exists.side_effect = _side_effect
        self.assertEqual(
            resolve_versioned_image(repo, "3.6.0"),
            f"{repo}:3.5",
        )


if __name__ == "__main__":
    raise SystemExit(unittest.main())
