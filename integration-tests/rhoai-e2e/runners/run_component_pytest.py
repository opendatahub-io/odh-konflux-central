#!/usr/bin/env python3
"""Run per-component opendatahub-tests pytest (smoke and/or tier1 catalog phases).

Uses ``run_bvt_pytest.main()`` for each catalog entry. Does not invoke ods-ci or Robot.

Env (required):
    COMPONENTS_CSV       -- comma-separated component ids (canonical)
    COMPONENTS_CONFIG    -- path to rhoai-e2e-components-smoke.yaml
Env (optional):
    ARTIFACTS_DIR        -- JUnit output directory (default /artifacts)
    COMPONENT_TEST_PLAN_JSON -- JSON plan from export_component_plan.py (preferred in Tekton)
"""

from __future__ import annotations

import json
import os
import re
import shlex
import sys
import time
from pathlib import Path
from typing import Any
from xml.sax.saxutils import escape, quoteattr

from _bootstrap import ensure_rhoai_e2e_path

ensure_rhoai_e2e_path()

from suite.component_phases import combine_pytest_markers, parse_component_test_phases
from suite.component_test_timeout import (
    COMPONENT_TEST_TIMEOUT_SECS_ENV,
    apply_cluster_source_timeout_cap,
    parse_component_timeout_seconds,
    resolve_component_test_timeout_raw,
)
from suite.component_task_exit import component_exit_file_path, resolve_component_exit_codes
from suite.component_catalog import SmokeComponent, load_components_smoke_catalog
from suite.component_dsc_gate import smoke_component_prereq_unavailable
from suite.cluster_api_health import cluster_smoke_infra_blocked_reason, is_definitive_infra_error
from suite.dsc_baseline import reconcile_baseline_dsc_before_component
from install.dsc_install import components_need_models_as_service
from runners.component_prereqs import (
    cluster_prep_already_done,
    prepare_component_for_smoke,
    refresh_maas_smoke_before_pytest,
    run_pooled_external_smoke_prep,
)
from suite.component_plan import parse_components_selection
from runners.run_bvt_pytest import run_single as run_single_pytest
from k8s.vault_runtime import ensure_runtime_vault_env
from k8s.shift_left_env import (
    apply_cluster_router_ca_from_kubeconfig,
    load_shift_left_env_from_mount,
    promote_shift_left_aws_env,
    suppress_ephemeral_jira_env,
)
from ogx_ea_distribution_plugin import apply_ogx_ea_distribution_patch
from components.maas_billing.oidc_users import (
    apply_maas_billing_htpasswd_test_user_overrides,
    apply_maas_oidc_client_secret_overrides,
    maas_billing_aitenant_bootstrap_pytest_extra_args,
    maas_billing_aitenant_pytest_extra_args,
    maas_billing_ephc_bbr_pytest_extra_args,
    maas_billing_rosa_hcp_pytest_extra_args,
)
from steps.tekton_util import (
    prepare_kubeconfig_auth_for_tests,
)
from suite.its_trigger_params import (
    CLUSTER_SOURCE_EPHC,
    is_ephemeral_hosted_cluster_source,
    is_external_cluster_source,
    is_pooled_external_cluster_source,
)

# EPHC/HCP quay mirrors fail registry.redhat.io-only checks (nodeids vary by suite).
_EHC_SKIP_IMAGE_VALIDATION = "-k 'not image_validation and not verify_images'"
# Pooled HCP: tags already resolved but ImageStream importer retries quay with a stale
# robot (ImportSuccess=False). EPHC keeps this test (cxn7l: import_success=N/A).
_EXTERNAL_SKIP_IMAGESTREAM_HEALTH = "-k 'not imagestream_health'"
# Do not add `-k 'not vector_stores'` here: under smoke+`not pgvector` that empties the
# suite (A2 kbbjt: 35 deselected / 0 selected → JP4 hollow fail). Keep Jenkins catalog
# baseline only; vector_stores client-ready hangs stay FailureIgnored until embeddings fix.
_CLUSTER_SANITY_SKIP_RHOAI = "--cluster-sanity-skip-rhoai-check"


def _needs_image_validation_skip() -> bool:
    """Skip registry.redhat.io-only image checks on mirrored clusters (EPHC IDMS, HCP Kyverno).

    External ROSA HCP rewrites pulls to quay.io/rhoai; PRODUCT=rhoai cleanup/reinstall
    hits the same mirror as test-only PRODUCT, so skip for every external CLUSTER_SOURCE.
    """
    cluster_source = os.environ.get("CLUSTER_SOURCE", "").strip()
    if cluster_source == CLUSTER_SOURCE_EPHC:
        return True
    return is_external_cluster_source(cluster_source)


def _needs_imagestream_health_skip() -> bool:
    """Skip workbenches ImageStream ImportSuccess checks on pooled external clusters."""
    cluster_source = os.environ.get("CLUSTER_SOURCE", "").strip()
    if cluster_source == CLUSTER_SOURCE_EPHC:
        return False
    return is_pooled_external_cluster_source(cluster_source)


def _needs_cluster_sanity_rhoai_skip() -> bool:
    """Pooled externals without MaaS DB never reach DSC Ready; skip full-DSC pytest sanity."""
    from suite.constants import is_test_only_product

    if not is_test_only_product(os.environ.get("PRODUCT", "")):
        return False
    return is_external_cluster_source(os.environ.get("CLUSTER_SOURCE", ""))


def _needs_schedulable_nodes_wait() -> bool:
    """Re-check schedulable nodes before component pytest (EPHC guests and pooled externals)."""
    source = os.environ.get("CLUSTER_SOURCE", "").strip()
    return is_ephemeral_hosted_cluster_source(source) or is_external_cluster_source(source)


