#!/usr/bin/env python3
"""Tekton step: wait for conforma on the same Snapshot, then pass or skip e2e smoke."""

from __future__ import annotations

import os
import sys
from pathlib import Path

from _bootstrap import ensure_rhoai_e2e_path

ensure_rhoai_e2e_path()

from runners.report.pipeline_test_outputs import konflux_conforma_skip_test_output_json
from suite.conforma_gate import (
    CONFORMA_GATE_PASS,
    CONFORMA_GATE_SKIP,
    decide_conforma_gate_without_wait,
    poll_conforma_gate,
    resolve_snapshot_name,
    should_wait_for_conforma,
)
from suite.snapshot_catalog_line import (
    catalog_line_from_snapshot_metadata,
    catalog_line_meets_min_version,
)
from steps.tekton_incluster import (
    fetch_snapshot_metadata,
    list_pipelineruns_for_snapshot,
    pipeline_run_snapshot_label,
)
from steps.tekton_util import write_result

_CONFORMA_SKIP_SIDECAR = ".rhoai-e2e-conforma-skip-test-output.json"


def _format_conforma_task_message(*, gate_label: str, detail: str) -> str:
    """Format Konflux TASK_MESSAGE lines for the conforma gate task.

    ``gate_label`` is the value shown in TASK_MESSAGE (``pass`` or ``skip``). On CLI /
    ``--run-its`` bypass the Tekton ``CONFORMA_GATE`` result stays ``pass`` so downstream
    ``when`` clauses run, while TASK_MESSAGE uses ``skip`` to show conforma was not waited on.
    Real gate failures (min-RHOAI, conforma fail/timeout) set both result and message to ``skip``.
    """
    detail = detail.strip()
    if gate_label == CONFORMA_GATE_SKIP and detail.lower().startswith("skipped:"):
        head = "Partial pass"
    else:
        head = "Succeeded"
    lines = [f"wait-for-conforma: {head}", f"CONFORMA_GATE={gate_label}."]
    if detail:
        lines.append(detail)
    return "\n".join(lines)


def _write_conforma_task_message(
    task_message_path: str,
    *,
    gate_label: str,
    detail: str,
) -> None:
    if not task_message_path:
        return
    write_result(
        task_message_path,
        _format_conforma_task_message(gate_label=gate_label, detail=detail),
    )


def _product_is_existing(product: str) -> bool:
    from suite.constants import is_test_only_product

    return is_test_only_product(product)


def _sidecar_path() -> Path | None:
    raw = os.environ.get("TESTS_PAYLOAD_DIR", "").strip()
    if not raw:
        return None
    return Path(raw) / _CONFORMA_SKIP_SIDECAR


def _write_sidecar(text: str) -> None:
    path = _sidecar_path()
    if path is None or not text.strip():
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text.strip() + "\n", encoding="utf-8")


def _resolve_snapshot_name() -> str:
    return resolve_snapshot_name(
        os.environ.get("SNAPSHOT", ""),
        snapshot_name_env=os.environ.get("SNAPSHOT_NAME", ""),
        pipeline_run_snapshot_label_fn=pipeline_run_snapshot_label,
    )


def _min_rhoai_skip_note(*, product: str, snapshot_name: str, namespace: str) -> str | None:
    """When the triggering Snapshot is below MIN_RHOAI_VERSION, return a skip message."""
    if _product_is_existing(product):
        return None
    snap = (snapshot_name or "").strip()
    if not snap:
        return None
    min_version = (os.environ.get("MIN_RHOAI_VERSION") or "3.5").strip() or "3.5"
    labels, annotations = fetch_snapshot_metadata(snap, namespace)
    catalog_line = catalog_line_from_snapshot_metadata(labels, annotations)
    if not catalog_line or catalog_line_meets_min_version(catalog_line, min_version):
        return None
    return (
        f"Skipped: snapshot catalog line {catalog_line!r} below MIN_RHOAI_VERSION "
        f"{min_version} — e2e smoke not run"
    )


