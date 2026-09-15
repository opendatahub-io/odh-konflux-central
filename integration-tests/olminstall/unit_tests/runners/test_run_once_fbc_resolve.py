"""Unit tests for --run-its FBC image resolution (no cluster)."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from runners.cli.cli import make_parser, parse_cli_args
from runners.cli.runner import OLMInstallRunner

_ROOT = Path(__file__).resolve().parents[2]
_RH_NIGHTLY_SNAP = _ROOT / "config" / "test-snapshot-rh-nightly.yaml"
_RH_NIGHTLY_ITS = _ROOT / "tekton" / "its" / "its-rhoai-e2e-rh-nightly-pm-ocp420.yaml"
_EPHC_ITS = _ROOT / "tekton" / "its" / "its-rhoai-e2e-ephc-ocp421.yaml"
_EPHC_422_ITS = _ROOT / "tekton" / "its" / "its-rhoai-e2e-ephc-ocp422.yaml"
_ODH_ITS = _ROOT / "tekton" / "its" / "its-olminstall-open-data-hub-tenant.yaml"
_PINNED_420 = (
    "quay.io/rhoai/rhoai-fbc-fragment@sha256:"
    "d9f54f26a526be21e0806a5c36b7d929b5861cffa68bcca57825fb878ecb40a2"
)
_LATEST_420 = (
    "quay.io/rhoai/rhoai-fbc-fragment@sha256:"
    "feedfacefeedfacefeedfacefeedfacefeedfacefeedfacefeedfacefeedface"
)


class RunItsManifestDefaultsTest(unittest.TestCase):
    def _runner(self) -> OLMInstallRunner:
        parser = make_parser()
        args = parse_cli_args(
            parser,
            ["--run-its", "rhoai-e2e-rh-nightly-pm-ocp420"],
        )
        runner = OLMInstallRunner(args)
        runner.snapshot_file = _RH_NIGHTLY_SNAP
        return runner

    def test_run_its_stores_pin_but_does_not_set_image(self) -> None:
        runner = self._runner()
        runner._apply_run_its_manifest_defaults(_RH_NIGHTLY_ITS)
        self.assertEqual(runner.args.product, "rhoai")
        self.assertEqual(runner.args.ocp_version, "4.20")
        self.assertEqual(runner.resolved_rhoai_fbc_name, "rhoai-fbc-fragment-ocp-420")
        self.assertEqual(runner._run_its_pinned_fbcf_image, _PINNED_420)
        self.assertEqual(runner.image, "")

    def test_run_its_ephc_does_not_set_ocp_prefix_from_latest_default_label(self) -> None:
        parser = make_parser()
        args = parse_cli_args(parser, ["--run-its", "rhoai-e2e-ephc-ocp421"])
        runner = OLMInstallRunner(args)
        runner._apply_run_its_manifest_defaults(_EPHC_ITS)
        self.assertEqual(runner.args.ocp_version, "")
        self.assertEqual(runner.resolved_rhoai_fbc_name, "rhoai-fbc-fragment-ocp-421")

    def test_run_its_keeps_manifest_cluster_source_without_cli_override(self) -> None:
        runner = self._runner()
        runner._apply_run_its_manifest_defaults(_RH_NIGHTLY_ITS)
        self.assertEqual(runner.external_kubeconfig_secret, "olminstall-kubeconfig-rh-nightly-pm")

    def test_run_its_cli_external_kubeconfig_overrides_manifest_cluster_source(self) -> None:
        with tempfile.NamedTemporaryFile(suffix=".kubeconfig", delete=False) as tf:
            kubeconfig = tf.name
        runner = self._runner()
        runner.args.external_kubeconfig = kubeconfig
        runner.args.external_kubeconfig_path = Path(kubeconfig)
        runner._apply_run_its_manifest_defaults(_RH_NIGHTLY_ITS)
        self.assertEqual(runner.external_kubeconfig_secret, "")


class RunItsComponentsParamTest(unittest.TestCase):
    def test_run_its_preserves_slice_b_components_after_cli_override_clear(self) -> None:
        parser = make_parser()
        args = parse_cli_args(parser, ["--run-its", "rhoai-e2e-ephc-ocp422"])
        runner = OLMInstallRunner(args)
        runner.its_file = _EPHC_422_ITS
        runner._stage_its_manifest_tmp(_EPHC_422_ITS, push_context=False)
        runner._apply_run_its_manifest_defaults(_EPHC_422_ITS)
        runner._apply_run_its_cli_overrides(odh_overrides=False)
        params = runner._read_its_params_from_tmp()
        self.assertEqual(params.get("COMPONENTS"), "dashboard_cypress,platform")

    def test_run_its_preserves_slice_a_components_after_cli_override_clear(self) -> None:
        parser = make_parser()
        args = parse_cli_args(parser, ["--run-its", "rhoai-e2e-ephc-ocp421"])
        runner = OLMInstallRunner(args)
        runner.its_file = _EPHC_ITS
        runner._stage_its_manifest_tmp(_EPHC_ITS, push_context=False)
        runner._apply_run_its_manifest_defaults(_EPHC_ITS)
        runner._apply_run_its_cli_overrides(odh_overrides=False)
        params = runner._read_its_params_from_tmp()
        components = params.get("COMPONENTS", "")
        self.assertIn("workbenches", components)
        self.assertNotIn("dashboard_cypress", components)
        self.assertNotIn("platform", components)

    def test_run_its_sets_args_components_from_manifest(self) -> None:
        parser = make_parser()
        args = parse_cli_args(parser, ["--run-its", "rhoai-e2e-ephc-ocp422"])
        runner = OLMInstallRunner(args)
        runner.its_file = _EPHC_422_ITS
        runner._apply_run_its_manifest_defaults(_EPHC_422_ITS)
        self.assertEqual(runner.args.components, "dashboard_cypress,platform")


class RunItsUpdateChannelTest(unittest.TestCase):
    def test_run_its_patch_uses_staged_manifest_update_channel(self) -> None:
        parser = make_parser()
        args = parse_cli_args(
            parser,
            ["--run-its", "tekton/its/its-olminstall-open-data-hub-tenant.yaml"],
        )
        runner = OLMInstallRunner(args)
        runner._stage_its_manifest_tmp(_ODH_ITS, push_context=False)
        runner._patch_its_cli_override_params(runner.its_apply_tmp, odh_overrides=True)
        from k8s.oc_util import run_cmd

        proc = run_cmd(
            [
                "yq",
                "e",
                '(.spec.params[] | select(.name == "UPDATE_CHANNEL") | .value) // ""',
                runner.its_apply_tmp,
            ],
            capture=True,
            check=True,
        )
        self.assertEqual(proc.stdout.strip().strip('"'), "odh-stable")


class ResolveRhoaiFbcForItsApplicationTest(unittest.TestCase):
    def _runner(self, *, run_its: str = "rhoai-e2e-ephc-ocp421") -> OLMInstallRunner:
        parser = make_parser()
        args = parse_cli_args(parser, ["--run-its", run_its])
        runner = OLMInstallRunner(args)
        runner.resolved_rhoai_fbc_name = "rhoai-fbc-fragment-ocp-421"
        return runner

    def test_uses_fast_lookup_on_its_application_only(self) -> None:
        runner = self._runner()
        with patch.object(
            runner,
            "latest_named_component_image_on_application",
            return_value=("2026-07-10T00:00:00Z", _LATEST_420, {"name": "snap-1"}),
        ) as mock_fast:
            runner._resolve_rhoai_fbc_for_its_application(
                "rhoai-fbc-fragment-ocp-421",
                "rhoai-fbc-fragment-ocp-421",
            )
        mock_fast.assert_called_once()
        self.assertEqual(runner.image, _LATEST_420)
        self.assertEqual(runner.resolved_app, "rhoai-fbc-fragment-ocp-421")

    def test_resolve_image_run_its_calls_its_application_path(self) -> None:
        runner = self._runner()
        runner.args.product = "rhoai"
        runner.resolved_rhoai_fbc_name = "rhoai-fbc-fragment-ocp-421"
        with patch.object(
            runner,
            "_resolve_rhoai_fbc_for_its_application",
        ) as mock_its, patch.object(
            runner,
            "_resolve_rhoai_fbc_latest_for_component",
        ) as mock_full:
            runner.resolve_image(odh_overrides=False)
        mock_its.assert_called_once_with("rhoai-fbc-fragment-ocp-421", "rhoai-fbc-fragment-ocp-421")
        mock_full.assert_not_called()


class ResolveRhoaiFbcLatestForComponentTest(unittest.TestCase):
    def _runner(self) -> OLMInstallRunner:
        parser = make_parser()
        args = parse_cli_args(parser, ["--product", "rhoai"])
        runner = OLMInstallRunner(args)
        runner.resolved_rhoai_fbc_name = "rhoai-fbc-fragment-ocp-420"
        return runner

    def test_picks_highest_version_app_with_newest_snapshot(self) -> None:
        runner = self._runner()

        def fake_latest(
            namespace: str,
            app: str,
            component_name: str,
            image_pattern: str,
        ) -> tuple[str, str, dict | None]:
            del namespace, component_name, image_pattern
            images = {
                "rhoai-fbc-fragment-ocp-420": ("2026-07-01T00:00:00Z", _PINNED_420),
                "rhoai-v3-4-foo": ("2026-07-02T00:00:00Z", "quay.io/rhoai/rhoai-fbc-fragment@sha256:3400"),
                "rhoai-v3-5-ea-2": ("2026-07-08T00:00:00Z", _LATEST_420),
            }
            ts, img = images.get(app, ("", ""))
            return ts, img, None

        with patch.object(runner, "get_applications", return_value=["rhoai-v3-4-foo", "rhoai-v3-5-ea-2"]):
            with patch.object(runner, "latest_named_component_image", side_effect=fake_latest):
                runner._resolve_rhoai_fbc_latest_for_component("rhoai-fbc-fragment-ocp-420")
        self.assertEqual(runner.image, _LATEST_420)
        self.assertEqual(runner.resolved_app, "rhoai-v3-5-ea-2")

    def test_prefers_fbc_fragment_app_when_newest(self) -> None:
        runner = self._runner()
        _FBC_APP_IMG = (
            "quay.io/rhoai/rhoai-fbc-fragment@sha256:"
            "abcabcabcabcabcabcabcabcabcabcabcabcabcabcabcabcabcabcabcabcabca"
        )

        def fake_latest(
            namespace: str,
            app: str,
            component_name: str,
            image_pattern: str,
        ) -> tuple[str, str, dict | None]:
            del namespace, component_name, image_pattern
            images = {
                "rhoai-fbc-fragment-ocp-420": ("2026-07-10T00:00:00Z", _FBC_APP_IMG),
                "rhoai-v3-5-ea-2": ("2026-07-08T00:00:00Z", _LATEST_420),
            }
            ts, img = images.get(app, ("", ""))
            return ts, img, None

        with patch.object(runner, "get_applications", return_value=["rhoai-v3-5-ea-2"]):
            with patch.object(runner, "latest_named_component_image", side_effect=fake_latest):
                runner._resolve_rhoai_fbc_latest_for_component("rhoai-fbc-fragment-ocp-420")
        self.assertEqual(runner.image, _FBC_APP_IMG)
        self.assertEqual(runner.resolved_app, "rhoai-fbc-fragment-ocp-420")

    def test_fbc_fragment_app_only_when_no_rhoai_v_apps(self) -> None:
        runner = self._runner()

        def fake_latest(
            namespace: str,
            app: str,
            component_name: str,
            image_pattern: str,
        ) -> tuple[str, str, dict | None]:
            del namespace, component_name, image_pattern
            if app == "rhoai-fbc-fragment-ocp-420":
                return "2026-07-03T00:00:00Z", _LATEST_420, None
            return "", "", None

        with patch.object(runner, "get_applications", return_value=[]):
            with patch.object(runner, "latest_named_component_image", side_effect=fake_latest):
                runner._resolve_rhoai_fbc_latest_for_component("rhoai-fbc-fragment-ocp-420")
        self.assertEqual(runner.image, _LATEST_420)
        self.assertEqual(runner.resolved_app, "rhoai-fbc-fragment-ocp-420")

    def test_falls_back_to_snapshot_pin_when_konflux_empty(self) -> None:
        runner = self._runner()
        runner.snapshot_file = _RH_NIGHTLY_SNAP
        runner._run_its_pinned_fbcf_image = _PINNED_420
        with patch.object(runner, "get_applications", return_value=[]):
            runner._resolve_rhoai_fbc_latest_for_component("rhoai-fbc-fragment-ocp-420")
        runner._apply_pinned_fbcf_fallback(reason="test")
        self.assertEqual(runner.image, _PINNED_420)


class RhoaiAppVersionKeyTest(unittest.TestCase):
    def test_parses_major_minor(self) -> None:
        self.assertEqual(
            OLMInstallRunner._rhoai_app_version_key("rhoai-v3-5-ea-2"),
            (3, 5),
        )

    def test_ignores_non_numeric_segments(self) -> None:
        self.assertEqual(
            OLMInstallRunner._rhoai_app_version_key("rhoai-v3-4-foo"),
            (3, 4),
        )


class ResolveRhoaiFbcVersionStreamTest(unittest.TestCase):
    _V35_IMG = (
        "quay.io/rhoai/rhoai-fbc-fragment@sha256:"
        "22cfc6658a66a16ed54fc0ebe5558165cbf8c345c8984124ff90410bff8e8cf7"
    )

    def _runner(self) -> OLMInstallRunner:
        parser = make_parser()
        args = parse_cli_args(
            parser,
            [
                "--run-its",
                "rhoai-e2e-rh-nightly-pm-ocp420",
                "--product",
                "rhoai",
                "--rhoai-version",
                "3.5",
            ],
        )
        runner = OLMInstallRunner(args)
        runner.args.ocp_version = "4.20"
        return runner

    def test_resolve_image_uses_version_stream_component_not_stale_ocp_app(self) -> None:
        runner = self._runner()
        stale = (
            "quay.io/rhoai/rhoai-fbc-fragment@sha256:"
            "b279a6c801d7deadbeefdeadbeefdeadbeefdeadbeefdeadbeefdeadbeef"
        )

        def fake_latest(
            namespace: str,
            app: str,
            component_name: str,
            image_pattern: str,
        ) -> tuple[str, str, dict | None]:
            del namespace, image_pattern
            if app == "rhoai-v3-5" and component_name == "rhoai-fbc-fragment-v3-5":
                return "2026-08-17T00:00:00Z", self._V35_IMG, None
            if app == "rhoai-v3-5" and component_name == "rhoai-fbc-fragment-ocp-420":
                return "", "", None
            return "", "", None

        with patch.object(
            runner,
            "get_applications",
            return_value=["rhoai-v3-5", "rhoai-fbc-fragment-ocp-420"],
        ), patch.object(
            runner,
            "latest_named_component_image_on_application",
            side_effect=fake_latest,
        ), patch.object(
            runner,
            "latest_matching_image",
            return_value=("2026-08-14T00:00:00Z", stale, None),
        ) as mock_fallback:
            runner.resolve_image(odh_overrides=False)
        mock_fallback.assert_not_called()
        self.assertEqual(runner.image, self._V35_IMG)
        self.assertEqual(runner.resolved_app, "rhoai-v3-5")
        self.assertEqual(runner.resolved_rhoai_fbc_name, "rhoai-fbc-fragment-v3-5")

    def test_run_its_patch_rhoai_fbc_name_after_version_stream_resolve(self) -> None:
        runner = self._runner()
        runner._stage_its_manifest_tmp(_RH_NIGHTLY_ITS, push_context=False)
        runner._apply_run_its_manifest_defaults(_RH_NIGHTLY_ITS)

        def fake_latest(
            namespace: str,
            app: str,
            component_name: str,
            image_pattern: str,
        ) -> tuple[str, str, dict | None]:
            del namespace, image_pattern
            if app == "rhoai-v3-5" and component_name == "rhoai-fbc-fragment-v3-5":
                return "2026-08-17T00:00:00Z", self._V35_IMG, None
            return "", "", None

        with patch.object(
            runner,
            "get_applications",
            return_value=["rhoai-v3-5"],
        ), patch.object(
            runner,
            "latest_named_component_image_on_application",
            side_effect=fake_latest,
        ), patch.object(
            runner,
            "latest_matching_image",
            return_value=("", "", None),
        ):
            runner.resolve_image(odh_overrides=False)
            runner._apply_run_its_cli_overrides(odh_overrides=False)

        params = runner._read_its_params_from_tmp()
        self.assertEqual(params.get("RHOAI_FBC_NAME"), "rhoai-fbc-fragment-v3-5")
        self.assertIn(self._V35_IMG, params.get("RHOAI_FBC_IMAGE", ""))

    def test_resolve_image_skips_ocp_fragment_when_auto_channel_is_stable(self) -> None:
        parser = make_parser()
        args = parse_cli_args(
            parser,
            [
                "--product",
                "rhoai",
                "--rhoai-version",
                "3.5",
                "--ocp-version",
                "4.21",
            ],
        )
        runner = OLMInstallRunner(args)
        ocp_img = (
            "quay.io/rhoai/rhoai-fbc-fragment@sha256:"
            "deadbeefdeadbeefdeadbeefdeadbeefdeadbeefdeadbeefdeadbeefdeadbeef"
        )

        def fake_latest(
            namespace: str,
            app: str,
            component_name: str,
            image_pattern: str,
        ) -> tuple[str, str, dict | None]:
            del namespace, image_pattern
            if app == "rhoai-v3-5" and component_name == "rhoai-fbc-fragment-v3-5":
                return "", "", None
            if app == "rhoai-v3-5" and component_name == "rhoai-fbc-fragment-ocp-421":
                return "2026-08-17T00:00:00Z", ocp_img, None
            return "", "", None

        with patch.object(
            runner,
            "get_applications",
            return_value=["rhoai-v3-5", "rhoai-fbc-fragment-ocp-421"],
        ), patch.object(
            runner,
            "latest_named_component_image_on_application",
            side_effect=fake_latest,
        ), patch.object(
            runner,
            "latest_matching_image",
            return_value=("", "", None),
        ):
            with self.assertRaises(Exception) as ctx:
                runner.resolve_image(odh_overrides=False)
        self.assertIn("No FBCF snapshot found", str(ctx.exception))
        self.assertNotIn("rhoai-fbc-fragment-ocp-421", str(ctx.exception))


class RunItsPostTriggerWatchTest(unittest.TestCase):
    def test_run_its_returns_post_trigger_watch_exit_code(self) -> None:
        parser = make_parser()
        args = parse_cli_args(
            parser,
            ["--run-its", "rhoai-e2e-rh-nightly-pm-ocp420"],
        )
        runner = OLMInstallRunner(args)
        with patch.object(runner, "_stage_its_manifest_tmp"), patch.object(
            runner, "_apply_run_its_manifest_defaults"
        ), patch.object(runner, "_print_effective_trigger_context"), patch.object(
            runner, "_apply_run_its_cli_overrides"
        ), patch.object(
            runner, "_apply_konflux_git_inference_from_clone_or_env"
        ), patch.object(runner, "get_pipelineruns", return_value=[]), patch.object(
            runner, "find_owned_live_watch_pr", return_value=""
        ), patch.object(
            runner, "_prepare_external_cluster_before_trigger"
        ), patch.object(runner, "resolve_image"), patch.object(
            runner, "_run_its_generate_prefix", return_value="e2e-cli-"
        ), patch.object(runner, "create_direct_pipelinerun"), patch.object(
            runner, "_run_post_trigger_watch", return_value=0
        ) as mock_watch:
            self.assertEqual(runner.run_integration_test_scenario(), 0)
        mock_watch.assert_called_once()
