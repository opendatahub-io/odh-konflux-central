"""Tests for Konflux TASK_MESSAGE line formatting."""

from __future__ import annotations

import unittest

from suite.konflux_task_message import format_konflux_task_message


class KonfluxTaskMessageFormatTest(unittest.TestCase):
    def test_splits_status_and_hint(self) -> None:
        msg = format_konflux_task_message(
            "provision-ephc-space: Succeeded - secretRef=my-space-secret",
        )
        self.assertEqual(
            msg,
            "provision-ephc-space: Succeeded.\nsecretRef=my-space-secret.",
        )

    def test_multiline_run_context(self) -> None:
        msg = format_konflux_task_message(
            "Trigger: CLI direct (manual trigger)\n"
            "Event: Incoming — CLI direct PipelineRun\n"
            "FBC: rhoai-fbc-fragment-ocp-421 @ sha256:abc123…",
        )
        self.assertEqual(
            msg.splitlines(),
            [
                "Trigger: CLI direct (manual trigger).",
                "Event: Incoming — CLI direct PipelineRun.",
                "FBC: rhoai-fbc-fragment-ocp-421 @ sha256:abc123….",
            ],
        )

    def test_failure_keeps_detail_on_one_line(self) -> None:
        msg = format_konflux_task_message(
            "install-ocp-cluster: Failed - step-prepare-kubeconfig - Error",
        )
        self.assertEqual(
            msg,
            "install-ocp-cluster: Failed - step-prepare-kubeconfig - Error.",
        )

    def test_splits_semicolon_clauses(self) -> None:
        msg = format_konflux_task_message("a; b")
        self.assertEqual(msg, "a.\nb.")


if __name__ == "__main__":
    unittest.main()
