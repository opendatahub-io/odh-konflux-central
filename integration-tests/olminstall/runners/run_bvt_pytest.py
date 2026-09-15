#!/usr/bin/env python3
"""Run BVT pytest inside the opendatahub-tests image.

Single run (default): set ARTIFACT_PREFIX and optional PYTEST_MARKER / TESTS_SUBDIR.

Full BVT health suite: set BVT_SUITE=health to run cluster_health then operator_health
(or placeholder JUnit when test-only PRODUCT without external kubeconfig). Used by
task-bvt-health-checks.

Env (required for single run):
    ARTIFACT_PREFIX  -- filename prefix for JUnit XML + console log
Env (optional):
    PYTEST_MARKER    -- pytest -m expression (empty = no -m flag)
    PYTEST_EXTRA_ARGS -- extra pytest CLI args (e.g. "--collect-only -q" or "-svv")
    ARTIFACTS_DIR    -- directory for JUnit + logs (default /artifacts; must be under
                       /artifacts or TEST_ARTIFACTS_DIR when set for local runs)
    TEST_ARTIFACTS_DIR -- optional extra allowed root (e.g. ./artifacts) for local debugging
    TESTS_SUBDIR     -- subdirectory under tests root (default "tests/cluster_health")
    COMPONENT_TEST_TIMEOUT_SECS -- optional per-run subprocess timeout in seconds (component smoke)
"""

from __future__ import annotations

import os
import shlex
import shutil
import signal
import subprocess
import sys
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

from suite.component_test_timeout import COMPONENT_TEST_TIMEOUT_SECS_ENV

from suite.component_task_exit import resolve_junit_aggregate_exit

from steps.tekton_util import ensure_writable_kubeconfig, prepare_kubeconfig_auth_for_tests
from suite.its_trigger_params import is_external_cluster_source

_KNOWN_ROOTS = [
    "/home/odh/opendatahub-tests",
    "/opendatahub-tests",
    "/opt/app-root/src",
    "/workspace/source",
]


def _safe_artifact_prefix(raw: str) -> str | None:
    """ARTIFACT_PREFIX must be a single filename segment (no path separators)."""
    if not raw or raw in {".", ".."}:
        return None
    if "/" in raw or "\\" in raw:
        return None
    p = Path(raw)
    if p.is_absolute() or ".." in p.parts or p.name != raw:
        return None
    return raw


def _artifacts_dir_bases() -> tuple[Path, ...]:
    """Allowed roots for ARTIFACTS_DIR (pipeline + local dev)."""
    bases: list[Path] = [
        Path("/artifacts"),
        Path("/workspace/tests-shared/tests-payload"),
        Path("/workspace/tests-shared/tests-payload/results"),
        Path("/workspace/tests-shared/artifacts"),
        Path("/workspace/tests-shared/artifacts/bvt"),
    ]
    extra = os.environ.get("TEST_ARTIFACTS_DIR", "").strip()
    if extra:
        bases.append(Path(extra))
    return tuple(bases)


def _validate_artifacts_dir(raw: str) -> Path:
    """Resolve *raw* and ensure it stays under an allowed artifacts root."""
    resolved = Path(raw).resolve()
    for base in _artifacts_dir_bases():
        root = base.resolve()
        if resolved == root:
            return resolved
        try:
            resolved.relative_to(root)
            return resolved
        except ValueError:
            continue
    print(
        f"ARTIFACTS_DIR must resolve under {', '.join(str(b) for b in _artifacts_dir_bases())}; got {resolved}",
        file=sys.stderr,
    )
    sys.exit(1)


def _safe_tests_subdir(raw: str) -> str | None:
    """TESTS_SUBDIR must be a relative path without .. components."""
    if not raw:
        return None
    p = Path(raw)
    if p.is_absolute() or ".." in p.parts:
        return None
    return raw


def _locate_tests_root(tests_subdir: str) -> str | None:
    sub = Path(tests_subdir)
    if sub.is_absolute() or ".." in sub.parts:
        return None
    for d in _KNOWN_ROOTS:
        if (Path(d) / sub).is_dir():
            return d
    return None


