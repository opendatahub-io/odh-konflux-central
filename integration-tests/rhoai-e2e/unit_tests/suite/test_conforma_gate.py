"""Unit tests for conforma gate (no cluster)."""

from __future__ import annotations

import unittest

from suite.conforma_gate import (
    CONFORMA_GATE_PASS,
    CONFORMA_GATE_SKIP,
    decide_conforma_gate_without_wait,
    evaluate_conforma_runs,
    is_enterprise_contract_pipelinerun,
    poll_conforma_gate,
    resolve_snapshot_name,
    should_wait_for_conforma,
    snapshot_name_from_snapshot_param,
)


def _pr(name: str, *, succeeded: str | None, ec: bool = True) -> dict:
    labels = {"test.appstudio.openshift.io/kind": "enterprise-contract"} if ec else {}
    item: dict = {"metadata": {"name": name, "labels": labels}}
    if succeeded is not None:
        item["status"] = {
            "conditions": [{"type": "Succeeded", "status": succeeded, "reason": "Completed"}]
        }
    return item


class ConformaGateTest(unittest.TestCase):
    def test_snapshot_name_from_param(self) -> None:
        raw = '{"metadata":{"name":"rhoai-fbc-fragment-ocp-420-on-push-abc"}}'
        self.assertEqual(snapshot_name_from_snapshot_param(raw), "rhoai-fbc-fragment-ocp-420-on-push-abc")

    def test_resolve_snapshot_name_spec_only_uses_env(self) -> None:
        spec_only = '{"application":"rhoai-fbc-fragment-ocp-421","components":[]}'
        self.assertEqual(
            resolve_snapshot_name(
                spec_only,
                snapshot_name_env="rhoai-fbc-fragment-ocp-421-20260714-164825-000",
            ),
            "rhoai-fbc-fragment-ocp-421-20260714-164825-000",
        )

    def test_resolve_snapshot_name_ignores_unresolved_tekton_env(self) -> None:
        self.assertEqual(
            resolve_snapshot_name(
                '{"application":"app"}',
                snapshot_name_env="$(context.pipelineRun.labels['appstudio.openshift.io/snapshot'])",
                pipeline_run_snapshot_label_fn=lambda: "snap-from-plr-label",
            ),
            "snap-from-plr-label",
        )

    def test_resolve_snapshot_name_prefers_metadata_over_env(self) -> None:
        raw = '{"metadata":{"name":"snap-from-param"}}'
        self.assertEqual(
            resolve_snapshot_name(raw, snapshot_name_env="snap-from-label"),
            "snap-from-param",
        )

    def test_resolve_snapshot_name_falls_back_to_label_reader(self) -> None:
        self.assertEqual(
            resolve_snapshot_name(
                '{"application":"app"}',
                pipeline_run_snapshot_label_fn=lambda: "snap-from-plr-label",
            ),
            "snap-from-plr-label",
        )

    def test_should_wait_requires_snapshot(self) -> None:
        self.assertFalse(
            should_wait_for_conforma(wait_for_conforma="true", product="rhoai", snapshot_name="")
        )
        self.assertFalse(
            should_wait_for_conforma(
                wait_for_conforma="true", product="", snapshot_name="snap-1"
            )
        )
        self.assertTrue(
            should_wait_for_conforma(
                wait_for_conforma="true", product="rhoai", snapshot_name="snap-1"
            )
        )

    def test_is_enterprise_contract_by_scenario_name(self) -> None:
        item = {
            "metadata": {
                "name": "conforma-fbc-rhoai-prod-ocp-420-xyz",
                "labels": {"test.appstudio.openshift.io/scenario": "conforma-fbc-rhoai-prod-ocp-420"},
            }
        }
        self.assertTrue(is_enterprise_contract_pipelinerun(item))

    def test_evaluate_pending(self) -> None:
        self.assertIsNone(
            evaluate_conforma_runs([_pr("ec-1", succeeded=None), _pr("olm-1", succeeded="True", ec=False)])
        )

    def test_evaluate_failed(self) -> None:
        decision = evaluate_conforma_runs(
            [_pr("ec-1", succeeded="False"), _pr("ec-2", succeeded="True")]
        )
        self.assertIsNotNone(decision)
        assert decision is not None
        self.assertEqual(decision.gate, CONFORMA_GATE_SKIP)
        self.assertIn("conforma failed", decision.note)

    def test_evaluate_passed(self) -> None:
        decision = evaluate_conforma_runs([_pr("ec-1", succeeded="True")])
        self.assertIsNotNone(decision)
        assert decision is not None
        self.assertEqual(decision.gate, CONFORMA_GATE_PASS)

    def test_poll_timeout_skip(self) -> None:
        decision = poll_conforma_gate(
            snapshot_name="snap-1",
            list_runs=lambda _snap: [],
            timeout_sec=1,
            sleep_fn=lambda _s: None,
            monotonic_fn=_fake_monotonic([0.0, 0.0, 2.0]),
        )
        self.assertEqual(decision.gate, CONFORMA_GATE_SKIP)
        self.assertEqual(decision.reason, "conforma_timeout")

    def test_poll_empty_snapshot_skips(self) -> None:
        decision = poll_conforma_gate(
            snapshot_name="",
            list_runs=lambda _snap: [],
            timeout_sec=1,
        )
        self.assertEqual(decision.gate, CONFORMA_GATE_SKIP)
        self.assertEqual(decision.reason, "no_snapshot")

    def test_poll_list_error_skips_after_timeout(self) -> None:
        errors: list[str] = []
        decision = poll_conforma_gate(
            snapshot_name="snap-1",
            list_runs=lambda _snap: errors.append("ERROR: list PipelineRuns for snapshot snap-1: forbidden") or [],
            timeout_sec=1,
            sleep_fn=lambda _s: None,
            monotonic_fn=_fake_monotonic([0.0, 0.0, 2.0]),
            list_errors=errors,
        )
        self.assertEqual(decision.gate, CONFORMA_GATE_SKIP)
        self.assertEqual(decision.reason, "pipelinerun_list_failed")

    def test_poll_transient_list_error_retries(self) -> None:
        errors: list[str] = []
        calls = {"n": 0}

        def list_runs(_snap: str) -> list:
            calls["n"] += 1
            if calls["n"] == 1:
                errors.append("ERROR: transient")
                return []
            return [_pr("ec-1", succeeded="True")]

        decision = poll_conforma_gate(
            snapshot_name="snap-1",
            list_runs=list_runs,
            timeout_sec=30,
            sleep_fn=lambda _s: None,
            list_errors=errors,
        )
        self.assertEqual(decision.gate, CONFORMA_GATE_PASS)
        self.assertEqual(decision.reason, "conforma_passed")


