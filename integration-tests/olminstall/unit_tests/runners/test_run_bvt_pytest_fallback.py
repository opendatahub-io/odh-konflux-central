"""Tests for run_bvt_pytest uv/pip fallback behavior."""

from __future__ import annotations

import os
import tempfile
import unittest
from contextlib import ExitStack
from pathlib import Path
from unittest import mock
from unittest.mock import MagicMock

from runners import run_bvt_pytest

_UV = "/usr/bin/uv"


def _enter_uv_run_single_mocks(
    stack: ExitStack,
    *,
    env: dict[str, str],
    tests_root: str,
    run_return: int,
    patch_chdir: bool = False,
    skip_existing_junit: bool = False,
) -> MagicMock:
    stack.enter_context(mock.patch.dict("os.environ", env, clear=False))
    stack.enter_context(mock.patch.object(run_bvt_pytest, "_locate_tests_root", return_value=tests_root))
    stack.enter_context(mock.patch.object(run_bvt_pytest, "ensure_writable_kubeconfig"))
    stack.enter_context(mock.patch.object(run_bvt_pytest, "_ensure_pytest_kubeconfig_auth"))
    if patch_chdir:
        stack.enter_context(mock.patch.object(run_bvt_pytest.os, "chdir"))
    if skip_existing_junit:
        stack.enter_context(mock.patch.object(run_bvt_pytest.Path, "is_file", return_value=True))
    stack.enter_context(mock.patch.object(run_bvt_pytest.shutil, "which", return_value=_UV))
    return stack.enter_context(mock.patch.object(run_bvt_pytest, "_run_with_tee", return_value=run_return))


