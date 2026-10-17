"""Unit tests for inline SNAPSHOT JSON built by the rhoai_e2e trigger."""

from __future__ import annotations

from suite.constants import DEFAULT_APP, DEFAULT_NAMESPACE
import json
import unittest

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

