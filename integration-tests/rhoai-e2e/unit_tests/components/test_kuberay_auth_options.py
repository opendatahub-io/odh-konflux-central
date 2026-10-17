#!/usr/bin/env python3
"""KubeRay authOptions CRD skip patch."""

from __future__ import annotations

import unittest

from components.kuberay.auth_options import (
    kuberay_skip_auth_options_if_crd_missing_shell,
    prepend_kuberay_auth_options_skip,
)


class KuberayAuthOptionsSkipTest(unittest.TestCase):
    def test_shell_skips_when_crd_lacks_field(self) -> None:
        shell = kuberay_skip_auth_options_if_crd_missing_shell()
        self.assertIn("rayclusters.ray.io", shell)
        self.assertIn("authOptions", shell)
        self.assertIn("TestRayClusterAuthOptions", shell)
        self.assertIn("TestRayClusterCRDPresent", shell)
        self.assertIn("run-tests.sh", shell)

    def test_prepend_wraps_run_command(self) -> None:
        out = prepend_kuberay_auth_options_skip("bash run-tests.sh -testTier=Smoke")
        self.assertIn("bash run-tests.sh -testTier=Smoke", out)
        self.assertIn("TestRayClusterAuthOptions", out)