def _wait_for_schedulable_nodes_before_component_pytest() -> str:
    """Return infra message when nodes stay unschedulable; empty when OK."""
    if not _needs_schedulable_nodes_wait():
        return ""
    from components.maas_billing.timeouts import component_cluster_nodes_timeout_sec
    from steps.prepare_bvt_cluster_nodes import wait_for_schedulable_nodes_for_bvt

    timeout_sec = component_cluster_nodes_timeout_sec()
    print(
        f"Waiting for schedulable cluster nodes before pytest ({timeout_sec}s)...",
        flush=True,
    )
    try:
        wait_for_schedulable_nodes_for_bvt(timeout_sec=timeout_sec, poll_sec=10)
    except RuntimeError as exc:
        return str(exc)
    print("✓ Cluster nodes schedulable before component pytest", flush=True)
    return ""


def _ensure_yaml_loader() -> None:
    """opendatahub-tests image has pytest but not PyYAML; install to writable tests-payload path."""
    try:
        import yaml  # noqa: F401
        return
    except ImportError:
        pass
    from helpers.pip_bootstrap import pip_install_to_target, prepend_pythonpath
    from components.dashboard_cypress.runtime import prepend_staged_python_deps

    if prepend_staged_python_deps():
        try:
            import yaml  # noqa: F401
            return
        except ImportError:
            pass
    artifacts = _artifacts_dir()
    os.environ.setdefault("ARTIFACTS_DIR", str(artifacts))
    from steps.tests_payload import resolve_tests_payload_root, tests_payload_tools_python_dir

    target = tests_payload_tools_python_dir(resolve_tests_payload_root(artifacts))
    print(f"Installing PyYAML to {target} (component pytest)...", flush=True)
    pip_install_to_target("pyyaml", target)
    prepend_pythonpath(str(target))
    import yaml  # noqa: F401


