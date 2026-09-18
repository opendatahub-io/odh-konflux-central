"""Unit tests for prepare-diagnostics kubeconfig soft-skip on CONFORMA_GATE=skip."""

from __future__ import annotations

import os
import unittest
from unittest import mock

from steps import prepare_diagnostics_kubeconfig as prep
from suite.conforma_gate import CONFORMA_GATE_SKIP


class PrepareDiagnosticsKubeconfigTest(unittest.TestCase):
    @mock.patch.object(prep, "write_result")
    @mock.patch.dict(
        os.environ,
        {
            "CONFORMA_GATE": CONFORMA_GATE_SKIP,
            "KUBECONFIG_PATH_RESULT": "/tmp/kc-result",
            "CLUSTER_SOURCE": "",
        },
        clear=False,
    )
    def test_conforma_skip_returns_without_kubeconfig(self, write_result: mock.MagicMock) -> None:
        self.assertEqual(prep.main(), 0)
        write_result.assert_called_once_with("/tmp/kc-result", "")


if __name__ == "__main__":
    unittest.main()
