"""Fail publish-results when requested BVT/smoke/install gates never executed.

Uses in-cluster PipelineRun TaskRun state so hollow green runs fail at
publish-results instead of reporting PASSED.

Only intentional min-RHOAI conforma skips (``CONFORMA_GATE=skip`` with a catalog line
below ``MIN_RHOAI_VERSION``) bypass this check. Conforma fail/timeout skips still
block requested gates here so publish-results cannot finish green without running tests.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

from runners.report.junit_suite_report import (
    GATE_NOT_RUN_SUMMARY,
    augment_publish_gate_note,
    format_gate_not_run_ui_line,
    is_gate_summary_placeholder,
)
from steps.pipeline_task_state import pipeline_task_execution_state, require_pipeline_tasks_ran
from steps.tekton_incluster import (
    list_taskruns_in_cluster,
    namespace_from_env,
    pipeline_run_name_from_env,
    result_map,
    task_name,
)
from suite.conforma_gate import CONFORMA_GATE_SKIP


def _requested_gates() -> set[str]:
    raw = (os.environ.get("TEST_GATES") or "").strip()
    if not raw:
        return set()
    return {g.strip().lower() for g in raw.split(",") if g.strip()}


_VERIFY_OPERATOR_READY_TASK = "verify-operator-ready"


def _install_tasks_for_product(product: str) -> tuple[str, ...]:
    p = (product or "").strip().lower()
    from suite.constants import is_test_only_product

    if is_test_only_product(p):
        return ()
    if p == "rhoai":
        return ("install-dep-operators", "install-rhoai")
    if p == "odh":
        return ("install-dep-operators", "install-odh")
    return ("install-dep-operators", "install-rhoai", "install-odh")


def read_gate_result(path: str) -> str:
    p = Path(path)
    if not p.is_file():
        return ""
    return p.read_text(encoding="utf-8").strip()


def _gate_result_ok(gate_val: str) -> bool:
    return bool(gate_val) and not is_gate_summary_placeholder(gate_val) and gate_val.strip() != GATE_NOT_RUN_SUMMARY


def _normalize_conforma_gate(raw: str | None) -> str:
    return (raw or "").strip().lower()


def _conforma_taskrun_results(*, pipeline_run: str, namespace: str) -> dict[str, str]:
    if not pipeline_run or not namespace:
        return {}
    for tr in list_taskruns_in_cluster(pipeline_run, namespace):
        if task_name(tr) == "wait-for-conforma":
            return result_map(tr)
    return {}


def _conforma_gate_from_taskrun(*, pipeline_run: str, namespace: str) -> str:
    """Read wait-for-conforma CONFORMA_GATE when the pipeline param was not wired."""
    return _normalize_conforma_gate(
        _conforma_taskrun_results(pipeline_run=pipeline_run, namespace=namespace).get(
            "CONFORMA_GATE"
        )
    )


def _conforma_skip_detail(
    *,
    conforma_gate: str | None = None,
    pipeline_run: str = "",
    namespace: str = "",
) -> str:
    gate = _normalize_conforma_gate(
        conforma_gate if conforma_gate is not None else os.environ.get("CONFORMA_GATE")
    )
    if gate != CONFORMA_GATE_SKIP:
        return ""
    detail = (os.environ.get("CONFORMA_GATE_DETAIL") or "").strip()
    if detail:
        return detail
    pr = (pipeline_run or pipeline_run_name_from_env()).strip()
    ns = (namespace or namespace_from_env()).strip()
    return _conforma_taskrun_results(pipeline_run=pr, namespace=ns).get("TASK_MESSAGE", "")


def upstream_blocked_test_gates(
    *,
    test_gates: str | None = None,
    product: str | None = None,
    pipeline_run: str = "",
    namespace: str = "",
) -> list[str]:
    """Return install/verify failures that legitimately prevented bvt/smoke from running."""
    gates = _requested_gates() if test_gates is None else {
        g.strip().lower() for g in (test_gates or "").split(",") if g.strip()
    }
    if not gates:
        return []

    prod = (product if product is not None else os.environ.get("PRODUCT") or "").strip().lower()
    pr_name = (pipeline_run or pipeline_run_name_from_env()).strip()
    ns = (namespace or namespace_from_env()).strip()
    if not (pr_name and ns):
        return []

    blockers: list[str] = []
    for task in _install_tasks_for_product(prod):
        state, detail = pipeline_task_execution_state(
            task,
            pipeline_run=pr_name,
            namespace=ns,
        )
        if state == "failed":
            blockers.append(f"{task}: failed")
        elif state == "skipped":
            blockers.append(f"{task}: skipped ({detail or 'when false'})")

    from suite.constants import product_installs_operator

    if product_installs_operator(prod):
        state, detail = pipeline_task_execution_state(
            _VERIFY_OPERATOR_READY_TASK,
            pipeline_run=pr_name,
            namespace=ns,
        )
        if state == "failed":
            blockers.append(f"{_VERIFY_OPERATOR_READY_TASK}: failed")
        elif state == "skipped":
            blockers.append(f"{_VERIFY_OPERATOR_READY_TASK}: skipped ({detail or 'when false'})")

    return blockers


def format_install_blocked_publish_note(
    blockers: list[str],
    *,
    test_gates: str = "",
) -> str:
    """Human-readable publish-results note when install/verify blocked requested gates."""
    head = "; ".join(block.strip() for block in blockers if block.strip())
    gate_lines = [
        format_gate_not_run_ui_line(gate)
        for gate in sorted(_requested_gates() if not test_gates else {
            g.strip().lower() for g in test_gates.split(",") if g.strip()
        })
    ]
    note = head
    if gate_lines:
        note = f"{note}\n" + "\n".join(gate_lines) if note else "\n".join(gate_lines)
    return augment_publish_gate_note(
        note,
        test_gates=test_gates,
        gate_summaries={},
    )


def _only_expected_gate_skip_failures(failures: list[str]) -> bool:
    if not failures:
        return False
    for line in failures:
        text = line.lower()
        if "placeholder" in text or "did not execute" in text or "missing or placeholder" in text:
            continue
        return False
    return True


def intentional_conforma_e2e_skip(
    *,
    conforma_gate: str | None = None,
    pipeline_run: str = "",
    namespace: str = "",
) -> bool:
    """True only when wait-for-conforma skipped e2e for catalog line below MIN_RHOAI_VERSION."""
    gate = _normalize_conforma_gate(
        conforma_gate if conforma_gate is not None else os.environ.get("CONFORMA_GATE")
    )
    if not gate:
        pr = (pipeline_run or pipeline_run_name_from_env()).strip()
        ns = (namespace or namespace_from_env()).strip()
        gate = _conforma_gate_from_taskrun(pipeline_run=pr, namespace=ns)
    if gate != CONFORMA_GATE_SKIP:
        return False
    detail = _conforma_skip_detail(
        conforma_gate=gate,
        pipeline_run=pipeline_run,
        namespace=namespace,
    )
    return "below MIN_RHOAI_VERSION" in detail


def conforma_skip_blocked_requested_gates(
    *,
    conforma_gate: str | None = None,
    pipeline_run: str = "",
    namespace: str = "",
) -> str:
    """Return a failure line when conforma skip blocked requested gates (not min-RHOAI)."""
    gate = _normalize_conforma_gate(
        conforma_gate if conforma_gate is not None else os.environ.get("CONFORMA_GATE")
    )
    if not gate:
        pr = (pipeline_run or pipeline_run_name_from_env()).strip()
        ns = (namespace or namespace_from_env()).strip()
        gate = _conforma_gate_from_taskrun(pipeline_run=pr, namespace=ns)
    if gate != CONFORMA_GATE_SKIP or intentional_conforma_e2e_skip(
        conforma_gate=gate,
        pipeline_run=pipeline_run,
        namespace=namespace,
    ):
        return ""
    detail = _conforma_skip_detail(
        conforma_gate=gate,
        pipeline_run=pipeline_run,
        namespace=namespace,
    ).strip()
    if detail:
        return f"conforma gate skipped e2e: {detail.splitlines()[0]}"
    return "conforma gate skipped e2e (requested TEST_GATES did not run)"


def collect_hollow_green_failures(
    *,
    test_gates: str | None = None,
    product: str | None = None,
    gate_values: dict[str, str] | None = None,
    conforma_gate: str | None = None,
) -> list[str]:
    """Return human-readable failure lines when requested gates did not run."""
    gates = _requested_gates() if test_gates is None else {
        g.strip().lower() for g in (test_gates or "").split(",") if g.strip()
    }
    if not gates:
        return []

    prod = (product if product is not None else os.environ.get("PRODUCT") or "").strip().lower()
    pr_name = pipeline_run_name_from_env().strip()
    ns = namespace_from_env().strip()
    has_cluster = bool(pr_name and ns)

    if intentional_conforma_e2e_skip(
        conforma_gate=conforma_gate,
        pipeline_run=pr_name,
        namespace=ns,
    ):
        return []

    conforma_failure = conforma_skip_blocked_requested_gates(
        conforma_gate=conforma_gate,
        pipeline_run=pr_name,
        namespace=ns,
    )
    if conforma_failure:
        return [conforma_failure]

    install_failures: list[str] = []
    gate_failures: list[str] = []
    install_tasks = _install_tasks_for_product(prod)
    if has_cluster:
        install_failures.extend(
            require_pipeline_tasks_ran(
                install_tasks,
                pipeline_run=pr_name,
                namespace=ns,
                allow_failed=True,
            )
        )
    else:
        for task in install_tasks:
            install_failures.append(f"{task}: cannot verify execution (no PipelineRun context)")

    for gate in sorted(gates):
        gate_key = f"{gate.upper()}_GATE"
        gate_val = ""
        if gate_values:
            gate_val = (gate_values.get(gate_key) or "").strip()
        if not gate_val:
            gate_path = os.environ.get(f"{gate.upper()}_GATE_PATH", "")
            if gate_path:
                gate_val = read_gate_result(gate_path)

        task_ok = False
        if has_cluster:
            task_errors = require_pipeline_tasks_ran(
                (f"run-{gate}-tests",),
                pipeline_run=pr_name,
                namespace=ns,
                allow_failed=True,
            )
            task_ok = not task_errors
        gate_ok = _gate_result_ok(gate_val)

        if task_ok or gate_ok:
            continue
        if gate_val and not gate_ok:
            gate_failures.append(f"{gate} gate result is placeholder: {gate_val!r}")
        elif has_cluster:
            gate_failures.append(
                f"{gate}: run-{gate}-tests did not execute and no gate result published"
            )
        else:
            gate_failures.append(
                f"{gate}: no PipelineRun context and gate result missing or placeholder"
            )

    blockers = upstream_blocked_test_gates(
        test_gates=test_gates,
        product=product,
        pipeline_run=pr_name,
        namespace=ns,
    )
    if blockers and _only_expected_gate_skip_failures(gate_failures):
        install_blockers = [
            blocker
            for blocker in blockers
            if any(blocker.startswith(f"{task}:") for task in install_tasks)
        ]
        if install_blockers:
            # Install/dep failure explains missing gate TaskRuns; do not report hollow green.
            return install_failures
        deduped: list[str] = []
        seen: set[str] = set()
        for item in install_failures + blockers:
            if item in seen:
                continue
            seen.add(item)
            deduped.append(item)
        return deduped

    return install_failures + gate_failures


def main() -> int:
    if intentional_conforma_e2e_skip():
        print(
            "CONFORMA_GATE=skip below MIN_RHOAI_VERSION - e2e intentionally not run; "
            "skipping hollow-green check"
        )
        return 0
    failures = collect_hollow_green_failures()
    if failures:
        print("Requested gates did not run:", file=sys.stderr)
        for line in failures:
            print(f"  - {line}", file=sys.stderr)
        return 1
    print("All requested gates executed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