class DecideConformaGateWithoutWaitTest(unittest.TestCase):
    def test_explicit_bypass_wait_disabled(self) -> None:
        decision = decide_conforma_gate_without_wait(
            wait_for_conforma="false",
            product="rhoai",
            snapshot_name="",
        )
        self.assertEqual(decision.gate, CONFORMA_GATE_PASS)
        self.assertEqual(decision.reason, "gate_disabled")

    def test_explicit_bypass_existing_product(self) -> None:
        decision = decide_conforma_gate_without_wait(
            wait_for_conforma="true",
            product="",
            snapshot_name="",
        )
        self.assertEqual(decision.gate, CONFORMA_GATE_PASS)
        self.assertEqual(decision.reason, "gate_disabled")

    def test_missing_snapshot_not_bypass(self) -> None:
        decision = decide_conforma_gate_without_wait(
            wait_for_conforma="true",
            product="rhoai",
            snapshot_name="",
        )
        self.assertEqual(decision.gate, CONFORMA_GATE_SKIP)
        self.assertEqual(decision.reason, "no_snapshot")


def _fake_monotonic(values: list[float]):
    it = iter(values)

    def _fn() -> float:
        try:
            return next(it)
        except StopIteration:
            return values[-1]

    return _fn


if __name__ == "__main__":
    unittest.main()