class RunBvtPytestFallbackTest(unittest.TestCase):
    def test_skips_pip_fallback_when_junit_exists(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            artifacts = Path(tmp)
            junit = artifacts / "workbenches-smoke.xml"
            junit.write_text(
                '<?xml version="1.0" encoding="utf-8"?>'
                '<testsuite name="workbenches" tests="1" failures="1" errors="0" skipped="0">'
                '<testcase classname="x" name="y"/></testsuite>',
                encoding="utf-8",
            )
            env = {
                "ARTIFACT_PREFIX": "workbenches-smoke",
                "ARTIFACTS_DIR": str(artifacts),
                "TESTS_SUBDIR": "tests/workbenches",
                "PYTEST_MARKER": "smoke",
                "TEST_ARTIFACTS_DIR": str(artifacts.parent),
            }
            tests_root = str(Path(tmp) / "opendatahub-tests")
            with ExitStack() as stack:
                run_mock = _enter_uv_run_single_mocks(
                    stack,
                    env=env,
                    tests_root=tests_root,
                    run_return=99,
                    patch_chdir=True,
                    skip_existing_junit=True,
                )
                ec = run_bvt_pytest.run_single()
            self.assertEqual(ec, 99)
            self.assertEqual(run_mock.call_count, 1)

    def test_uv_retry_passes_declining_timeout_budget(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            artifacts = Path(tmp)
            tests_root = Path(tmp) / "opendatahub-tests"
            (tests_root / "tests" / "ogx").mkdir(parents=True)
            (tests_root / "pyproject.toml").write_text("[project]\nname = 'odh'\n", encoding="utf-8")
            env = {
                "ARTIFACT_PREFIX": "ogx-smoke",
                "ARTIFACTS_DIR": str(artifacts),
                "TESTS_SUBDIR": "tests/ogx",
                "PYTEST_MARKER": "smoke",
                "TEST_ARTIFACTS_DIR": str(artifacts.parent),
                "COMPONENT_TEST_TIMEOUT_SECS": "120",
            }
            timeouts: list[float | None] = []
            call_count = 0

            elapsed = {"value": 0.0}

            def _monotonic() -> float:
                return elapsed["value"]

            def _advance_monotonic(seconds: float) -> None:
                elapsed["value"] += seconds

            def _side_effect(cmd, log, *, env=None, timeout=None):
                nonlocal call_count
                call_count += 1
                timeouts.append(timeout)
                if call_count == 1:
                    _advance_monotonic(40.0)
                    return 2
                if call_count == 2:
                    _advance_monotonic(40.0)
                    return 0
                return 1

            with ExitStack() as stack:
                stack.enter_context(mock.patch.dict("os.environ", env, clear=False))
                stack.enter_context(mock.patch.object(run_bvt_pytest, "_locate_tests_root", return_value=str(tests_root)))
                stack.enter_context(mock.patch.object(run_bvt_pytest, "ensure_writable_kubeconfig"))
                stack.enter_context(mock.patch.object(run_bvt_pytest, "_ensure_pytest_kubeconfig_auth"))
                stack.enter_context(mock.patch.object(run_bvt_pytest.os, "chdir"))
                stack.enter_context(mock.patch.object(run_bvt_pytest.shutil, "which", return_value=_UV))
                stack.enter_context(
                    mock.patch.object(run_bvt_pytest, "_run_with_tee", side_effect=_side_effect),
                )
                stack.enter_context(
                    mock.patch.object(run_bvt_pytest.time, "monotonic", side_effect=_monotonic),
                )
                ec = run_bvt_pytest.run_single()
            self.assertEqual(ec, 1)
            self.assertEqual(len(timeouts), 3)
            self.assertEqual(timeouts[0], 120.0)
            self.assertEqual(timeouts[1], 80.0)
            self.assertEqual(timeouts[2], 40.0)

    def test_results_dir_uses_writable_work_cwd_when_tests_root_read_only(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            artifacts = Path(tmp) / "artifacts"
            artifacts.mkdir()
            tests_root = Path(tmp) / "opendatahub-tests"
            (tests_root / "tests" / "cluster_health").mkdir(parents=True)
            (tests_root / "pyproject.toml").write_text("[project]\nname = 'odh'\n", encoding="utf-8")
            tests_root.chmod(0o555)
            env = {
                "ARTIFACT_PREFIX": "cluster-health",
                "ARTIFACTS_DIR": str(artifacts),
                "TESTS_SUBDIR": "tests/cluster_health",
                "TEST_ARTIFACTS_DIR": str(tmp),
            }
            try:
                with ExitStack() as stack:
                    run_mock = _enter_uv_run_single_mocks(
                        stack,
                        env=env,
                        tests_root=str(tests_root),
                        run_return=0,
                    )
                    ec = run_bvt_pytest.run_single()
            finally:
                tests_root.chmod(0o755)
            self.assertEqual(ec, 0)
            self.assertTrue((artifacts / "pytest-work-cwd" / "results").is_dir())
            self.assertEqual(run_mock.call_count, 1)
            cmd = run_mock.call_args.args[0]
            self.assertEqual(cmd[:4], [_UV, "--directory", str(tests_root), "run"])
            self.assertIn(str(tests_root / "tests" / "cluster_health"), cmd)

    def test_health_suite_runs_operator_health_from_cluster_health_dir(self) -> None:
        seen: list[tuple[str, str, str]] = []

        def _record_run_single() -> int:
            seen.append(
                (
                    run_bvt_pytest.os.environ.get("PYTEST_MARKER", ""),
                    run_bvt_pytest.os.environ.get("TESTS_SUBDIR", ""),
                    run_bvt_pytest.os.environ.get("ARTIFACT_PREFIX", ""),
                )
            )
            return 0

        env = {
            "BVT_SUITE": "health",
            "ARTIFACTS_DIR": "/artifacts",
            "CLUSTER_SOURCE": "EPHC",
            "PRODUCT": "rhoai",
        }
        with (
            mock.patch.dict("os.environ", env, clear=False),
            mock.patch.object(run_bvt_pytest, "_prepare_bvt_oc"),
            mock.patch(
                "steps.prepare_bvt_cluster_nodes.prepare_bvt_cluster_nodes",
                return_value=0,
            ),
            mock.patch("steps.prepare_bvt_dsc_ready.prepare_bvt_dsc_ready", return_value=0),
            mock.patch.object(run_bvt_pytest, "run_single", side_effect=_record_run_single),
            mock.patch.object(
                run_bvt_pytest,
                "resolve_junit_aggregate_exit",
                return_value=({}, 0),
            ),
        ):
            ec = run_bvt_pytest.run_health_suite()
        self.assertEqual(ec, 0)
        self.assertEqual(
            seen,
            [
                ("cluster_health", "tests/cluster_health", "cluster-health"),
                ("operator_health", "tests/cluster_health", "operator-health"),
            ],
        )

    def test_health_suite_external_rewaits_dashboard_before_apps_marker(self) -> None:
        seen: list[str] = []
        wait_calls = 0

        def _record_run_single() -> int:
            seen.append(run_bvt_pytest.os.environ.get("ARTIFACT_PREFIX", ""))
            return 0

        def _record_wait(**_kwargs: object) -> None:
            nonlocal wait_calls
            wait_calls += 1

        env = {
            "BVT_SUITE": "health",
            "ARTIFACTS_DIR": "/artifacts",
            "CLUSTER_SOURCE": "rh-nightly-pm-kubeconfig",
            "PRODUCT": "rhoai",
        }
        with (
            mock.patch.dict("os.environ", env, clear=False),
            mock.patch.object(run_bvt_pytest, "_prepare_bvt_oc"),
            mock.patch(
                "steps.prepare_bvt_cluster_nodes.prepare_bvt_cluster_nodes",
                return_value=0,
            ),
            mock.patch("steps.prepare_bvt_dsc_ready.prepare_bvt_dsc_ready", return_value=0),
            mock.patch(
                "steps.prepare_bvt_apps_namespace.suspend_apps_cronjobs_for_bvt",
                return_value=[],
            ),
            mock.patch(
                "steps.prepare_bvt_apps_namespace.pause_mlflow_operator_reconcile_for_bvt",
                return_value=None,
            ),
            mock.patch(
                "steps.prepare_bvt_apps_namespace.wait_dashboard_pods_ready_for_bvt",
                side_effect=_record_wait,
            ),
            mock.patch.object(run_bvt_pytest, "run_single", side_effect=_record_run_single),
            mock.patch.object(
                run_bvt_pytest,
                "resolve_junit_aggregate_exit",
                return_value=({}, 0),
            ),
        ):
            ec = run_bvt_pytest.run_health_suite()
        self.assertEqual(ec, 0)
        self.assertEqual(
            seen,
            [
                "cluster-health",
                "operator-health-core",
                "operator-health-apps",
            ],
        )
        self.assertEqual(wait_calls, 1)

    def test_health_suite_placeholder_when_external_without_odh_apis(self) -> None:
        env = {
            "BVT_SUITE": "health",
            "ARTIFACTS_DIR": "/artifacts",
            "CLUSTER_SOURCE": "olminstall-kubeconfig-test",
            "PRODUCT": "",
        }
        with (
            mock.patch.dict("os.environ", env, clear=False),
            mock.patch.object(run_bvt_pytest, "_prepare_bvt_oc"),
            mock.patch.object(run_bvt_pytest, "_cluster_has_odh_apis", return_value=False),
            mock.patch("runners.bvt_product_existing_placeholder_junit.main", return_value=0) as placeholder,
        ):
            ec = run_bvt_pytest.run_health_suite()
        self.assertEqual(ec, 0)
        placeholder.assert_called_once()

    def test_cluster_has_odh_apis_uses_staged_oc_path(self) -> None:
        with (
            mock.patch.object(run_bvt_pytest, "ensure_writable_kubeconfig"),
            mock.patch("k8s.oc_util._oc_path", return_value="/tmp/tests-payload/.tools/bin/oc"),
            mock.patch.object(
                run_bvt_pytest.subprocess,
                "run",
                return_value=mock.Mock(returncode=0, stdout="datascienceclusters\n"),
            ) as run_mock,
        ):
            self.assertTrue(run_bvt_pytest._cluster_has_odh_apis())
        self.assertEqual(run_mock.call_args.args[0][0], "/tmp/tests-payload/.tools/bin/oc")

    def test_prepare_bvt_oc_sets_oc_binary_path_for_staged_oc(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            artifacts = Path(tmp) / "artifacts"
            tools_bin = artifacts / "tests-payload" / ".tools" / "bin"
            tools_bin.mkdir(parents=True)
            staged = tools_bin / "oc"
            staged.write_bytes(b"")
            staged.chmod(0o755)
            prev_path = os.environ.get("PATH", "")
            with (
                mock.patch.dict("os.environ", {"ARTIFACTS_DIR": str(artifacts)}, clear=False),
                mock.patch("runners.orchestrator.stage_oc_for_pytest"),
            ):
                run_bvt_pytest._prepare_bvt_oc()
                self.assertEqual(os.environ.get("OC_BINARY_PATH"), str(staged))
            os.environ.pop("OC_BINARY_PATH", None)
            os.environ["PATH"] = prev_path
