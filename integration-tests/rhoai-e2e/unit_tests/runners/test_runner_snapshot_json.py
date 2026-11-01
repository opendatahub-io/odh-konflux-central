"""Unit tests for inline SNAPSHOT JSON built by the rhoai_e2e trigger."""

from __future__ import annotations

from suite.constants import DEFAULT_APP, DEFAULT_NAMESPACE
import json
import unittest
from pathlib import Path

from runners.cli.cli import make_parser, parse_cli_args
from runners.cli.runner import RhoaiE2ERunner

class BuildSnapshotJsonTest(unittest.TestCase):
    def _runner(self, argv: list[str]) -> RhoaiE2ERunner:
        parser = make_parser()
        args = parse_cli_args(parser, argv)
        return RhoaiE2ERunner(args)

    def test_existing_omits_container_image(self) -> None:
        runner = self._runner([])
        spec = json.loads(runner._build_snapshot_json(odh_overrides=False))
        comp = spec["components"][0]
        self.assertNotIn("containerImage", comp)
        self.assertIn("source", comp)
        self.assertEqual(spec["application"], DEFAULT_APP)

    def test_existing_with_image_includes_container_image(self) -> None:
        pullspec = "quay.io/rhoai/rhoai-fbc-fragment@sha256:deadbeef"
        runner = self._runner(["--image", pullspec])
        spec = json.loads(runner._build_snapshot_json(odh_overrides=False))
        self.assertEqual(spec["components"][0]["containerImage"], pullspec)

    def test_rhoai_fallback_uses_yaml_pin_when_no_cli_image(self) -> None:
        runner = self._runner(["--product", "rhoai"])
        runner.image = ""
        spec = json.loads(runner._build_snapshot_json(odh_overrides=False))
        img = spec["components"][0].get("containerImage", "")
        self.assertTrue(img.startswith("quay.io/rhoai/rhoai-fbc-fragment@sha256:"))

    def test_odh_overrides_component_name(self) -> None:
        runner = self._runner([])
        spec = json.loads(runner._build_snapshot_json(odh_overrides=True))
        self.assertEqual(spec["components"][0]["name"], "odh-operator-catalog")

    def test_read_its_params_from_colocated_manifest(self) -> None:
        manifest = (
            Path(__file__).resolve().parents[2]
            / "tekton/its/its-e2e-ephc-ocp420-wb-mr-msrv-mrt-maas.yaml"
        )
        runner = self._runner(
            [
                "--run-its",
                "e2e-ephc-ocp420-wb-mr-msrv-mrt-maas",
                "--konflux-app",
                "testops-playpen",
            ]
        )
        runner._stage_its_manifest_tmp(manifest, push_context=False)
        params = runner._read_its_params_from_tmp()
        self.assertEqual(params.get("RHOAI_FBC_NAME"), "rhoai-fbc-fragment-ocp-420")
        self.assertEqual(params.get("CLUSTER_SOURCE"), "EPHC")
        url, rev, path_in_repo = runner._read_its_git_resolver()
        self.assertIn("its-e2e-ephc-ocp420-wb-mr-msrv-mrt-maas.yaml", path_in_repo)

