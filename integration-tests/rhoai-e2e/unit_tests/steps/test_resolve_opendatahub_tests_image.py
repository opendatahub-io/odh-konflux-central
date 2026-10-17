#!/usr/bin/env python3
"""Tests for opendatahub-tests image resolve on external existing clusters."""

from __future__ import annotations

import os
import unittest
from unittest import mock

from steps.resolve_opendatahub_tests_image import (  # noqa: E402
    _skip_csv_cluster_probe,
    resolve_csv_version_for_tests_image,
)

class ResolveOpendatahubTestsImageTest(unittest.TestCase):
    def test_skip_probe_snapshot_only_existing(self) -> None:
        with mock.patch.dict(
            os.environ,
            {"PRODUCT": "", "CLUSTER_SOURCE": ""},
            clear=False,
        ):
            self.assertTrue(_skip_csv_cluster_probe())

    def test_probe_external_existing_cluster(self) -> None:
        with mock.patch.dict(
            os.environ,
            {"PRODUCT": "", "CLUSTER_SOURCE": "rhoai-e2e-kubeconfig-nmanos-konflux1"},
            clear=False,
        ):
            self.assertFalse(_skip_csv_cluster_probe())

    def test_resolve_probes_csv_when_external_existing(self) -> None:
        env = {
            "PRODUCT": "",
            "CLUSTER_SOURCE": "rhoai-e2e-kubeconfig-nmanos-konflux1",
            "OPERATOR_NAMESPACE": "redhat-ods-operator",
            "OPERATOR_NAME": "rhods-operator",
            "KUBECONFIG": "/tmp/fake-kubeconfig",
        }
        with mock.patch.dict(os.environ, env, clear=False):
            with mock.patch("os.path.isfile", return_value=True):
                with mock.patch(
                    "steps.resolve_opendatahub_tests_image._probe_csv_from_kubeconfig",
                    return_value="3.5.0-ea.2",
                ):
                    self.assertEqual(resolve_csv_version_for_tests_image(), "3.5.0-ea.2")

