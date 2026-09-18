"""Tests for pipeline run context formatting."""

from __future__ import annotations

import unittest

from suite.constants import TRIGGER_TYPE_MANUAL, TRIGGER_TYPE_RH_NIGHTLY_AUTO
from suite.pipeline_run_context import (
    TRIGGER_CONTEXT_RESULT_NAMES,
    build_pipeline_run_context_lines,
    build_pipeline_run_context_message,
    build_pipeline_run_context_results,
    context_from_pipelinerun_json,
    fbc_image_from_snapshot,
    short_digest,
)

_FBC = (
    "quay.io/rhoai/rhoai-fbc-fragment@sha256:"
    "d9f54f26a526be21e0806a5c36b7d929b5861cffa68bcca57825fb878ecb40a2"
)
_SNAPSHOT = (
    '{"metadata":{"name":"rh-nightly-snap-4mh9f"},'
    '"components":[{"name":"rhoai-fbc-fragment-ocp-420","containerImage":"'
    + _FBC
    + '"}]}'
)


class PipelineRunContextTest(unittest.TestCase):
    def test_short_digest(self) -> None:
        self.assertEqual(short_digest(_FBC), "d9f54f26a526…")

    def test_fbc_image_from_snapshot(self) -> None:
        self.assertEqual(
            fbc_image_from_snapshot(_SNAPSHOT, "rhoai-fbc-fragment-ocp-420"),
            _FBC,
        )

    def test_rh_nightly_catalog_sync_log_lines(self) -> None:
        lines = build_pipeline_run_context_lines(
            pipelinerun_name="e2e-its-rh-nightly-pm-smoke-2gh84",
            trigger_type=TRIGGER_TYPE_RH_NIGHTLY_AUTO,
            konflux_event="push",
            snapshot_name="rh-nightly-snap-4mh9f",
            fbc_component="rhoai-fbc-fragment-ocp-420",
            fbc_image=_FBC,
            cluster_source="rhoai-e2e-kubeconfig-rh-nightly-pm",
            product="rhoai",
            test_gates="bvt,smoke",
            trigger_command=(
                "python3 integration-tests/rhoai-e2e/rhoai_e2e.py "
                "--enable-its rhoai-e2e-rh-nightly-pm-ocp420 --konflux-namespace rhoai-tenant"
            ),
        )
        text = "\n".join(lines)
        self.assertIn("rh-nightly catalog sync", text)
        self.assertIn("rh-nightly-snap-4mh9f", text)
        self.assertIn("rhoai-fbc-fragment-ocp-420", text)
        self.assertIn("--enable-its", text)

    def test_cli_direct_task_message(self) -> None:
        msg = build_pipeline_run_context_message(
            trigger_type=TRIGGER_TYPE_MANUAL,
            konflux_event="incoming",
            fbc_component="rhoai-fbc-fragment-ocp-420",
            fbc_image=_FBC,
            cluster_source="rhoai-e2e-kubeconfig-rh-nightly-pm",
            product="rhoai",
            test_gates="bvt,smoke",
            trigger_command="python3 integration-tests/rhoai-e2e/rhoai_e2e.py --run-its rhoai-e2e-rh-nightly-pm-ocp420",
        )
        self.assertIn("CLI direct", msg)
        self.assertIn("Incoming", msg)
        self.assertIn("--run-its", msg)

    def test_cli_direct_trigger_context_results(self) -> None:
        results = build_pipeline_run_context_results(
            trigger_type=TRIGGER_TYPE_MANUAL,
            konflux_event="incoming",
            fbc_component="rhoai-fbc-fragment-ocp-420",
            fbc_image=_FBC,
            cluster_source="rhoai-e2e-kubeconfig-rh-nightly-pm",
            product="rhoai",
            test_gates="bvt,smoke",
            trigger_command="python3 integration-tests/rhoai-e2e/rhoai_e2e.py --run-its rhoai-e2e-rh-nightly-pm-ocp420",
        )
        self.assertEqual(tuple(results.keys()), TRIGGER_CONTEXT_RESULT_NAMES)
        self.assertIn("CLI direct", results["TRIGGER"])
        self.assertIn("Incoming", results["KONFLUX_EVENT"])
        self.assertIn("rhoai-fbc-fragment-ocp-420", results["FBC"])
        self.assertIn("--run-its", results["TRIGGER_CMD"])

    def test_its_only_upstream_task_message(self) -> None:
        msg = build_pipeline_run_context_message(
            konflux_event="push",
            snapshot_name="rhoai-fbc-fragment-ocp-420-build-abc",
            fbc_component="rhoai-fbc-fragment-ocp-420",
            fbc_image=_FBC,
            cluster_source="rhoai-e2e-kubeconfig-rh-nightly-pm",
            product="rhoai",
            test_gates="bvt,smoke",
        )
        self.assertIn("Integration Service", msg)
        self.assertIn("TRIGGER_CMD: (none", msg)
        self.assertNotIn("rh-nightly catalog sync", msg)

    def test_context_from_pipelinerun_json(self) -> None:
        prj = {
            "metadata": {
                "name": "e2e-its-rh-nightly-pm-smoke-2gh84",
                "annotations": {
                    "rhoai-e2e.trigger-type": TRIGGER_TYPE_RH_NIGHTLY_AUTO,
                    "rhoai-e2e.trigger-command": "python3 integration-tests/rhoai-e2e/rhoai_e2e.py --enable-its rhoai-e2e-rh-nightly-pm-ocp420",
                    "rhoai-e2e.fbcf-image": _FBC,
                },
                "labels": {
                    "appstudio.openshift.io/snapshot": "rh-nightly-snap-4mh9f",
                    "pac.test.appstudio.openshift.io/event-type": "push",
                },
            }
        }
        ctx = context_from_pipelinerun_json(
            prj,
            snapshot_raw=_SNAPSHOT,
            fbc_component="rhoai-fbc-fragment-ocp-420",
            cluster_source="rhoai-e2e-kubeconfig-rh-nightly-pm",
            product="rhoai",
            test_gates="bvt,smoke",
        )
        self.assertEqual(ctx["trigger_type"], TRIGGER_TYPE_RH_NIGHTLY_AUTO)
        self.assertEqual(ctx["snapshot_name"], "rh-nightly-snap-4mh9f")
        self.assertEqual(ctx["fbc_image"], _FBC)

    def test_context_unpacks_into_task_message(self) -> None:
        ctx = context_from_pipelinerun_json(
            {
                "metadata": {
                    "name": "e2e-cli-nmanos-nmanos-konflux1-bvt-smoke-xnkvp",
                    "annotations": {
                        "rhoai-e2e.trigger-type": TRIGGER_TYPE_MANUAL,
                        "rhoai-e2e.trigger-command": "python3 integration-tests/rhoai-e2e/rhoai_e2e.py",
                    },
                    "labels": {"pac.test.appstudio.openshift.io/event-type": "incoming"},
                }
            },
            fbc_component="rhoai-fbc-fragment-ocp-421",
            cluster_source="rhoai-e2e-kubeconfig-nmanos-konflux1-nmanos",
            product="",
            test_gates="bvt,smoke",
        )
        msg = build_pipeline_run_context_message(**ctx)
        self.assertIn("CLI direct", msg)


if __name__ == "__main__":
    unittest.main()
