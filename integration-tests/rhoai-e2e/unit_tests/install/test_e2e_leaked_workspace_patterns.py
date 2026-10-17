"""Workspace namespace leak patterns."""

from __future__ import annotations

import unittest

from install.e2e_leaked_namespace_patterns import matches_leaked_component_test_namespace


class TestWorkspaceLeakPatterns(unittest.TestCase):
    def test_workspace_names_match(self) -> None:
        self.assertTrue(matches_leaked_component_test_namespace("workspace1-0c4g28sb"))
        self.assertTrue(matches_leaked_component_test_namespace("workspace2-qf7ho4kp"))
        self.assertFalse(matches_leaked_component_test_namespace("workspace"))