def _ensure_odh_tests_results_dir(tests_root: Path) -> None:
    """opendatahub-tests conftest writes ``results/pytest-tests.log`` under the project root."""
    if not tests_root.is_dir():
        return
    odh_results = tests_root / "results"
    if odh_results.exists():
        return
    try:
        odh_results.mkdir(parents=True, exist_ok=True)
    except OSError:
        return


_PIP_FALLBACK_PACKAGES = ("pytest", "shortuuid", "kubernetes")


def _pytest_produced_junit(junit_path: str) -> bool:
    """True when uv/pytest already wrote a parseable JUnit file (do not pip-fallback)."""
    path = Path(junit_path)
    if not path.is_file():
        return False
    try:
        from suite.component_junit import junit_counts

        return junit_counts(path) is not None
    except Exception:
        return path.stat().st_size > 64


def _uv_infra_exit_code(ec: int) -> bool:
    """True for pytest collection/infra exits; not for test failures or plugin post-run codes."""
    return ec in (2, 3, 4, 5)


def _uv_project_cmd(uv: str, tests_root: Path, *args: str) -> list[str]:
    """Run a uv subcommand against the opendatahub-tests project (not pytest workdir)."""
    return [uv, "--directory", str(tests_root), *args]


def _prepend_pythonpath(env: dict[str, str], *paths: str) -> None:
    head = [p for p in paths if p]
    if not head:
        return
    tail = env.get("PYTHONPATH", "").strip()
    env["PYTHONPATH"] = os.pathsep.join(head + ([tail] if tail else []))


def _ogx_ea_plugin_requested(pytest_extra: str, tests_subdir: str) -> bool:
    if "ogx_ea_distribution_plugin" in pytest_extra:
        return True
    return tests_subdir.rstrip("/").endswith("tests/ogx")


def _build_pytest_args(
    marker: str,
    extra_args: str,
    tests_subdir: str,
    junit_path: str,
) -> list[str]:
    args: list[str] = []
    if marker:
        args.extend(["-m", marker])
    if extra_args:
        args.extend(shlex.split(extra_args))
    args.extend([tests_subdir, f"--junitxml={junit_path}", "--tb=native"])
    return args


class _ComponentTestTimeoutBudget:
    """Single wall-clock budget shared across pytest, uv sync, and retry subprocesses."""

    def __init__(self, total_seconds: float | None) -> None:
        self._total = total_seconds
        self._started = time.monotonic() if total_seconds is not None else None

    def remaining(self) -> float | None:
        if self._total is None or self._started is None:
            return None
        return max(0.0, self._total - (time.monotonic() - self._started))

    def expired(self) -> bool:
        remaining = self.remaining()
        return remaining is not None and remaining <= 0


def _component_test_timeout_seconds() -> float | None:
    raw = os.environ.get(COMPONENT_TEST_TIMEOUT_SECS_ENV, "").strip()
    if not raw:
        return None
    try:
        secs = float(raw)
    except ValueError as exc:
        raise ValueError(
            f"{COMPONENT_TEST_TIMEOUT_SECS_ENV} must be a positive number, got: {raw!r}"
        ) from exc
    if secs <= 0:
        raise ValueError(
            f"{COMPONENT_TEST_TIMEOUT_SECS_ENV} must be a positive number, got: {raw!r}"
        )
    return secs


def _run_with_tee(
    cmd: list[str],
    log_path: str,
    *,
    env: dict[str, str] | None = None,
    timeout: float | None = None,
) -> int:
    """Run *cmd*, tee stdout+stderr to *log_path*, return exit code.

    *timeout* overrides ``COMPONENT_TEST_TIMEOUT_SECS`` when not ``None`` (omit for env-based timeout).
    """
    timeout_s = _component_test_timeout_seconds() if timeout is None else (timeout if timeout > 0 else None)
    attempt_header = (
        f"\n--- pytest attempt {datetime.now(timezone.utc).isoformat()} "
        f"({' '.join(cmd[:3])}...) ---\n"
    )
    with open(log_path, "a", encoding="utf-8") as log:
        log.write(attempt_header)
        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            env=env,
        )
        assert proc.stdout is not None

        def _tee_stdout() -> None:
            for line in proc.stdout:
                sys.stdout.write(line)
                log.write(line)

        reader = threading.Thread(target=_tee_stdout, daemon=True)
        reader.start()
        try:
            if timeout_s is not None:
                proc.wait(timeout=timeout_s)
            else:
                proc.wait()
        except subprocess.TimeoutExpired:
            print(
                f"ERROR: command timed out after {timeout_s}s ({COMPONENT_TEST_TIMEOUT_SECS_ENV}): {' '.join(cmd)}",
                file=sys.stderr,
            )
            # Prefer SIGINT so pytest can flush junitxml before hard kill.
            try:
                proc.send_signal(signal.SIGINT)
                proc.wait(timeout=60)
            except (subprocess.TimeoutExpired, OSError, ValueError):
                proc.terminate()
                try:
                    proc.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    proc.kill()
                    proc.wait(timeout=30)
            reader.join(timeout=5)
            return 124
        reader.join()
    return proc.returncode if proc.returncode is not None else 1


