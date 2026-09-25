"""Unit tests for fast --run-its FBC snapshot lookup on one Konflux Application."""

from __future__ import annotations

import json
import unittest
from unittest.mock import MagicMock, patch

from runners.cli.cli import make_parser, parse_cli_args
from runners.cli.runner import RhoaiE2ERunner
from suite.constants import RHOAI_FBCF_IMAGE_REF_PATTERN, DEFAULT_APP, DEFAULT_NAMESPACE

_FBC_IMAGE = (
    "quay.io/rhoai/rhoai-fbc-fragment@sha256:"
    "feedfacefeedfacefeedfacefeedfacefeedfacefeedfacefeedfacefeedface"
)


class LatestNamedComponentImageOnApplicationTest(unittest.TestCase):
    def _runner(self) -> RhoaiE2ERunner:
        parser = make_parser()
        args = parse_cli_args(parser, ["--product", "rhoai"])
        return RhoaiE2ERunner(args)

    def test_returns_newest_matching_component_without_full_list_json(self) -> None:
        runner = self._runner()
        list_out = (
            "NAME  TS\n"
            "snap-old  2026-07-01T00:00:00Z\n"
            "snap-new  2026-07-10T00:00:00Z\n"
        )
        snap_json = json.dumps(
            {
                "metadata": {"name": "snap-new", "creationTimestamp": "2026-07-10T00:00:00Z"},
                "spec": {
                    "components": [
                        {
                            "name": "rhoai-fbc-fragment-ocp-421",
                            "containerImage": _FBC_IMAGE,
                        }
                    ]
                },
            }
        )

        def fake_run(cmd, **kwargs):
            del kwargs
            proc = MagicMock()
            proc.returncode = 0
            if cmd[:4] == ["oc", "get", "snapshots", "-n"]:
                proc.stdout = list_out
            else:
                proc.stdout = snap_json
            return proc

        with patch("runners.cli.runner_mixin_list.run_cmd", side_effect=fake_run):
            ts, img, meta = runner.latest_named_component_image_on_application(
                DEFAULT_NAMESPACE,
                "rhoai-fbc-fragment-ocp-421",
                "rhoai-fbc-fragment-ocp-421",
                RHOAI_FBCF_IMAGE_REF_PATTERN,
            )
        self.assertEqual(ts, "2026-07-10T00:00:00Z")
        self.assertEqual(img, _FBC_IMAGE)
        self.assertEqual((meta or {}).get("name"), "snap-new")

    def test_newest_snapshot_without_named_component_returns_empty(self) -> None:
        runner = self._runner()
        list_out = "NAME  TS\nsnap-a  2026-07-10T00:00:00Z\nsnap-b  2026-07-11T00:00:00Z\n"
        empty_comp = json.dumps(
            {
                "metadata": {"name": "snap-b"},
                "spec": {"components": [{"name": "other", "containerImage": _FBC_IMAGE}]},
            }
        )
        calls: list[str] = []

        def fake_run(cmd, **kwargs):
            del kwargs
            proc = MagicMock()
            proc.returncode = 0
            if cmd[:4] == ["oc", "get", "snapshots", "-n"]:
                proc.stdout = list_out
            else:
                snap = cmd[3]
                calls.append(snap)
                proc.stdout = empty_comp
            return proc

        with patch("runners.cli.runner_mixin_list.run_cmd", side_effect=fake_run):
            ts, img, meta = runner.latest_named_component_image_on_application(
                DEFAULT_NAMESPACE,
                "rhoai-fbc-fragment-ocp-421",
                "rhoai-fbc-fragment-ocp-421",
                RHOAI_FBCF_IMAGE_REF_PATTERN,
            )
        self.assertEqual(calls, ["snap-b"])
        self.assertEqual(ts, "")
        self.assertEqual(img, "")
        self.assertIsNone(meta)