def _iter_components_from_plan(plan_path: Path) -> list[dict[str, str]]:
    try:
        raw: Any = json.loads(plan_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        print(f"ERROR: invalid component smoke plan {plan_path}: {exc}", file=sys.stderr)
        raise SystemExit(1) from exc
    items = raw.get("components") if isinstance(raw, dict) else None
    if not isinstance(items, list) or not items:
        print(f"ERROR: plan must contain non-empty components list: {plan_path}", file=sys.stderr)
        raise SystemExit(1)
    out: list[dict[str, str]] = []
    for i, item in enumerate(items):
        if not isinstance(item, dict):
            print(f"ERROR: components[{i}] must be an object in {plan_path}", file=sys.stderr)
            raise SystemExit(1)
        cid = str(item.get("id", "")).strip()
        if not cid:
            print(f"ERROR: components[{i}].id required in {plan_path}", file=sys.stderr)
            raise SystemExit(1)
        min_success = item.get("min_pass_rate_for_success")
        non_blocking = item.get("non_blocking_on_timeout")
        comp_timeout = item.get("component_test_timeout")
        comp_timeout_by_gate = item.get("component_test_timeout_by_gate")
        by_gate: dict[str, str] = {}
        if isinstance(comp_timeout_by_gate, dict):
            by_gate = {str(k): str(v) for k, v in comp_timeout_by_gate.items()}
        phase_markers_raw = item.get("phase_markers")
        phase_markers: dict[str, str] = {}
        if isinstance(phase_markers_raw, dict):
            phase_markers = {str(k): str(v) for k, v in phase_markers_raw.items()}
        out.append(
            {
                "id": cid,
                "artifact_prefix": str(item.get("artifact_prefix", "")).strip(),
                "pytest_marker": str(item.get("pytest_marker", "")).strip(),
                "phase_markers": phase_markers,
                "pytest_extra_args": str(item.get("pytest_extra_args", "-svv")).strip(),
                "tests_subdir": str(item.get("tests_subdir", "")).strip(),
                "min_pass_rate_for_success": str(min_success) if min_success is not None else "",
                "non_blocking_on_timeout": "true" if non_blocking else "",
                "component_test_timeout": str(comp_timeout).strip() if comp_timeout else "",
                "component_test_timeout_by_gate": by_gate,
                "version_skip_reason": str(item.get("version_skip_reason", "")).strip(),
            }
        )
    return out


def _comp_from_catalog_entry(comp: SmokeComponent) -> dict[str, str]:
    return {
        "id": comp.id,
        "artifact_prefix": comp.artifact_prefix,
        "pytest_marker": comp.pytest_marker,
        "phase_markers": dict(comp.phase_markers),
        "pytest_extra_args": comp.pytest_extra_args,
        "tests_subdir": comp.tests_subdir,
        "min_pass_rate_for_success": (
            str(comp.min_pass_rate_for_success) if comp.min_pass_rate_for_success is not None else ""
        ),
        "non_blocking_on_timeout": "true" if comp.non_blocking_on_timeout else "",
        "component_test_timeout": comp.component_test_timeout or "",
        "component_test_timeout_by_gate": dict(comp.component_test_timeout_by_gate or {}),
    }


def _plan_gate_timeout_defaults(plan_path: Path | None) -> dict[str, str]:
    if plan_path is None or not plan_path.is_file():
        return {}
    try:
        raw: Any = json.loads(plan_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    if not isinstance(raw, dict):
        return {}
    defaults_raw = raw.get("default_component_test_timeout_by_gate")
    if not isinstance(defaults_raw, dict):
        return {}
    return {str(k): str(v) for k, v in defaults_raw.items()}


def _plan_component_test_phases(plan_path: Path | None) -> tuple[str, ...]:
    if plan_path and plan_path.is_file():
        try:
            raw: Any = json.loads(plan_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            raw = None
        if isinstance(raw, dict):
            phases_raw = raw.get("component_test_phases")
            if isinstance(phases_raw, list):
                phases = tuple(
                    str(p).strip().lower()
                    for p in phases_raw
                    if str(p).strip().lower() in {"smoke", "tier1"}
                )
                if phases:
                    return phases
    return parse_component_test_phases(os.environ.get("TEST_GATES", os.environ.get("COMPONENT_TEST_PHASES", "")))


def _pytest_marker_for_component(comp: dict[str, str], phases: tuple[str, ...]) -> str:
    phase_markers_raw = comp.get("phase_markers")
    phase_markers: dict[str, str] = {}
    if isinstance(phase_markers_raw, dict):
        phase_markers = {str(k): str(v) for k, v in phase_markers_raw.items()}
    fallback = comp.get("pytest_marker", "smoke").strip() or "smoke"
    if not phase_markers and fallback:
        phase_markers = {"smoke": fallback}
    return combine_pytest_markers(phase_markers, phases, fallback_smoke_marker=fallback)


def _artifacts_dir() -> Path:
    from runners.run_bvt_pytest import _validate_artifacts_dir

    return _validate_artifacts_dir(os.environ.get("ARTIFACTS_DIR", "/artifacts").strip())


def _truthy_env(name: str) -> bool:
    return os.environ.get(name, "").strip().lower() in ("1", "true", "yes")


from runners.component_junit import prereq_junit_outcome, write_single_failure_junit


def _return_infra_junit_failure(
    comp: dict[str, str],
    *,
    artifacts_dir: Path,
    prefix_by_id: dict[str, str],
    testcase_name: str,
    message: str,
    refresh_test_output: Any,
    outcome: str = "failure",
) -> int:
    cid = comp.get("id", "unknown")
    write_single_failure_junit(
        comp,
        artifacts_dir=artifacts_dir,
        testcase_name=testcase_name,
        message=message,
        outcome=outcome,
    )
    prefix_by_id[cid] = comp["artifact_prefix"]
    strict_ec, tekton_ec = resolve_component_exit_codes(
        comp,
        raw_ec=1,
        artifacts_dir=artifacts_dir,
    )
    if _filter_component_id():
        _accumulate_exit_file(strict_ec)
    refresh_test_output()
    return tekton_ec


def _ensure_failure_junit(
    comp: dict[str, str],
    artifacts_dir: Path,
    raw_ec: int,
    timeout_seconds: float | None = None,
) -> None:
    """If pytest failed (exit != 0) and left no JUnit, write a synthetic failure suite.

    Prefer keeping a partial JUnit that already has real testcases (e.g. timeout mid-suite).
    Else salvage PASSED/FAILED/ERROR lines from the component console log.
    """
    if raw_ec == 0:
        return
    # artifact_prefix already ends in -smoke (e.g. ogx-smoke); do not append another -smoke.
    prefix = (comp.get("artifact_prefix") or "").strip()
    for candidate in (
        artifacts_dir / f"{comp.get('id', 'unknown')}-smoke.xml",
        artifacts_dir / f"{prefix}.xml" if prefix else None,
    ):
        if candidate is None or not candidate.is_file() or not candidate.name.endswith(".xml"):
            continue
        try:
            text = candidate.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        if "<testcase" in text and 'name="timeout"' not in text:
            print(
                f"NOTE: keeping partial JUnit after failure ({candidate.name}); "
                "not overwriting with synthetic failure",
                flush=True,
            )
            return
    if prefix and _salvage_junit_from_console_log(comp, artifacts_dir, prefix):
        return
    cid = comp.get("id", "unknown")
    tests_subdir = comp.get("tests_subdir", "")
    marker = comp.get("pytest_marker", "")
    if raw_ec == 124:
        message = (
            f"Component timed out after {timeout_seconds or 0.0:g}s. "
            f"pytest marker={marker!s}, tests_subdir={tests_subdir!s}"
        )
        testcase_name = "timeout"
    else:
        message = (
            f"Component failed with exit {raw_ec} (no JUnit produced). "
            f"pytest marker={marker!s}, tests_subdir={tests_subdir!s}"
        )
        testcase_name = "failure"
    write_single_failure_junit(
        comp,
        artifacts_dir=artifacts_dir,
        testcase_name=testcase_name,
        message=message,
        time_seconds=timeout_seconds or 0.0,
    )


_TEST_STATUS_RE = re.compile(
    r"TEST:\s+(\S+)\s+STATUS:\s+(?:\x1b\[[0-9;]*m)*\s*(PASSED|FAILED|ERROR|SKIPPED)",
    re.IGNORECASE,
)


def _salvage_junit_from_console_log(
    comp: dict[str, str], artifacts_dir: Path, prefix: str
) -> bool:
    """Build JUnit from pytest console STATUS lines when session XML was never flushed."""
    log_path = artifacts_dir / f"{prefix}.console.log"
    if not log_path.is_file():
        # Some runners only tee to stdout; also try pytest-tests.log under results/
        for alt in (
            artifacts_dir / "pytest-tests.log",
            artifacts_dir / "results" / "pytest-tests.log",
        ):
            if alt.is_file():
                log_path = alt
                break
        else:
            return False
    try:
        text = log_path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return False
    # Strip ANSI for matching
    plain = re.sub(r"\x1b\[[0-9;]*m", "", text)
    # Keep last status per test name (console logs often reprint the same TEST: line).
    status_by_test: dict[str, str] = {}
    for match in _TEST_STATUS_RE.finditer(plain):
        status_by_test[match.group(1)] = match.group(2).upper()
    if not status_by_test:
        return False
    cid = comp.get("id", "unknown")
    failures = sum(1 for st in status_by_test.values() if st == "FAILED")
    errors = sum(1 for st in status_by_test.values() if st == "ERROR")
    skipped = sum(1 for st in status_by_test.values() if st == "SKIPPED")
    cases: list[str] = []
    for name, status in status_by_test.items():
        name_attr = quoteattr(name)
        if status == "PASSED":
            cases.append(f'  <testcase classname={quoteattr(cid)} name={name_attr} time="1"/>\n')
        elif status == "SKIPPED":
            cases.append(
                f'  <testcase classname={quoteattr(cid)} name={name_attr} time="0">\n'
                f'    <skipped message="salvaged from console after timeout"/>\n'
                f"  </testcase>\n"
            )
        elif status == "ERROR":
            cases.append(
                f'  <testcase classname={quoteattr(cid)} name={name_attr} time="1">\n'
                f'    <error message="salvaged from console after timeout"/>\n'
                f"  </testcase>\n"
            )
        else:
            cases.append(
                f'  <testcase classname={quoteattr(cid)} name={name_attr} time="1">\n'
                f'    <failure message="salvaged from console after timeout"/>\n'
                f"  </testcase>\n"
            )
    junit_path = artifacts_dir / f"{prefix}.xml"
    passed = len(status_by_test) - failures - errors - skipped
    xml = (
        '<?xml version="1.0" encoding="utf-8"?>\n'
        f'<testsuite name={quoteattr(cid)} tests="{len(status_by_test)}" failures="{failures}" '
        f'errors="{errors}" skipped="{skipped}" time="0" '
        f'timestamp="{time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}">\n'
        f"{''.join(cases)}"
        "</testsuite>\n"
    )
    junit_path.write_text(xml, encoding="utf-8")
    print(
        f"NOTE: salvaged {len(status_by_test)} testcase(s) from {log_path.name} after timeout "
        f"({passed} passed, {failures} failed, {errors} errors)",
        flush=True,
    )
    return True


def _apply_non_blocking_timeout(comp: dict[str, str], ec: int, raw_ec: int) -> int:
    """When catalog marks timeout as non-blocking, do not fail the smoke loop on exit 124."""
    if raw_ec != 124:
        return ec
    cluster_source = os.environ.get("CLUSTER_SOURCE", "").strip()
    if is_ephemeral_hosted_cluster_source(cluster_source) and comp.get("id") == "ogx":
        return ec
    flag = comp.get("non_blocking_on_timeout", "").strip().lower()
    if flag not in ("1", "true", "yes"):
        return ec
    print(
        f"Component {comp['id']!r} hit per-component timeout (exit 124) — "
        "nonBlockingOnTimeout; continuing smoke",
        flush=True,
    )
    return 0


def _merge_pytest_k_skip(extra: str, skip_fragment: str) -> str:
    """Merge ``-k 'not foo'`` skip fragments into pytest extra args."""
    try:
        frag_tokens = shlex.split(skip_fragment)
    except ValueError:
        frag_tokens = skip_fragment.split()
    skip_expr = ""
    for index, token in enumerate(frag_tokens):
        if token == "-k" and index + 1 < len(frag_tokens):
            skip_expr = frag_tokens[index + 1].strip("'\"")
            break
    if not skip_expr:
        return f"{extra} {skip_fragment}".strip() if extra else skip_fragment
    if extra:
        try:
            tokens = shlex.split(extra)
        except ValueError:
            tokens = extra.split()
        for index, token in enumerate(tokens):
            if token != "-k" or index + 1 >= len(tokens):
                continue
            existing = tokens[index + 1].strip("'\"")
            if skip_expr in existing:
                return shlex.join(tokens)
            merged = f"({existing}) and {skip_expr}"
            tokens = tokens[:index] + ["-k", merged] + tokens[index + 2 :]
            return shlex.join(tokens)
        return shlex.join([*tokens, *frag_tokens])
    return skip_fragment


def _apply_cluster_source_pytest_extra_args(extra: str) -> str:
    """EPHC IDMS / external HCP mirror quay.io/rhoai; skip registry-only image checks."""
    if _needs_image_validation_skip():
        extra = _merge_pytest_k_skip(extra, _EHC_SKIP_IMAGE_VALIDATION) if extra else _EHC_SKIP_IMAGE_VALIDATION
    if _needs_imagestream_health_skip():
        extra = (
            _merge_pytest_k_skip(extra, _EXTERNAL_SKIP_IMAGESTREAM_HEALTH)
            if extra
            else _EXTERNAL_SKIP_IMAGESTREAM_HEALTH
        )
    if _needs_cluster_sanity_rhoai_skip() and _CLUSTER_SANITY_SKIP_RHOAI not in extra:
        extra = f"{extra} {_CLUSTER_SANITY_SKIP_RHOAI}".strip() if extra else _CLUSTER_SANITY_SKIP_RHOAI
    return extra


_OGX_EA_DISTRIBUTION = "rh-dev"
_EPHC_SKIP_OGX_FILE_SEARCH = "-k 'not file_search'"
_RHOAI_E2E_ROOT = Path(__file__).resolve().parents[1]


def _ensure_rhoai_e2e_on_pythonpath() -> None:
    """ogx_ea_distribution_plugin lives under rhoai-e2e/, not opendatahub-tests."""
    root = str(_RHOAI_E2E_ROOT)
    parts = [p for p in os.environ.get("PYTHONPATH", "").split(os.pathsep) if p]
    if root in parts:
        return
    os.environ["PYTHONPATH"] = f"{root}{os.pathsep}{os.environ.get('PYTHONPATH', '')}".rstrip(os.pathsep)


def _force_pytest_tc(extra: str, key: str, value: str) -> str:
    scrubbed = re.sub(rf"--tc\s+{re.escape(key)}:\S+", "", extra)
    scrubbed = re.sub(rf"--tc\s+{re.escape(key)}={re.escape(value)}", "", scrubbed)
    return f"{scrubbed.strip()} --tc {key}:{value}".strip()


def _override_ogx_shift_left_distribution_env() -> None:
    """P-OGX1: shift-left envfile may set distribution_name=rh; EA.2 webhook only accepts rh-dev."""
    for key in ("distribution_name", "DISTRIBUTION_NAME", "OGX_DISTRIBUTION_NAME"):
        val = os.environ.get(key, "").strip()
        if val in ("", "rh"):
            os.environ[key] = _OGX_EA_DISTRIBUTION


def _apply_ogx_pytest_extra_args(extra: str) -> str:
    _override_ogx_shift_left_distribution_env()
    extra = _force_pytest_tc(extra, "distribution_name", _OGX_EA_DISTRIBUTION)
    if "-p ogx_ea_distribution_plugin" not in extra:
        extra = f"-p ogx_ea_distribution_plugin {extra}".strip()
    if "-p ogx_tekton_route_plugin" not in extra:
        extra = f"-p ogx_tekton_route_plugin {extra}".strip()
    cluster_source = os.environ.get("CLUSTER_SOURCE", "").strip()
    if is_ephemeral_hosted_cluster_source(cluster_source):
        extra = _merge_pytest_k_skip(extra, _EPHC_SKIP_OGX_FILE_SEARCH)
    return extra


def _materialize_ogx_ea_conftest(artifacts_dir: Path) -> None:
    """Stage ogx EA plugin under pytest workdir so uv subprocess can import -p and sitecustomize."""
    import shutil

    work = artifacts_dir / "pytest-work-cwd"
    work.mkdir(parents=True, exist_ok=True)
    root = _RHOAI_E2E_ROOT
    plugin_src = root / "ogx_ea_distribution_plugin.py"
    tekton_src = root / "ogx_tekton_route_plugin.py"
    shutil.copy2(plugin_src, work / "ogx_ea_distribution_plugin.py")
    shutil.copy2(tekton_src, work / "ogx_tekton_route_plugin.py")
    (work / "sitecustomize.py").write_text(
        "import ogx_ea_distribution_plugin as _ogx_ea\n"
        "import ogx_tekton_route_plugin as _ogx_tekton\n"
        "_ogx_ea.apply_ogx_ea_distribution_patch()\n"
        "_ogx_tekton.apply_ogx_tekton_route_patch()\n",
        encoding="utf-8",
    )
    (work / "conftest.py").write_text(
        f"import sys\nsys.path.insert(0, {str(root)!r})\n"
        "import ogx_ea_distribution_plugin as _ogx_ea\n"
        "import ogx_tekton_route_plugin as _ogx_tekton\n"
        "def pytest_configure(config):\n"
        "    _ogx_ea.pytest_configure(config)\n"
        "    _ogx_tekton.pytest_configure(config)\n",
        encoding="utf-8",
    )


_MODEL_SERVING_COMPONENT_IDS = frozenset({"model_server", "model_runtime", "maas_billing"})
# Pytest cluster sanity is only ~120s; these suites exit with 0 tests when DSC is Not Ready
# (MaaSPrerequisites / dashboard lag after install). Wait like BVT before invoking pytest.
_FULL_DSC_READY_COMPONENT_IDS = frozenset(
    {
        "maas_billing",
        "ogx",
        "ai_safety",
        "ai_safety_evalhub",
        "ai_safety_guardrails",
        "ai_safety_lmeval",
        "ai_safety_trustyai_operator",
        "ai_safety_trustyai_service",
    }
)


def _needs_full_dsc_ready_before_pytest(cid: str) -> bool:
    """True when component pytest requires DSC Ready (avoid empty JUnit from sanity exit)."""
    if cid in _FULL_DSC_READY_COMPONENT_IDS:
        return True
    return cid.startswith("ai_safety")


def _collect_only_mode() -> bool:
    raw = os.environ.get("COMPONENT_TEST_COLLECT_ONLY", "").strip().lower()
    if not raw:
        raw = os.environ.get("COMPONENT_SMOKE_COLLECT_ONLY", "").strip().lower()
    if raw in ("1", "true", "yes"):
        return True
    if raw in ("0", "false", "no"):
        return False
    if is_external_cluster_source(os.environ.get("CLUSTER_SOURCE", "")):
        return False
    kubeconfig = os.environ.get("KUBECONFIG", "").strip()
    if kubeconfig and Path(kubeconfig).is_file():
        return False
    product = os.environ.get("PRODUCT", "").strip().lower()
    from suite.constants import is_test_only_product

    return is_test_only_product(product)


def _read_exit_code(path: Path) -> int:
    if not path.is_file():
        return 0
    try:
        return int(path.read_text(encoding="ascii").strip())
    except ValueError:
        return 0


def _component_exit_path() -> Path:
    cid = _filter_component_id() or ""
    return component_exit_file_path(_artifacts_dir(), cid)


def _accumulate_exit_file(ec: int) -> None:
    exit_path = _component_exit_path()
    worst = _read_exit_code(exit_path)
    if ec != 0:
        worst = ec if worst == 0 else max(worst, ec)
        exit_path.write_text(str(worst), encoding="ascii")


def _filter_component_id() -> str | None:
    raw = os.environ.get("COMPONENT_TEST_COMPONENT_ID", "").strip()
    if not raw:
        raw = os.environ.get("COMPONENT_SMOKE_COMPONENT_ID", "").strip()
    return raw or None


def _run_one_component(
    comp: dict[str, str],
    *,
    component_phases: tuple[str, ...],
    collect_only: bool,
    artifacts_dir: Path,
    timeout_raw: str,
    catalog_gate_defaults: dict[str, str],
    prefix_by_id: dict[str, str],
    refresh_test_output: Any,
) -> int:
    cid = comp["id"]
    by_gate_raw = comp.get("component_test_timeout_by_gate")
    by_gate: dict[str, str] = by_gate_raw if isinstance(by_gate_raw, dict) else {}
    comp_timeout_raw = resolve_component_test_timeout_raw(
        phases=component_phases,
        component_default=comp.get("component_test_timeout", ""),
        component_by_gate=by_gate,
        catalog_gate_defaults=catalog_gate_defaults,
        cli_override=timeout_raw,
    )
    comp_timeout_raw = apply_cluster_source_timeout_cap(
        component_id=cid,
        timeout_raw=comp_timeout_raw,
    )
    try:
        comp_timeout_seconds = parse_component_timeout_seconds(comp_timeout_raw)
    except ValueError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        raise SystemExit(2) from exc
    if comp_timeout_seconds is not None:
        os.environ[COMPONENT_TEST_TIMEOUT_SECS_ENV] = str(comp_timeout_seconds)
    else:
        os.environ.pop(COMPONENT_TEST_TIMEOUT_SECS_ENV, None)
    os.environ["ARTIFACT_PREFIX"] = comp["artifact_prefix"]
    marker = _pytest_marker_for_component(comp, component_phases)
    os.environ["PYTEST_MARKER"] = marker
    extra = _apply_cluster_source_pytest_extra_args(comp["pytest_extra_args"])
    if cid == "maas_billing":
        for skip_fn in (
            maas_billing_rosa_hcp_pytest_extra_args,
            maas_billing_aitenant_pytest_extra_args,
            maas_billing_aitenant_bootstrap_pytest_extra_args,
            maas_billing_ephc_bbr_pytest_extra_args,
        ):
            maas_skip = skip_fn()
            if maas_skip:
                extra = _merge_pytest_k_skip(extra, maas_skip)
    if cid == "ogx":
        extra = _apply_ogx_pytest_extra_args(extra)
        _ensure_rhoai_e2e_on_pythonpath()
        os.environ["RHOAI_E2E_SCRIPTS_ROOT"] = str(_RHOAI_E2E_ROOT)
    if collect_only:
        extra = f"{extra} --collect-only -q".strip() if extra else "--collect-only -q"
    os.environ["PYTEST_EXTRA_ARGS"] = extra
    os.environ["TESTS_SUBDIR"] = comp["tests_subdir"]
    print(
        f"=== component tests: {cid} (phases={','.join(component_phases)}, marker={marker!r}, subdir={comp['tests_subdir']}) ===",
        flush=True,
    )
    version_skip = comp.get("version_skip_reason", "").strip()
    if version_skip:
        print(
            f"SKIP component tests {cid}: version gate ({version_skip})",
            flush=True,
        )
        return _return_infra_junit_failure(
            comp,
            artifacts_dir=artifacts_dir,
            prefix_by_id=prefix_by_id,
            testcase_name="version_not_supported",
            message=version_skip,
            refresh_test_output=refresh_test_output,
            outcome="skip",
        )
    if not collect_only:
        tekton_kubeconfig = os.environ.get("KUBECONFIG", "").strip()
        prepare_kubeconfig_auth_for_tests(tekton_kubeconfig_path=tekton_kubeconfig)
        api_reason = cluster_smoke_infra_blocked_reason()
        if api_reason:
            print(
                f"FAIL component tests {cid}: {api_reason} — skipping pytest",
                flush=True,
            )
            return _return_infra_junit_failure(
                comp,
                artifacts_dir=artifacts_dir,
                prefix_by_id=prefix_by_id,
                testcase_name="cluster_smoke_infra_blocked",
                message=api_reason,
                refresh_test_output=refresh_test_output,
            )
    if not collect_only and components_need_models_as_service({cid}):
        try:
            refresh_maas_smoke_before_pytest(component_id=cid)
        except Exception as exc:
            if is_definitive_infra_error(str(exc)):
                print(
                    f"FAIL component tests {cid}: MaaS/infra ({exc}) — skipping pytest",
                    flush=True,
                )
                return _return_infra_junit_failure(
                    comp,
                    artifacts_dir=artifacts_dir,
                    prefix_by_id=prefix_by_id,
                    testcase_name="maas_infra_blocked",
                    message=str(exc),
                    refresh_test_output=refresh_test_output,
                )
            print(
                f"WARN: MaaS pre-pytest refresh for {cid} failed ({exc}); continuing",
                file=sys.stderr,
                flush=True,
            )
    pytest_extra_env: dict[str, str] = {}
    if not collect_only and cid == "maas_billing":
        pytest_extra_env = apply_maas_billing_htpasswd_test_user_overrides()
    if not collect_only:
        run_pooled_external_smoke_prep(cid)
        if not cluster_prep_already_done(artifacts_dir):
            prepare_component_for_smoke(cid)
        # Heal baseline-Managed Ready flaps (DeploymentsNotReady, Removed, …) before pytest.
        reconcile_baseline_dsc_before_component(cid, artifacts_dir)
        api_reason = cluster_smoke_infra_blocked_reason()
        if api_reason:
            print(
                f"FAIL component tests {cid}: {api_reason} — skipping pytest",
                flush=True,
            )
            return _return_infra_junit_failure(
                comp,
                artifacts_dir=artifacts_dir,
                prefix_by_id=prefix_by_id,
                testcase_name="cluster_smoke_infra_blocked",
                message=api_reason,
                refresh_test_output=refresh_test_output,
            )
    if not collect_only and _needs_full_dsc_ready_before_pytest(cid):
        from components.maas_billing.timeouts import bvt_dsc_ready_timeout_sec
        from components.maas_billing.wait import require_dsc_ready_for_bvt

        try:
            print(
                f"Waiting for DSC Ready before {cid} pytest (cluster sanity is only 120s)...",
                flush=True,
            )
            require_dsc_ready_for_bvt(timeout_sec=bvt_dsc_ready_timeout_sec())
        except RuntimeError as exc:
            print(
                f"FAIL component tests {cid}: DSC not Ready ({exc}) — skipping pytest",
                flush=True,
            )
            return _return_infra_junit_failure(
                comp,
                artifacts_dir=artifacts_dir,
                prefix_by_id=prefix_by_id,
                testcase_name="dsc_not_ready",
                message=str(exc),
                refresh_test_output=refresh_test_output,
            )
    if _truthy_env("FAIL_FAST_DISABLED_COMPONENT"):
        unavailable, reason = smoke_component_prereq_unavailable(cid)
        if unavailable:
            print(
                f"FAIL component tests {cid}: component not ready ({reason}) — skipping pytest",
                flush=True,
            )
            return _return_infra_junit_failure(
                comp,
                artifacts_dir=artifacts_dir,
                prefix_by_id=prefix_by_id,
                testcase_name="component_not_ready",
                message=f"Component not ready for {cid}: {reason}",
                refresh_test_output=refresh_test_output,
                outcome=prereq_junit_outcome(reason),
            )
    if not collect_only and cid in _MODEL_SERVING_COMPONENT_IDS:
        promote_shift_left_aws_env()
        if not os.environ.get("AWS_ACCESS_KEY_ID", "").strip():
            msg = (
                f"AWS_ACCESS_KEY_ID unset after shift-left promote for {cid} "
                "(check tenant vault-approle and Vault apps/rhods-ci/shift-left envFileCommon)"
            )
            print(f"ERROR: {msg}", file=sys.stderr, flush=True)
            return _return_infra_junit_failure(
                comp,
                artifacts_dir=artifacts_dir,
                prefix_by_id=prefix_by_id,
                testcase_name="shift_left_aws_credentials_missing",
                message=msg,
                refresh_test_output=refresh_test_output,
                outcome="failure",
            )
        if cid == "model_server":
            from k8s.smoke_ci_s3_test_dir import log_model_server_ci_s3_layout

            log_model_server_ci_s3_layout()
        if cid == "model_runtime":
            from k8s.smoke_ci_s3_test_dir import (
                log_model_runtime_ci_s3_layout,
                model_runtime_pytest_extra_args,
            )

            log_model_runtime_ci_s3_layout()
            runtime_skip = model_runtime_pytest_extra_args()
            if runtime_skip:
                extra = _merge_pytest_k_skip(
                    os.environ.get("PYTEST_EXTRA_ARGS", extra), runtime_skip
                )
                os.environ["PYTEST_EXTRA_ARGS"] = extra
    if not collect_only and cid == "ogx":
        _materialize_ogx_ea_conftest(artifacts_dir)
        _ensure_rhoai_e2e_on_pythonpath()
        if apply_ogx_ea_distribution_patch():
            print("✓ Patched tests.ogx.server_config for rh-dev (EA.2)", flush=True)
        from ogx_tekton_route_plugin import apply_ogx_tekton_route_patch

        if apply_ogx_tekton_route_patch():
            print("✓ Patched tests.ogx.conftest ogx_client for Tekton port-forward", flush=True)
    if not collect_only:
        node_err = _wait_for_schedulable_nodes_before_component_pytest()
        if node_err:
            print(
                f"FAIL component tests {cid}: {node_err} — skipping pytest",
                flush=True,
            )
            return _return_infra_junit_failure(
                comp,
                artifacts_dir=artifacts_dir,
                prefix_by_id=prefix_by_id,
                testcase_name="cluster_nodes_unschedulable",
                message=node_err,
                refresh_test_output=refresh_test_output,
            )
    raw_ec = run_single_pytest(extra_env=pytest_extra_env or None)
    if not collect_only and cid == "ogx":
        from components.ogx.platform_smoke import ensure_ogx_junit_after_pytest

        try:
            ensure_ogx_junit_after_pytest(
                artifacts_dir,
                prefix=comp.get("artifact_prefix", "ogx-smoke") or "ogx-smoke",
            )
        except Exception as exc:  # noqa: BLE001 - never let post-processing mask the real result
            print(
                f"WARN: ensure_ogx_junit_after_pytest failed ({exc}); keeping raw pytest result",
                file=sys.stderr,
                flush=True,
            )
    if not collect_only and cid == "maas_billing":
        from steps.tekton_util import OLMINSTALL_HTPASSWD_KUBECONFIG_ENV

        if os.environ.pop(OLMINSTALL_HTPASSWD_KUBECONFIG_ENV, None):
            prepare_kubeconfig_auth_for_tests(tekton_kubeconfig_path=tekton_kubeconfig)
    if raw_ec != 0:
        _ensure_failure_junit(comp, artifacts_dir, raw_ec, comp_timeout_seconds)
    strict_ec, tekton_ec = resolve_component_exit_codes(
        comp,
        raw_ec=raw_ec,
        artifacts_dir=artifacts_dir,
    )
    if raw_ec == 124:
        # Keep timeout red even when partial JUnit pass rate would green the component;
        # nonBlockingOnTimeout may still downgrade to 0.
        timeout_strict = 124 if strict_ec == 0 else strict_ec
        strict_ec = _apply_non_blocking_timeout(comp, timeout_strict, raw_ec)
        tekton_ec = 0 if strict_ec == 0 else strict_ec
    if _filter_component_id():
        _accumulate_exit_file(strict_ec)
    prefix_by_id[cid] = comp["artifact_prefix"]
    refresh_test_output()
    return tekton_ec


def main() -> int:
    filter_id = _filter_component_id()
    collect_only = _collect_only_mode()
    if not collect_only:
        tekton_kubeconfig = os.environ.get("KUBECONFIG", "").strip()
        os.environ.setdefault("ARTIFACTS_DIR", str(_artifacts_dir()))
        _ensure_yaml_loader()
        prepare_kubeconfig_auth_for_tests(tekton_kubeconfig_path=tekton_kubeconfig)
        ensure_runtime_vault_env()
        load_shift_left_env_from_mount()
        promote_shift_left_aws_env()
        if _filter_component_id() == "ogx":
            _override_ogx_shift_left_distribution_env()
        from components.codeflare_sdk.auth import read_pytest_vault_env

        for key, val in read_pytest_vault_env().items():
            if val and not os.environ.get(key, "").strip():
                os.environ[key] = val
        suppress_ephemeral_jira_env()
        apply_maas_oidc_client_secret_overrides()
        apply_cluster_router_ca_from_kubeconfig()
        artifacts_dir = _artifacts_dir()
        os.environ.setdefault("ARTIFACTS_DIR", str(artifacts_dir))
        from runners.orchestrator import prepare_oc_binary_path_for_pytest, stage_git_for_prereqs

        stage_git_for_prereqs()
        prepare_oc_binary_path_for_pytest()
        artifacts_bin = artifacts_dir / "bin"
        if artifacts_bin.is_dir():
            os.environ["PATH"] = f"{artifacts_bin}:{os.environ.get('PATH', '')}"
    plan_path = os.environ.get("COMPONENT_TEST_PLAN_JSON", "").strip()

    plan_path_obj = Path(plan_path) if plan_path else None
    component_phases = _plan_component_test_phases(plan_path_obj)
    if not component_phases:
        print("ERROR: no component test phases selected (smoke and/or tier1)", file=sys.stderr)
        return 1

    if plan_path:
        components = _iter_components_from_plan(plan_path_obj)  # type: ignore[arg-type]
        catalog_gate_defaults = _plan_gate_timeout_defaults(plan_path_obj)
    else:
        _ensure_yaml_loader()
        csv = os.environ.get("COMPONENTS_CSV", "").strip()
        cfg_path = os.environ.get("COMPONENTS_CONFIG", "").strip()
        if not csv or not cfg_path:
            print("COMPONENTS_CSV and COMPONENTS_CONFIG are required", file=sys.stderr)
            return 1
        catalog = load_components_smoke_catalog(Path(cfg_path))
        selected = parse_components_selection(csv, catalog)
        components = [
            _comp_from_catalog_entry(catalog.components[cid])
            for cid in catalog.component_ids
            if cid in selected
        ]
        catalog_gate_defaults = dict(catalog.default_component_test_timeout_by_gate or {})

    prefix_by_id: dict[str, str] = {}
    worst = 0
    test_output_path = os.environ.get("TEST_OUTPUT_PATH", "").strip()
    note_prefix = os.environ.get("NOTE_PREFIX", "Smoke").strip()
    artifacts_dir = _artifacts_dir()
    timeout_raw = os.environ.get("COMPONENT_TEST_TIMEOUT", "").strip()
    component_order: list[str] = [c["id"] for c in components]
    components_in_run = set(component_order)

    if filter_id:
        if filter_id not in components_in_run:
            print(
                f"SKIP smoke {filter_id}: not in component smoke plan (COMPONENTS selection)",
                flush=True,
            )
            return 0
        components = [c for c in components if c["id"] == filter_id]

    def _refresh_test_output() -> None:
        if not test_output_path:
            return
        from steps.summarize_test_output import write_junit_test_output

        write_junit_test_output(
            artifacts_dir,
            test_output_path,
            note_prefix=note_prefix,
            component_order=component_order,
        )

    for comp in components:
        ec = _run_one_component(
            comp,
            component_phases=component_phases,
            collect_only=collect_only,
            artifacts_dir=artifacts_dir,
            timeout_raw=timeout_raw,
            catalog_gate_defaults=catalog_gate_defaults,
            prefix_by_id=prefix_by_id,
            refresh_test_output=_refresh_test_output,
        )
        if ec == 2:
            return 2
        if ec != 0:
            worst = ec if worst == 0 else max(worst, ec)

    if not prefix_by_id and not filter_id:
        print("ERROR: no component tests ran (all skipped or empty COMPONENTS)", file=sys.stderr)
        worst = 1

    if filter_id:
        return worst

    _component_exit_path().write_text(str(worst), encoding="ascii")
    return worst


if __name__ == "__main__":
    raise SystemExit(main())
