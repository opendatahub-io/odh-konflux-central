"""Tests for external kubeconfig secret volume mount staging."""

from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path

from steps.external_kubeconfig_mount import copy_external_kubeconfig_mount

class ExternalKubeconfigMountTests(unittest.TestCase):
    def test_copy_from_mount(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            mount_dir = Path(tmp) / "mount"
            mount_dir.mkdir()
            mount_file = mount_dir / "kubeconfig"
            mount_file.write_text("apiVersion: v1\n", encoding="utf-8")
            dest = Path(tmp) / "out" / "kubeconfig"
            os.environ["OLMINSTALL_EXTERNAL_KUBECONFIG_MOUNT"] = str(mount_file)
            try:
                self.assertTrue(copy_external_kubeconfig_mount(dest))
            finally:
                os.environ.pop("OLMINSTALL_EXTERNAL_KUBECONFIG_MOUNT", None)
            self.assertEqual(dest.read_text(encoding="utf-8"), "apiVersion: v1\n")

    def test_missing_mount_returns_false(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            dest = Path(tmp) / "kubeconfig"
            os.environ["OLMINSTALL_EXTERNAL_KUBECONFIG_MOUNT"] = str(Path(tmp) / "missing")
            try:
                self.assertFalse(copy_external_kubeconfig_mount(dest))
            finally:
                os.environ.pop("OLMINSTALL_EXTERNAL_KUBECONFIG_MOUNT", None)
            self.assertFalse(dest.is_file())

