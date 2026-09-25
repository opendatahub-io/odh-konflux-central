#!/usr/bin/env python3
"""Unit tests for component prep track labels."""

from __future__ import annotations

import unittest
from unittest.mock import patch

from steps.component_prep_track import (
    component_prep_log_prefix,
    read_component_prep_track_note,
    record_component_prep_track,
    resolve_component_prep_track,
)


class ComponentPrepTrackTest(unittest.TestCase):
    def test_resolve_ephc_track(self) -> None:
        with patch.dict(
            "os.environ",
            {"CLUSTER_SOURCE": "EPHC", "PRODUCT": "rhoai"},
            clear=False,
        ):
            self.assertEqual(resolve_component_prep_track(), "ephc")
            self.assertEqual(component_prep_log_prefix(), "[prep-ephc]")

    def test_resolve_external_track(self) -> None:
        with patch.dict(
            "os.environ",
            {"CLUSTER_SOURCE": "rhoai-e2e-kubeconfig-psi-07", "PRODUCT": ""},
            clear=False,
        ):
            self.assertEqual(resolve_component_prep_track(), "external")
            self.assertEqual(component_prep_log_prefix(), "[prep-external]")

    def test_record_and_read_track_note(self) -> None:
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            with patch.dict("os.environ", {"TESTS_SHARED": tmp}, clear=False):
                record_component_prep_track("ephc")
                self.assertIn("EPHC", read_component_prep_track_note())


if __name__ == "__main__":
    raise SystemExit(unittest.main())
