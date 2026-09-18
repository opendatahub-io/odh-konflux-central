"""Unit tests for wait-for-conforma Tekton step (min-RHOAI gate)."""

from __future__ import annotations

import os
import unittest
from unittest import mock

from steps import wait_for_conforma_gate as gate


_LABELS_225 = {
    "pac.test.appstudio.openshift.io/original-prname": "rhoai-fbc-fragment-rhoai-225-ocp-421-on-push",
}
_ANNOTATIONS_225 = {
    "pac.test.appstudio.openshift.io/sha-title": "Patching the stage catalog with rhoai-2.25",
}
_LABELS_35 = {
    "pac.test.appstudio.openshift.io/original-prname": "rhoai-fbc-fragment-rhoai-35-ea2-ocp-421-on-push",
}
_ANNOTATIONS_35 = {
    "pac.test.appstudio.openshift.io/sha-title": "Patching the stage catalog with rhoai-3.5-ea.2",
}


class WaitForConformaGateTest(unittest.TestCase):
    def _assert_wrote_skip_task_message(self, write_result: mock.MagicMock) -> None:
        wrote_skip = any(
            len(call.args) >= 2 and "CONFORMA_GATE=skip" in str(call.args[1])
            for call in write_result.call_args_list
        )
        self.assertTrue(wrote_skip)

    def test_format_conforma_task_message_bypass_shows_skip_label(self) -> None:
        msg = gate._format_conforma_task_message(
            gate_label=gate.CONFORMA_GATE_SKIP,
            detail="bypassed (gate_disabled)",
        )
        self.assertIn("CONFORMA_GATE=skip", msg)
        self.assertIn("Succeeded", msg)
        self.assertIn("bypassed (gate_disabled)", msg)

    def test_format_conforma_task_message_pass(self) -> None:
        msg = gate._format_conforma_task_message(
            gate_label=gate.CONFORMA_GATE_PASS,
            detail="conforma passed (ec-run-1)",
        )
        self.assertIn("CONFORMA_GATE=pass", msg)
        self.assertIn("ec-run-1", msg)

    def test_format_conforma_task_message_skipped_note_uses_partial_pass(self) -> None:
        msg = gate._format_conforma_task_message(
            gate_label=gate.CONFORMA_GATE_SKIP,
            detail="Skipped: conforma failed (ec-run-1) — e2e smoke not run",
        )
        self.assertIn("Partial pass", msg)
        self.assertIn("CONFORMA_GATE=skip", msg)

    @mock.patch.dict(
        os.environ,
        {
            "WAIT_FOR_CONFORMA": "false",
            "CONFORMA_GATE_PATH": "/tmp/conforma-gate",
            "TASK_MESSAGE_PATH": "/tmp/conforma-task-message",
            "PRODUCT": "rhoai",
            "SNAPSHOT_NAME": "snap-1",
        },
        clear=False,
    )
    @mock.patch.object(gate, "write_result")
    @mock.patch.object(gate, "_resolve_snapshot_name", return_value="snap-1")
    def test_main_bypass_writes_skip_in_task_message(
        self,
        _resolve_snapshot: mock.MagicMock,
        write_result: mock.MagicMock,
    ) -> None:
        self.assertEqual(gate.main(), 0)
        write_result.assert_any_call(mock.ANY, gate.CONFORMA_GATE_PASS)
        self._assert_wrote_skip_task_message(write_result)

    def test_min_rhoai_skip_note_below_minimum(self) -> None:
        with mock.patch.object(
            gate,
            "fetch_snapshot_metadata",
            return_value=(_LABELS_225, _ANNOTATIONS_225),
        ):
            note = gate._min_rhoai_skip_note(
                product="rhoai",
                snapshot_name="rhoai-fbc-fragment-ocp-421-20260714-164825-000",
                namespace="rhoai-tenant",
            )
        self.assertIsNotNone(note)
        self.assertIn("2.25", note or "")
        self.assertIn("3.5", note or "")

    def test_min_rhoai_skip_note_passes_35_ea2(self) -> None:
        with mock.patch.object(
            gate,
            "fetch_snapshot_metadata",
            return_value=(_LABELS_35, _ANNOTATIONS_35),
        ):
            note = gate._min_rhoai_skip_note(
                product="rhoai",
                snapshot_name="rhoai-fbc-fragment-ocp-421-20260703-121508-000",
                namespace="rhoai-tenant",
            )
        self.assertIsNone(note)

    @mock.patch.dict(
        os.environ,
        {
            "MIN_RHOAI_VERSION": "3.5",
            "CONFORMA_GATE_PATH": "/tmp/conforma-gate",
            "TASK_MESSAGE_PATH": "/tmp/conforma-task-message",
            "PIPELINE_NAMESPACE": "rhoai-tenant",
            "PRODUCT": "rhoai",
        },
        clear=False,
    )
    @mock.patch.object(gate, "_write_sidecar")
    @mock.patch.object(gate, "write_result")
    @mock.patch.object(
        gate,
        "fetch_snapshot_metadata",
        return_value=(_LABELS_225, _ANNOTATIONS_225),
    )
    @mock.patch.object(gate, "_resolve_snapshot_name", return_value="snap-225")
    def test_main_skips_e2e_for_225_snapshot(
        self,
        _resolve_snapshot: mock.MagicMock,
        _fetch_metadata: mock.MagicMock,
        write_result: mock.MagicMock,
        _write_sidecar: mock.MagicMock,
    ) -> None:
        self.assertEqual(gate.main(), 0)
        write_result.assert_any_call(mock.ANY, gate.CONFORMA_GATE_SKIP)
        self._assert_wrote_skip_task_message(write_result)


if __name__ == "__main__":
    unittest.main()
