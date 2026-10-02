"""Tests for workspace-only pipeline TEST_OUTPUT emission."""

from __future__ import annotations

import json
from pathlib import Path

from runners.report.emit_pipeline_test_output_from_workspace import main

def test_emit_from_workspace_sidecars(tmp_path: Path, monkeypatch) -> None:
    smoke = {
        "result": "WARNING",
        "successes": 114,
        "failures": 30,
        "skipped": 83,
        "note": "smoke: 50% pass rate (114 passed, 30 failed, 83 skipped)",
    }
    bvt = {
        "result": "SUCCESS",
        "successes": 5,
        "failures": 0,
        "skipped": 0,
        "note": "bvt: 100% pass rate (5 passed, 0 failed, 0 skipped)",
    }
    smoke_path = tmp_path / ".rhoai-e2e-smoke-test-output.json"
    bvt_path = tmp_path / ".rhoai-e2e-bvt-test-output.json"
    smoke_path.write_text(json.dumps(smoke), encoding="utf-8")
    bvt_path.write_text(json.dumps(bvt), encoding="utf-8")
    out_dir = tmp_path / "results"
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / "TEST_OUTPUT"

    monkeypatch.setenv("TEKTON_RESULTS_DIR", str(out_dir))

    monkeypatch.setenv("RESULT_PATH", str(out_path))
    monkeypatch.setenv("SMOKE_TEST_OUTPUT_PATH", str(smoke_path))
    monkeypatch.setenv("BVT_TEST_OUTPUT_PATH", str(bvt_path))
    monkeypatch.setenv("TEST_GATES", "bvt,smoke")

    assert main() == 0
    payload = json.loads(out_path.read_text(encoding="utf-8"))
    assert payload["successes"] == 0
    assert payload["failures"] == 0
    assert payload["warnings"] == 0
    assert payload["result"] == "SUCCESS"
    assert "smoke:" in payload["note"]
    assert "bvt:" in payload["note"]
