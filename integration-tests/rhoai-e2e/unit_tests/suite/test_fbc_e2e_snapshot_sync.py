"""Unit tests for FBC override Snapshot sync planning."""

from __future__ import annotations

import json
import unittest

from suite.constants import LABEL_KONFLUX_COMPONENT, LABEL_SNAPSHOT_TYPE, SNAPSHOT_TYPE_OVERRIDE
from suite.fbc_e2e_snapshot_sync import (
    build_fbc_override_snapshot_json,
    parse_fbc_sync_slices,
    plan_fbc_e2e_snapshot_sync,
)
from suite.rh_nightly_auto_trigger import build_override_snapshot_labels


class _Resolver:
    def __init__(self, images: dict[str, str]) -> None:
        self._images = images

    def latest_named_component_image_on_application(
        self,
        namespace: str,
        app_name: str,
        component_name: str,
        image_pattern: str,
        *,
        required_catalog_line: str = "",
    ) -> tuple[str, str, dict | None]:
        _ = namespace, app_name, image_pattern, required_catalog_line
        img = self._images.get(component_name, "")
        return ("2026-01-01T00:00:00Z", img, None) if img else ("", "", None)


class TestFbcE2eSnapshotSync(unittest.TestCase):
    def test_parse_slices_default(self) -> None:
        self.assertEqual(parse_fbc_sync_slices(""), ["420", "421", "422"])

    def test_parse_slices_422_only(self) -> None:
        self.assertEqual(parse_fbc_sync_slices("422"), ["422"])

    def test_override_labels(self) -> None:
        labels = build_override_snapshot_labels(
            application="rhoai-fbc-fragment-ocp-422",
            fbc_component="rhoai-fbc-fragment-ocp-422",
        )
        self.assertEqual(labels[LABEL_SNAPSHOT_TYPE], SNAPSHOT_TYPE_OVERRIDE)
        self.assertEqual(labels[LABEL_KONFLUX_COMPONENT], "rhoai-fbc-fragment-ocp-422")

    def test_plan_skips_unchanged_digest(self) -> None:
        image = "quay.io/rhoai/rhoai-fbc-fragment@sha256:abc"
        state = {"fbc-e2e-sync:rhoai-fbc-fragment-ocp-420": {"digest": "abc"}}
        resolver = _Resolver({"rhoai-fbc-fragment-ocp-420": image})
        plans = plan_fbc_e2e_snapshot_sync(
            resolver=resolver,
            namespace="rhoai-tenant",
            slices=["420"],
            state=state,
            min_rhoai="3.5",
            force=False,
        )
        self.assertEqual(plans[0]["action"], "skip")

    def test_build_override_snapshot_json(self) -> None:
        body = build_fbc_override_snapshot_json(
            namespace="rhoai-tenant",
            application="rhoai-fbc-fragment-ocp-420",
            fbc_component="rhoai-fbc-fragment-ocp-420",
            fbc_image="quay.io/rhoai/rhoai-fbc-fragment@sha256:abc",
            labels={},
            annotations={"rhoai-e2e.trigger-type": "rh-nightly-auto"},
            git_url="https://github.com/opendatahub-io/odh-konflux-central.git",
            git_revision="main",
        )
        doc = json.loads(body)
        self.assertEqual(doc["metadata"]["labels"][LABEL_SNAPSHOT_TYPE], SNAPSHOT_TYPE_OVERRIDE)


if __name__ == "__main__":
    unittest.main()
