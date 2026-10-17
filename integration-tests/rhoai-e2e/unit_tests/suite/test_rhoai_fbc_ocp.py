"""Tests for per-OCP RHOAI FBC component naming."""

from __future__ import annotations

import unittest

from suite.rhoai_fbc_ocp import (
    normalize_ocp_minor,
    ocp_minor_from_rhoai_fbc_name,
    rhoai_fbc_name_from_ocp_minor,
    rhoai_fbc_name_from_rhoai_version,
)


class RhoaiFbcOcpTests(unittest.TestCase):
    def test_round_trip(self) -> None:
        self.assertEqual(rhoai_fbc_name_from_ocp_minor("4.21"), "rhoai-fbc-fragment-ocp-421")
        self.assertEqual(ocp_minor_from_rhoai_fbc_name("rhoai-fbc-fragment-ocp-421"), "4.21")

    def test_all_stage_ocp_suffixes(self) -> None:
        for minor, suffix in (
            ("4.19", "419"),
            ("4.20", "420"),
            ("4.21", "421"),
            ("4.22", "422"),
        ):
            self.assertEqual(rhoai_fbc_name_from_ocp_minor(minor), f"rhoai-fbc-fragment-ocp-{suffix}")

    def test_round_trip_single_digit_minor(self) -> None:
        self.assertEqual(rhoai_fbc_name_from_ocp_minor("4.9"), "rhoai-fbc-fragment-ocp-409")
        self.assertEqual(ocp_minor_from_rhoai_fbc_name("rhoai-fbc-fragment-ocp-409"), "4.9")

    def test_normalize_rejects_patch(self) -> None:
        with self.assertRaises(ValueError):
            normalize_ocp_minor("4.21.1")

    def test_ocp5_has_no_fbc_fragment(self) -> None:
        self.assertEqual(rhoai_fbc_name_from_ocp_minor("5.0"), "")

    def test_version_stream_component(self) -> None:
        self.assertEqual(rhoai_fbc_name_from_rhoai_version("3.5"), "rhoai-fbc-fragment-v3-5")
        self.assertEqual(rhoai_fbc_name_from_rhoai_version("3.5-ea.2"), "")