def _ensure_pytest_kubeconfig_auth() -> None:
    """Refresh bearer token after writable kubeconfig copy (EPHC current_client_token parity)."""
    artifacts_path = os.environ.get("ARTIFACTS_DIR", "/artifacts").strip() or "/artifacts"
    os.environ.setdefault("ARTIFACTS_DIR", artifacts_path)
    prepare_kubeconfig_auth_for_tests()


def run_single(*, extra_env: dict[str, str] | None = None) -> int:
    """Run one BVT pytest invocation (env: ARTIFACT_PREFIX, PYTEST_MARKER, …)."""
    _ensure_pytest_kubeconfig_auth()
    artifact_prefix = os.environ.get("ARTIFACT_PREFIX", "").strip()
    if not artifact_prefix:
        print("ARTIFACT_PREFIX is required", file=sys.stderr)
        return 1
    if _safe_artifact_prefix(artifact_prefix) is None:
        print("ARTIFACT_PREFIX must be a single filename segment (no / or ..)", file=sys.stderr)
        return 1

    artifacts_path = _validate_artifacts_dir(os.environ.get("ARTIFACTS_DIR", "/artifacts").strip())
    tests_subdir = os.environ.get("TESTS_SUBDIR", "tests/cluster_health").strip()
    if _safe_tests_subdir(tests_subdir) is None:
        print("TESTS_SUBDIR must be a relative path without ..", file=sys.stderr)
        return 1
    pytest_marker = os.environ.get("PYTEST_MARKER", "").strip()
    pytest_extra = os.environ.get("PYTEST_EXTRA_ARGS", "").strip()

    root = _locate_tests_root(tests_subdir)
    if not root:
        if tests_subdir.rstrip("/").endswith("operator_health"):
            msg = (
                f"{tests_subdir} not present in this opendatahub-tests image; "
                "skipping operator_health BVT for this release."
            )
            print(f"WARN: {msg}", file=sys.stderr, flush=True)
            from runners.bvt_product_existing_placeholder_junit import _testsuite_xml

            artifacts_path.mkdir(parents=True, exist_ok=True)
            junit = artifacts_path / f"{artifact_prefix}.xml"
            log = artifacts_path / f"{artifact_prefix}.console.log"
            junit.write_text(_testsuite_xml(name=artifact_prefix, msg=msg), encoding="utf-8")
            log.write_text(f"SKIP: {msg}\n", encoding="utf-8")
            print(f"JUnit ({artifact_prefix}): {junit} (skipped)")
            return 0
        print(f"ERROR: could not find {tests_subdir} under known opendatahub-tests paths.", file=sys.stderr)
        return 1

    tests_root = Path(root)
    test_path = tests_root / tests_subdir
    work_cwd = artifacts_path / "pytest-work-cwd"
    work_cwd.mkdir(parents=True, exist_ok=True)
    (work_cwd / "results").mkdir(exist_ok=True)
    _ensure_odh_tests_results_dir(tests_root)
    prev_cwd = os.getcwd()
    os.chdir(work_cwd)
    try:
        artifacts_path.mkdir(parents=True, exist_ok=True)
        junit = str(artifacts_path / f"{artifact_prefix}.xml")
        log = str(artifacts_path / f"{artifact_prefix}.console.log")
        pytest_args = _build_pytest_args(pytest_marker, pytest_extra, str(test_path), junit)

        # Official opendatahub-tests image: ENTRYPOINT is `uv run pytest` after
        # build-time `uv sync`. Tekton script mode bypasses ENTRYPOINT. Upstream
        # Dockerfile puts uv in /.local/bin; ensure that and ~/.local/bin are on PATH.
        extra_path = "/.local/bin:/home/odh/.local/bin"
        env = dict(os.environ)
        if extra_env:
            env.update(extra_env)
        env["PATH"] = f"{extra_path}:{env.get('PATH', '')}"
        env["PYTHONPATH"] = f"{tests_root}{os.pathsep}{env.get('PYTHONPATH', '')}"
        if _ogx_ea_plugin_requested(pytest_extra, tests_subdir):
            olminstall_root = env.get("OLMINSTALL_SCRIPTS_ROOT", "").strip()
            _prepend_pythonpath(env, str(work_cwd), olminstall_root)
            env.setdefault("PYTEST_PLUGINS", "ogx_ea_distribution_plugin")

        ec = 1
        ran_uv = False
        budget = _ComponentTestTimeoutBudget(_component_test_timeout_seconds())

        def _run_phase(cmd: list[str], *, phase_env: dict[str, str] | None = None) -> int:
            if budget.expired():
                print(
                    f"ERROR: component test timeout budget exhausted ({COMPONENT_TEST_TIMEOUT_SECS_ENV})",
                    file=sys.stderr,
                )
                return 124
            return _run_with_tee(cmd, log, env=phase_env, timeout=budget.remaining())

        uv = shutil.which("uv", path=env["PATH"])
        pyproject = tests_root / "pyproject.toml"
        if uv and pyproject.is_file():
            ran_uv = True
            uv_pytest = _uv_project_cmd(uv, tests_root, "run", "-m", "pytest", *pytest_args)
            ec = _run_phase(uv_pytest, phase_env=env)
            if ec == 124:
                return ec
            # pytest exit 0=pass 1=tests failed; anything ≥2 is an infra/collection error.
            if ec >= 2 and _uv_infra_exit_code(ec):
                print(
                    f"WARN: uv run pytest exited {ec} (infra error); running uv sync then retrying...",
                    file=sys.stderr,
                )
                sync_ec = _run_phase(_uv_project_cmd(uv, tests_root, "sync"), phase_env=env)
                if sync_ec == 124:
                    return sync_ec
                retry_env = {k: v for k, v in env.items() if k != "UV_NO_SYNC"}
                ec = _run_phase(uv_pytest, phase_env=retry_env)
                if ec == 124:
                    return ec

        if not ran_uv:
            # ec was never set by uv; treat as infra so pip+pytest runs instead of exiting 1 without tests.
            ec = 2

        if ec == 124:
            return ec

        # Only fall back to bare python when uv is unavailable or produced an infra/collection error.
        # ec==1 means tests ran but some failed — keep that result rather than re-running.
        # Custom plugin exits (e.g. 99 after cluster sanity) still emit JUnit; do not re-run without deps.
        if ec >= 2 and _pytest_produced_junit(junit):
            print(
                f"pytest exited {ec} but JUnit exists at {junit}; skipping pip fallback",
                flush=True,
            )
            return ec

        if ec >= 2 and not _uv_infra_exit_code(ec) and ran_uv:
            print(
                f"pytest exited {ec} without readable JUnit; treating as test/plugin exit (no pip fallback)",
                flush=True,
            )
            return ec

        if ec >= 2:
            print("WARN: uv unavailable or failed; using pip --target + PYTHONPATH.", file=sys.stderr)
            pylibs = "/tmp/tekton-pytest-libs"
            Path(pylibs).mkdir(exist_ok=True)
            pip_env = dict(env)
            pip_env["PYTHONPATH"] = pylibs
            import_check = "import pytest, shortuuid, kubernetes"
            pip_timeout = budget.remaining()
            if pip_timeout is not None:
                if pip_timeout <= 0:
                    print(
                        f"ERROR: component test timeout budget exhausted ({COMPONENT_TEST_TIMEOUT_SECS_ENV})",
                        file=sys.stderr,
                    )
                    return 124
            else:
                pip_timeout = 600.0
            try:
                subprocess.run(
                    ["python3", "-c", import_check],
                    env=pip_env,
                    check=True,
                    capture_output=True,
                )
            except subprocess.CalledProcessError:
                try:
                    pip_proc = subprocess.run(
                        [
                            "python3",
                            "-m",
                            "pip",
                            "install",
                            "--no-cache-dir",
                            "-q",
                            "-t",
                            pylibs,
                            *_PIP_FALLBACK_PACKAGES,
                        ],
                        env=env,
                        check=False,
                        capture_output=True,
                        text=True,
                        timeout=pip_timeout,
                    )
                except subprocess.TimeoutExpired:
                    print(
                        f"ERROR: pip install timed out after {pip_timeout}s",
                        file=sys.stderr,
                    )
                    return 124
                if pip_proc.returncode != 0:
                    tail = ((pip_proc.stdout or "") + (pip_proc.stderr or "")).strip()[:4000]
                    print(
                        f"ERROR: pip install {_PIP_FALLBACK_PACKAGES} failed (exit {pip_proc.returncode}): "
                        f"{tail or '(no output)'}",
                        file=sys.stderr,
                    )
                    return pip_proc.returncode
            try:
                subprocess.run(
                    ["python3", "-c", "import ocp_resources"],
                    env={**pip_env, "PYTHONPATH": f"{tests_root}{os.pathsep}{pip_env.get('PYTHONPATH', '')}"},
                    check=True,
                    capture_output=True,
                )
            except subprocess.CalledProcessError:
                print(
                    "ERROR: ocp_resources unavailable after uv sync and pip fallback; "
                    "cluster_health BVT cannot run in this image",
                    file=sys.stderr,
                )
                return ec if ec >= 2 else 2
            existing = env.get("PYTHONPATH", "")
            pip_env["PYTHONPATH"] = f"{pylibs}:{tests_root}{os.pathsep}{existing}" if existing else f"{pylibs}:{tests_root}"
            ec = _run_phase(["python3", "-m", "pytest", *pytest_args], phase_env=pip_env)

        print(f"JUnit ({artifact_prefix}): {junit}")
        return ec
    finally:
        os.chdir(prev_cwd)


