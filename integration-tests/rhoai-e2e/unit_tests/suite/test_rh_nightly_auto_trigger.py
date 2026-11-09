"""Unit tests for rh-nightly catalog sync helpers."""

from __future__ import annotations

import unittest

from suite.constants import DEFAULT_APP, DEFAULT_NAMESPACE
from suite.constants import LABEL_SNAPSHOT_TYPE, SNAPSHOT_TYPE_OVERRIDE
from suite.rh_nightly_auto_trigger import (
    build_auto_trigger_snapshot_doc,
    build_auto_trigger_snapshot_yaml,
    build_override_snapshot_labels,
    decide_auto_trigger,
    image_digest,
    rhoai_fbc_component_meets_min_version,
)


class TestRhNightlyAutoTrigger(unittest.TestCase):
    def test_image_digest(self) -> None:
        self.assertEqual(
            image_digest("quay.io/rhoai/rhoai-fbc-fragment@sha256:abc123"),
            "abc123",
        )

    def test_meets_min_version_ocp_and_v35(self) -> None:
        self.assertTrue(rhoai_fbc_component_meets_min_version("rhoai-fbc-fragment-ocp-420"))
        self.assertTrue(rhoai_fbc_component_meets_min_version("rhoai-fbc-fragment-v3-5-ea-2"))
        self.assertFalse(rhoai_fbc_component_meets_min_version("rhoai-fbc-fragment-v3-4"))

    def test_decide_skip_unchanged_digest(self) -> None:
        image = "quay.io/rhoai/rhoai-fbc-fragment@sha256:deadbeef"
        state = {
            "rh-nightly-pm:rhoai-fbc-fragment-ocp-420": {"digest": "deadbeef"},
        }
        decision = decide_auto_trigger(
            cluster_id="rh-nightly-pm",
            fbc_component="rhoai-fbc-fragment-ocp-420",
            fbc_image=image,
            state=state,
        )
        self.assertEqual(decision.action, "skip")
        self.assertIn("catalog digest unchanged", decision.reason)

    def test_decide_trigger_new_digest(self) -> None:
        decision = decide_auto_trigger(
            cluster_id="rh-nightly-pm",
            fbc_component="rhoai-fbc-fragment-ocp-420",
            fbc_image="quay.io/rhoai/rhoai-fbc-fragment@sha256:newdigest",
            state={},
        )
        self.assertEqual(decision.action, "trigger")
        self.assertEqual(decision.digest, "newdigest")

    def test_build_snapshot_yaml(self) -> None:
        text = build_auto_trigger_snapshot_yaml(
            application=DEFAULT_APP,
            fbc_component="rhoai-fbc-fragment-ocp-420",
            fbc_image="quay.io/rhoai/rhoai-fbc-fragment@sha256:abc",
        )
        self.assertIn(f"application: {DEFAULT_APP}", text)
        self.assertIn("name: rhoai-fbc-fragment-ocp-420", text)
        self.assertIn("containerImage: quay.io/rhoai/rhoai-fbc-fragment@sha256:abc", text)

    def test_override_snapshot_doc_labels(self) -> None:
        labels = build_override_snapshot_labels(
            application=DEFAULT_APP,
            fbc_component="rhoai-fbc-fragment-ocp-420",
        )
        doc = build_auto_trigger_snapshot_doc(
            namespace=DEFAULT_NAMESPACE,
            application=DEFAULT_APP,
            fbc_component="rhoai-fbc-fragment-ocp-420",
            fbc_image="quay.io/rhoai/rhoai-fbc-fragment@sha256:abc",
            labels=labels,
        )
        self.assertEqual(doc["metadata"]["labels"][LABEL_SNAPSHOT_TYPE], SNAPSHOT_TYPE_OVERRIDE)


if __name__ == "__main__":
    unittest.main()
