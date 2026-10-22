"""Unit tests for FBC structural validation helpers (no make / git / cluster)."""

from __future__ import annotations

import os
import unittest
from pathlib import Path
from unittest.mock import patch

from steps.validate_fbc_structural import (
    ensure_findutils,
    ensure_opm,
    ensure_yq,
    nightly_release_path,
    should_skip_structural,
)


class NightlyReleasePathTests(unittest.TestCase):
    def test_explicit_catalog_line(self) -> None:
        self.assertEqual(
            nightly_release_path("3.6-ea.1", snapshot_raw="", component_name=""),
            "releases/self-managed/nightly-3.6-ea.1",
        )

    def test_strips_default_suffix(self) -> None:
        self.assertEqual(
            nightly_release_path("3.5-ea.2 (default)", snapshot_raw="", component_name=""),
            "releases/self-managed/nightly-3.5-ea.2",
        )

    def test_rejects_unspecified(self) -> None:
        self.assertEqual(nightly_release_path("unspecified (default)", "", ""), "")
        self.assertEqual(nightly_release_path("n/a", "", ""), "")
        self.assertEqual(nightly_release_path("", "", ""), "")

    def test_opm_render_fallback(self) -> None:
        with patch(
            "steps.validate_fbc_structural.catalog_line_from_fbc_opm_render",
            return_value="3.6-ea.1",
        ):
            self.assertEqual(
                nightly_release_path("", snapshot_raw="", component_name="", fbcf_image="quay.io/x@sha256:abc"),
                "releases/self-managed/nightly-3.6-ea.1",
            )

    def test_snapshot_json_fallback(self) -> None:
        snap = {
            "metadata": {
                "annotations": {
                    "pac.test.appstudio.openshift.io/sha-title": "Patching the stage catalog with rhoai-3.6-ea.1",
                }
            }
        }
        import json

        self.assertEqual(
            nightly_release_path("", snapshot_raw=json.dumps(snap), component_name=""),
            "releases/self-managed/nightly-3.6-ea.1",
        )


class EnsureOpmTests(unittest.TestCase):
    def test_skips_download_when_opm_on_path(self) -> None:
        with patch("steps.validate_fbc_structural.shutil.which", return_value="/usr/bin/opm"):
            with patch("steps.validate_fbc_structural.subprocess.run") as run:
                path = ensure_opm()
        self.assertEqual(path, Path("/usr/bin/opm"))
        run.assert_not_called()

    def test_yq_skips_download_when_on_path(self) -> None:
        with patch("steps.validate_fbc_structural.shutil.which", return_value="/usr/bin/yq"):
            with patch("steps.validate_fbc_structural.subprocess.run") as run:
                path = ensure_yq()
        self.assertEqual(path, Path("/usr/bin/yq"))
        run.assert_not_called()

    def test_findutils_skips_install_when_find_on_path(self) -> None:
        with patch("steps.validate_fbc_structural.shutil.which", return_value="/usr/bin/find"):
            with patch("steps.validate_fbc_structural.subprocess.run") as run:
                ensure_findutils()
        run.assert_not_called()


class SkipStructuralTests(unittest.TestCase):
    def test_skip_test_only(self) -> None:
        self.assertTrue(should_skip_structural(product="", fbcf_image="quay.io/rhoai/x@sha256:abc"))

    def test_skip_odh(self) -> None:
        self.assertTrue(should_skip_structural(product="odh", fbcf_image="quay.io/rhoai/x@sha256:abc"))

    def test_skip_na_image(self) -> None:
        self.assertTrue(should_skip_structural(product="rhoai", fbcf_image="n/a"))

    def test_run_rhoai_with_digest(self) -> None:
        self.assertFalse(
            should_skip_structural(
                product="rhoai",
                fbcf_image="quay.io/rhoai/rhoai-fbc-fragment@sha256:deadbeef",
            )
        )


class MainSkipTests(unittest.TestCase):
    @patch.dict(
        os.environ,
        {"PRODUCT": "rhoai", "FBCF_IMAGE": "n/a"},
        clear=False,
    )
    def test_main_skips_na_image(self) -> None:
        from steps.validate_fbc_structural import main

        self.assertEqual(main(), 0)
