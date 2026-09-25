#!/usr/bin/env python3
"""Unit tests for Llama Stack dependency gating (no cluster)."""

from __future__ import annotations

import unittest
from unittest.mock import patch

from install.llama_stack_deps import (
    components_csv_requires_llama_stack,
    try_prepare_llama_stack_operator,
)

class LlamaStackDepsTest(unittest.TestCase):
    def test_full_matrix_csv_requires_llama_when_enabled(self) -> None:
        with patch(
            "install.llama_stack_deps._llama_stack_enabled_for_current_version",
            return_value=True,
        ):
            self.assertTrue(components_csv_requires_llama_stack("workbenches,llama_stack"))

    def test_full_matrix_csv_skips_llama_when_version_gated(self) -> None:
        with patch(
            "install.llama_stack_deps._llama_stack_enabled_for_current_version",
            return_value=False,
        ):
            self.assertFalse(components_csv_requires_llama_stack("workbenches,llama_stack,ogx"))

    @patch("install.llama_stack_deps.ensure_dsc_component_managed")
    @patch("install.llama_stack_deps._cr_exists", return_value=True)
    def test_try_prepare_skips_when_version_gated(self, _cr, managed) -> None:
        with patch(
            "install.llama_stack_deps._llama_stack_enabled_for_current_version",
            return_value=False,
        ):
            self.assertFalse(try_prepare_llama_stack_operator(timeout_sec=10))
        managed.assert_not_called()

if __name__ == "__main__":
    raise SystemExit(unittest.main())
