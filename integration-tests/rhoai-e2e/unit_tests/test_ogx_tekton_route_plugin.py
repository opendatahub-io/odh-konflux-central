#!/usr/bin/env python3
"""Tests for OGX Tekton port-forward plugin."""

from __future__ import annotations

import os
import unittest

from ogx_tekton_route_plugin import (  # noqa: E402
    _ogx_tekton_route_patch_enabled,
)
from suite.its_trigger_params import CLUSTER_SOURCE_EPHC


class OgxTektonRoutePluginTest(unittest.TestCase):
    def test_enabled_for_ephc_and_external(self) -> None:
        os.environ["CLUSTER_SOURCE"] = CLUSTER_SOURCE_EPHC
        self.assertTrue(_ogx_tekton_route_patch_enabled())
        os.environ["CLUSTER_SOURCE"] = "rhoai-e2e-kubeconfig-rh-nightly-pm"
        self.assertTrue(_ogx_tekton_route_patch_enabled())
        os.environ.pop("CLUSTER_SOURCE", None)
        self.assertFalse(_ogx_tekton_route_patch_enabled())
