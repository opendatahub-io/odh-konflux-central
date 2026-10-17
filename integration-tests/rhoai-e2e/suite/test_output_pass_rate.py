"""Pass-rate tiers for test-finalize Konflux TEST_OUTPUT coloring (component aggregate)."""

from __future__ import annotations

SUCCESS_MIN_PASS_RATE = 0.99
UNSTABLE_MIN_PASS_RATE = 0.80


def gate_pass_rate(*, passed: int, failed: int, skipped: int = 0) -> float | None:
    """Share of executed tests passed: passed / (passed + failed).

    Skips are excluded — they were not run, so they do not affect stability rate.
    """
    _ = skipped
    executed = passed + failed
    if executed <= 0:
        return None
    return passed / executed


def classify_result_by_pass_rate(*, passed: int, failed: int, skipped: int = 0) -> str:
    """Map aggregate counts to Konflux TEST_OUTPUT result for test-finalize.

    Rate uses executed tests only (passed + failed). Skips do not change the rate.

    - >= 99% of executed passed: SUCCESS (green)
    - >= 80% of executed passed: WARNING (yellow / unstable)
    - < 80% or nothing executed: FAILURE (red)
    """
    rate = gate_pass_rate(passed=passed, failed=failed, skipped=skipped)
    if rate is None or rate < UNSTABLE_MIN_PASS_RATE:
        return "FAILURE"
    if rate >= SUCCESS_MIN_PASS_RATE:
        return "SUCCESS"
    return "WARNING"


def _gate_test_counts(data: dict[str, object]) -> tuple[int, int, int]:
    passed = int(data.get("successes", data.get("passed", 0)) or 0)
    failed = int(data.get("failures", 0) or 0)
    skipped = int(data.get("skipped", 0) or 0)
    return passed, failed, skipped


def gate_test_output_with_pass_rate_result(data: dict[str, object]) -> dict[str, object]:
    """Return a copy of gate TEST_OUTPUT with ``result`` from pass-rate tiers."""
    passed, failed, skipped = _gate_test_counts(data)
    out = dict(data)
    out["result"] = classify_result_by_pass_rate(
        passed=passed,
        failed=failed,
        skipped=skipped,
    )
    return out
