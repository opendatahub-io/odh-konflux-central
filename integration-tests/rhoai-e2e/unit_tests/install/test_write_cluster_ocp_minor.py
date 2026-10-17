"""Tests for install.write_cluster_ocp_minor."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from install.write_cluster_ocp_minor import write_cluster_ocp_minor_result


class WriteClusterOcpMinorTests(unittest.TestCase):
    @patch("install.write_cluster_ocp_minor.cluster_ocp_minor_from_kubeconfig", return_value="4.22")
    def test_writes_result_file(self, _probe: object) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "ocpMinor"
            minor = write_cluster_ocp_minor_result("/tmp/kubeconfig", out_path=out)
            self.assertEqual(minor, "4.22")
            self.assertEqual(out.read_text(encoding="ascii"), "4.22")

    @patch("install.write_cluster_ocp_minor.cluster_ocp_minor_from_kubeconfig", return_value="")
    def test_empty_probe_skips_write(self, _probe: object) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "ocpMinor"
            minor = write_cluster_ocp_minor_result("/tmp/kubeconfig", out_path=out)
            self.assertEqual(minor, "")
            self.assertFalse(out.is_file())
