"""Unit tests for OpenShift CI releases JSON used by provision-ephemeral-cluster."""

from __future__ import annotations

import json
import os
import unittest
from unittest import mock

from steps.resolve_oci_releases import main, releases_json, resolve_ocp_minor


class ResolveOciReleasesTest(unittest.TestCase):
    def test_override_prefix_wins(self) -> None:
        self.assertEqual(
            resolve_ocp_minor("4.20.", "rhoai-fbc-fragment-ocp-421"),
            "4.20",
        )

    def test_fbc_name_when_override_is_latest_placeholder(self) -> None:
        self.assertEqual(
            resolve_ocp_minor("latest (default)", "rhoai-fbc-fragment-ocp-421"),
            "4.21",
        )

    def test_fbc_ocp422(self) -> None:
        self.assertEqual(resolve_ocp_minor("", "rhoai-fbc-fragment-ocp-422"), "4.22")

    def test_default_when_no_fbc_suffix(self) -> None:
        self.assertEqual(resolve_ocp_minor("", "odh-operator-catalog"), "4.21")

    def test_releases_json_stable_multi(self) -> None:
        payload = json.loads(releases_json("4.21"))
        self.assertEqual(
            payload,
            {
                "latest": {
                    "release": {
                        "channel": "stable",
                        "version": "4.21",
                        "architecture": "multi",
                    }
                }
            },
        )

    def test_releases_json_candidate_ec(self) -> None:
        payload = json.loads(releases_json("5.0", "candidate"))
        self.assertEqual(
            payload,
            {
                "latest": {
                    "release": {
                        "channel": "candidate",
                        "version": "5.0",
                        "architecture": "multi",
                    }
                }
            },
        )

    def test_releases_json_nightly(self) -> None:
        payload = json.loads(releases_json("5.0", "nightly"))
        self.assertEqual(
            payload,
            {
                "latest": {
                    "candidate": {
                        "product": "ocp",
                        "stream": "nightly",
                        "version": "5.0",
                    }
                }
            },
        )

    def test_main_writes_minor_releases_and_channel(self) -> None:
        with mock.patch.dict(
            os.environ,
            {
                "OVERRIDE": "4.20",
                "OCP_RELEASE_CHANNEL": "candidate",
                "RHOAI_FBC_NAME": "",
                "MINOR_RESULT_PATH": "/tekton/results/ocpMinor",
                "RELEASES_RESULT_PATH": "/tekton/results/releases",
                "CHANNEL_RESULT_PATH": "/tekton/results/ocpChannel",
            },
            clear=False,
        ), mock.patch("steps.resolve_oci_releases.write_result") as write_result:
            self.assertEqual(main(), 0)
        write_result.assert_any_call("/tekton/results/ocpMinor", "4.20")
        write_result.assert_any_call("/tekton/results/ocpChannel", "candidate")
        write_result.assert_any_call(
            "/tekton/results/releases",
            releases_json("4.20", "candidate"),
        )
        self.assertEqual(write_result.call_count, 3)