def _prepare_bvt_oc() -> None:
    """Stage oc under tests-payload/.tools/bin and set OC_BINARY_PATH for pytest."""
    from runners.orchestrator import prepare_oc_binary_path_for_pytest

    prepare_oc_binary_path_for_pytest()


def _cluster_has_odh_apis() -> bool:
    """True when the target cluster exposes Open Data Hub CRDs (e.g. after operator install)."""
    ensure_writable_kubeconfig()
    from k8s.oc_util import _oc_path

    try:
        proc = subprocess.run(
            [
                _oc_path(),
                "api-resources",
                "--api-group=datasciencecluster.opendatahub.io",
                "-o",
                "name",
            ],
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        print(f"WARN: unable to probe Open Data Hub APIs: {exc}", file=sys.stderr)
        return False
    if proc.returncode != 0:
        return False
    return "datascienceclusters" in (proc.stdout or "").lower()


def run_health_suite() -> int:
    """Run cluster_health then operator_health (BVT Tekton task entry)."""
    _prepare_bvt_oc()
    external = is_external_cluster_source(os.environ.get("CLUSTER_SOURCE", ""))
    product = os.environ.get("PRODUCT", "").strip().lower()
    from suite.constants import is_test_only_product

    if is_test_only_product(product) and (not external or not _cluster_has_odh_apis()):
        artifacts = os.environ.get("ARTIFACTS_DIR", "/artifacts").strip() or "/artifacts"
        sys.argv = [sys.argv[0], artifacts]
        from runners.bvt_product_existing_placeholder_junit import main as placeholder

        return placeholder()

    artifacts_path = Path(os.environ.get("ARTIFACTS_DIR", "/artifacts").strip() or "/artifacts")
    mlflow_op_prior: int | None = None
    apps_cronjobs_suspended: list[str] | None = None
    worst = 0

    def _run_marker(marker: str, prefix: str, subdir: str, *, extra_args: str = "-svv") -> int:
        os.environ["PYTEST_MARKER"] = marker
        os.environ["PYTEST_EXTRA_ARGS"] = extra_args
        os.environ["ARTIFACT_PREFIX"] = prefix
        os.environ["TESTS_SUBDIR"] = subdir
        return run_single()

    try:
        from steps.prepare_bvt_cluster_nodes import prepare_bvt_cluster_nodes

        nodes_ec = prepare_bvt_cluster_nodes()
        if nodes_ec != 0:
            return nodes_ec

        ec = _run_marker("cluster_health", "cluster-health", "tests/cluster_health")
        if ec != 0:
            worst = ec if worst == 0 else max(worst, ec)
            if ec == 124:
                return ec

        from steps.prepare_bvt_dsc_ready import prepare_bvt_dsc_ready

        dsc_ec = prepare_bvt_dsc_ready()
        if dsc_ec != 0:
            return dsc_ec

        if external:
            from steps.prepare_bvt_apps_namespace import (
                pause_mlflow_operator_reconcile_for_bvt,
                suspend_apps_cronjobs_for_bvt,
            )

            ec = _run_marker(
                "operator_health",
                "operator-health-core",
                "tests/cluster_health",
                extra_args='-svv -k "not test_application_namespace_pod_healthy"',
            )
            if ec != 0:
                worst = ec if worst == 0 else max(worst, ec)
                if ec == 124:
                    return ec

            try:
                apps_cronjobs_suspended = suspend_apps_cronjobs_for_bvt()
            except Exception as exc:
                print(
                    f"WARN: apps CronJob/Job-pod cleanup before BVT failed ({exc}); continuing",
                    file=sys.stderr,
                    flush=True,
                )
                apps_cronjobs_suspended = None

            try:
                mlflow_op_prior = pause_mlflow_operator_reconcile_for_bvt()
            except Exception as exc:
                print(
                    f"WARN: mlflow migration cleanup before BVT failed ({exc}); continuing with pytest",
                    file=sys.stderr,
                    flush=True,
                )
                mlflow_op_prior = None

            from steps.prepare_bvt_apps_namespace import wait_dashboard_pods_ready_for_bvt

            try:
                wait_dashboard_pods_ready_for_bvt()
            except RuntimeError as exc:
                print(f"ERROR: {exc}", file=sys.stderr, flush=True)
                return 1

            ec = _run_marker(
                "operator_health",
                "operator-health-apps",
                "tests/cluster_health",
                extra_args='-svv -k "test_application_namespace_pod_healthy"',
            )
            if ec != 0:
                worst = ec if worst == 0 else max(worst, ec)
                if ec == 124:
                    return ec

            core_junit = artifacts_path / "operator-health-core.xml"
            apps_junit = artifacts_path / "operator-health-apps.xml"
            merged_junit = artifacts_path / "operator-health.xml"
            if core_junit.is_file() or apps_junit.is_file():
                from components.dashboard_cypress.config import write_merged_junit_reports

                reports = [p for p in (core_junit, apps_junit) if p.is_file()]
                write_merged_junit_reports(reports, merged_junit)
        else:
            ec = _run_marker("operator_health", "operator-health", "tests/cluster_health")
            if ec != 0:
                worst = ec if worst == 0 else max(worst, ec)
                if ec == 124:
                    return ec

        _, tekton_ec = resolve_junit_aggregate_exit(
            artifacts_path,
            (
                artifacts_path / "cluster-health.xml",
                artifacts_path / "operator-health.xml",
            ),
            raw_ec=worst,
        )
        return tekton_ec
    finally:
        if apps_cronjobs_suspended is not None:
            try:
                from steps.prepare_bvt_apps_namespace import resume_apps_cronjobs

                resume_apps_cronjobs(apps_cronjobs_suspended)
            except Exception as exc:
                print(
                    f"WARN: failed to restore apps CronJobs after BVT ({exc})",
                    file=sys.stderr,
                    flush=True,
                )
        if mlflow_op_prior is not None:
            try:
                from steps.prepare_bvt_apps_namespace import resume_mlflow_operator_reconcile

                resume_mlflow_operator_reconcile(prior_replicas=mlflow_op_prior)
            except Exception as exc:
                print(
                    f"WARN: failed to restore mlflow operator after BVT ({exc})",
                    file=sys.stderr,
                    flush=True,
                )


def main() -> int:
    if os.environ.get("BVT_SUITE", "").strip().lower() == "health":
        return run_health_suite()
    return run_single()


if __name__ == "__main__":
    raise SystemExit(main())