def _emit_skip(
    *,
    gate_path: str,
    test_output_path: str,
    task_message_path: str,
    note: str,
) -> int:
    print(f"⚠ {note}", flush=True)
    write_result(gate_path, CONFORMA_GATE_SKIP)
    warning_json = konflux_conforma_skip_test_output_json(note=note)
    if test_output_path:
        write_result(test_output_path, warning_json)
    _write_sidecar(warning_json)
    _write_conforma_task_message(
        task_message_path,
        gate_label=CONFORMA_GATE_SKIP,
        detail=note,
    )
    return 0


def main() -> int:
    gate_path = os.environ.get("CONFORMA_GATE_PATH", "").strip()
    test_output_path = os.environ.get("TEST_OUTPUT_PATH", "").strip()
    task_message_path = os.environ.get("TASK_MESSAGE_PATH", "").strip()
    if not gate_path:
        print("CONFORMA_GATE_PATH is required", file=sys.stderr)
        return 1

    wait_raw = os.environ.get("WAIT_FOR_CONFORMA", "true")
    product = os.environ.get("PRODUCT", "")
    snapshot_name = _resolve_snapshot_name()
    namespace = os.environ.get("PIPELINE_NAMESPACE", "").strip()
    timeout_sec = int(os.environ.get("CONFORMA_GATE_TIMEOUT_SEC", "1800").strip() or "1800")

    min_rhoai_note = _min_rhoai_skip_note(
        product=product,
        snapshot_name=snapshot_name,
        namespace=namespace,
    )
    if min_rhoai_note:
        return _emit_skip(
            gate_path=gate_path,
            test_output_path=test_output_path,
            task_message_path=task_message_path,
            note=min_rhoai_note,
        )

    wait_enabled = (wait_raw or "true").strip().lower() in ("1", "true", "yes")
    if wait_enabled and not _product_is_existing(product) and not (snapshot_name or "").strip():
        note = "Skipped: conforma wait requires a Snapshot name — e2e smoke not run"
        return _emit_skip(
            gate_path=gate_path,
            test_output_path=test_output_path,
            task_message_path=task_message_path,
            note=note,
        )

    if not should_wait_for_conforma(
        wait_for_conforma=wait_raw,
        product=product,
        snapshot_name=snapshot_name,
    ):
        decision = decide_conforma_gate_without_wait(
            wait_for_conforma=wait_raw,
            product=product,
            snapshot_name=snapshot_name,
        )
        print(f"✓ Conforma gate bypassed ({decision.reason})")
        write_result(gate_path, CONFORMA_GATE_PASS)
        _write_conforma_task_message(
            task_message_path,
            gate_label=CONFORMA_GATE_SKIP,
            detail=f"bypassed ({decision.reason})",
        )
        return 0

    print(f"Waiting for conforma on snapshot {snapshot_name!r} (timeout {timeout_sec}s)...")

    list_errors: list[str] = []

    decision = poll_conforma_gate(
        snapshot_name=snapshot_name,
        list_runs=lambda snap: list_pipelineruns_for_snapshot(snap, namespace, error_out=list_errors),
        timeout_sec=timeout_sec,
        list_errors=list_errors,
    )

    write_result(gate_path, decision.gate)
    if decision.gate == CONFORMA_GATE_PASS:
        runs = ", ".join(decision.conforma_runs[:3]) if decision.conforma_runs else "(none)"
        print(f"✓ conforma passed ({runs}); CONFORMA_GATE=pass")
        _write_conforma_task_message(
            task_message_path,
            gate_label=CONFORMA_GATE_PASS,
            detail=f"conforma passed ({runs})",
        )
        return 0

    note = decision.note or f"Skipped: conforma gate ({decision.reason})"
    return _emit_skip(
        gate_path=gate_path,
        test_output_path=test_output_path,
        task_message_path=task_message_path,
        note=note,
    )


if __name__ == "__main__":
    raise SystemExit(main())
