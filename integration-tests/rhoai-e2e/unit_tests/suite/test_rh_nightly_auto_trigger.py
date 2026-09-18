"""Unit tests for rh-nightly catalog sync helpers."""

from __future__ import annotations

import unittest

from suite.rh_nightly_auto_trigger import (
    build_auto_trigger_snapshot_yaml,
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
            application="testops-playpen",
            fbc_component="rhoai-fbc-fragment-ocp-420",
            fbc_image="quay.io/rhoai/rhoai-fbc-fragment@sha256:abc",
        )
        self.assertIn("application: testops-playpen", text)
        self.assertIn("name: rhoai-fbc-fragment-ocp-420", text)
        self.assertIn("containerImage: quay.io/rhoai/rhoai-fbc-fragment@sha256:abc", text)


if __name__ == "__main__":
    unittest.main()
