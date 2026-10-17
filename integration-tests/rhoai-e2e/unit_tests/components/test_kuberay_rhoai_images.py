#!/usr/bin/env python3
"""Tests for KubeRay RHOAI image IDMS patch."""

from __future__ import annotations

import unittest

from components.kuberay.rhoai_images import (  # noqa: E402
    kuberay_rhoai_idms_patch_shell,
    prepend_kuberay_smoke_patch,
)


class KuberayRhoaiImagesPatchTest(unittest.TestCase):
    def test_patch_targets_rhoai_images_test(self) -> None:
        shell = kuberay_rhoai_idms_patch_shell()
        self.assertIn("raycluster_rhoai_images_test.go", shell)
        self.assertIn("olminstall-kuberay-idms", shell)
        self.assertIn("registry.redhat.io/", shell)

    def test_prepend_includes_auth_options_and_idms(self) -> None:
        out = prepend_kuberay_smoke_patch("bash run-tests.sh -testTier=Smoke")
        self.assertIn("bash run-tests.sh -testTier=Smoke", out)
        self.assertIn("TestRayClusterAuthOptions", out)
        self.assertIn("kube-rbac-proxy assertion not found", out)

    def test_idms_patch_python_compiles(self) -> None:
        from components.kuberay.rhoai_images import _rhoai_idms_patch_python_body

        compile(_rhoai_idms_patch_python_body(), "<kuberay-idms>", "exec")
