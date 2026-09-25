"""Tests for trigger-layer param registry (CLEANUP / CLUSTER_SOURCE)."""

from __future__ import annotations

import unittest

from suite.its_trigger_params import CLUSTER_SOURCE_EPHC
from suite.trigger_param_registry import (
    TriggerContext,
    apply_trigger_param_resolution,
    build_trigger_context_from_args,
    resolve_trigger_params,
    resolve_trigger_patch_plan,
)

_LABELS_35 = {
    "pac.test.appstudio.openshift.io/original-prname": "rhoai-fbc-fragment-rhoai-35-ea2-ocp-421-on-push",
}
_ANNOTATIONS_35 = {
    "pac.test.appstudio.openshift.io/sha-title": "Patching the stage catalog with rhoai-3.5-ea.2",
}


def _ctx(
    *,
    product: str = "rhoai",
    rhoai_version: str = "",
    external_secret: str = "",
    external: bool = False,
) -> TriggerContext:
    return TriggerContext(
        product=product,
        rhoai_version=rhoai_version,
        tests="smoke",
        install_dependencies=False,
        external_kubeconfig=external,
        external_secret=external_secret,
    )


class TriggerParamRegistryTests(unittest.TestCase):
    def test_cleanup_infer_rhoai_external(self) -> None:
        ctx = _ctx(product="rhoai", external=True)
        resolved = resolve_trigger_params(ctx, its_params={}, explicit={})
        self.assertEqual(resolved["CLEANUP"], "true")

    def test_cleanup_infer_ephc_harmless(self) -> None:
        ctx = _ctx(product="rhoai")
        resolved = resolve_trigger_params(ctx, its_params={}, explicit={})
        self.assertEqual(resolved["CLEANUP"], "true")

    def test_cleanup_existing_smoke_default_false(self) -> None:
        ctx = _ctx(product="", external=True)
        resolved = resolve_trigger_params(ctx, its_params={}, explicit={})
        self.assertEqual(resolved["CLEANUP"], "false")

    def test_cleanup_explicit_cli_wins(self) -> None:
        ctx = _ctx(product="rhoai", external=True)
        resolved = resolve_trigger_params(
            ctx,
            its_params={"CLEANUP": "true"},
            explicit={"CLEANUP": "false"},
        )
        self.assertEqual(resolved["CLEANUP"], "false")

    def test_cleanup_its_override_wins_over_infer(self) -> None:
        ctx = _ctx(product="rhoai", external=True)
        resolved = resolve_trigger_params(
            ctx,
            its_params={"CLEANUP": "false"},
            explicit={},
        )
        self.assertEqual(resolved["CLEANUP"], "false")

    def test_cleanup_infer_from_rhoai_version(self) -> None:
        ctx = _ctx(product="", rhoai_version="3.5-ea.2", external=True)
        resolved = resolve_trigger_params(ctx, its_params={}, explicit={})
        self.assertEqual(resolved["CLEANUP"], "false")

    def test_infer_cleanup_param_rhoai(self) -> None:
        from suite.trigger_param_registry import infer_cleanup_param

        self.assertEqual(infer_cleanup_param(product="rhoai"), "true")
        self.assertEqual(infer_cleanup_param(product=""), "false")

    def test_rhoai_version_from_fbc_snapshot_meta(self) -> None:
        ctx = TriggerContext(
            product="rhoai",
            rhoai_version="",
            tests="smoke",
            install_dependencies=False,
            external_kubeconfig=False,
            image="quay.io/rhoai/rhoai-fbc-fragment@sha256:deadbeef",
            resolved_app="rhoai-v3-5-ea-2",
            fbc_snapshot_meta={"labels": _LABELS_35, "annotations": _ANNOTATIONS_35},
        )
        resolved = resolve_trigger_params(ctx, its_params={}, explicit={})
        self.assertEqual(resolved["RHOAI_VERSION"], "3.5-ea.2")

    def test_cluster_source_infer_ephc(self) -> None:
        ctx = _ctx(product="rhoai")
        resolved = resolve_trigger_params(ctx, its_params={}, explicit={})
        self.assertEqual(resolved["CLUSTER_SOURCE"], CLUSTER_SOURCE_EPHC)

    def test_cluster_source_infer_external_secret(self) -> None:
        secret = "rhoai-e2e-kubeconfig-rh-nightly-pm"
        ctx = _ctx(product="rhoai", external=True, external_secret=secret)
        resolved = resolve_trigger_params(ctx, its_params={}, explicit={})
        self.assertEqual(resolved["CLUSTER_SOURCE"], secret)

    def test_cluster_source_its_wins(self) -> None:
        ctx = _ctx(
            product="rhoai",
            external=True,
            external_secret="rhoai-e2e-kubeconfig-rh-nightly-pm",
        )
        resolved = resolve_trigger_params(
            ctx,
            its_params={"CLUSTER_SOURCE": "custom-secret"},
            explicit={},
        )
        self.assertEqual(resolved["CLUSTER_SOURCE"], "custom-secret")

    def test_apply_trigger_param_resolution_from_args(self) -> None:
        import argparse

        args = argparse.Namespace(
            product="rhoai",
            version="",
            tests="smoke",
            install_dependencies=False,
            external_kubeconfig="",
            external_kubeconfig_secret="my-secret",
            external_kubeconfig_path=None,
            cleanup=None,
            cleanup_opt_out=False,
        )
        apply_trigger_param_resolution(args, its_manifest_path=None)
        self.assertTrue(args.cleanup)
        self.assertFalse(args.cleanup_opt_out)

    def test_apply_trigger_param_resolution_no_cleanup_flag(self) -> None:
        import argparse

        args = argparse.Namespace(
            product="",
            version="",
            tests="smoke",
            install_dependencies=False,
            external_kubeconfig="/tmp/kc",
            external_kubeconfig_secret="",
            external_kubeconfig_path="/tmp/kc",
            cleanup=False,
            cleanup_opt_out=True,
        )
        apply_trigger_param_resolution(args, its_manifest_path=None)
        self.assertFalse(args.cleanup)
        self.assertTrue(args.cleanup_opt_out)

    def test_components_from_committed_its_after_stage_clear(self) -> None:
        ctx = TriggerContext(
            product="rhoai",
            rhoai_version="",
            tests="bvt,smoke",
            install_dependencies=False,
            external_kubeconfig=False,
            committed_its_params={"COMPONENTS": "dashboard_cypress,platform"},
        )
        resolved = resolve_trigger_params(ctx, its_params={}, explicit={})
        self.assertEqual(resolved["COMPONENTS"], "dashboard_cypress,platform")
        _, patch_plan = resolve_trigger_patch_plan(ctx, its_params={}, explicit={})
        self.assertTrue(patch_plan["COMPONENTS"])

    def test_test_gates_from_committed_its_patches_non_default(self) -> None:
        ctx = TriggerContext(
            product="rhoai",
            rhoai_version="",
            tests="smoke",
            install_dependencies=False,
            external_kubeconfig=False,
            committed_its_params={"TEST_GATES": "smoke"},
        )
        _, patch_plan = resolve_trigger_patch_plan(ctx, its_params={}, explicit={})
        self.assertTrue(patch_plan["TEST_GATES"])

    def test_build_trigger_context_from_args(self) -> None:
        import argparse

        args = argparse.Namespace(
            product="rhoai",
            version="3.5",
            tests="smoke",
            install_dependencies=False,
            external_kubeconfig="",
            external_kubeconfig_secret="",
            external_kubeconfig_path=None,
        )
        ctx = build_trigger_context_from_args(args)
        self.assertEqual(ctx.product, "rhoai")
        self.assertEqual(ctx.external_secret, "")
        resolved = resolve_trigger_params(ctx, its_params={}, explicit={})
        self.assertEqual(resolved["CLUSTER_SOURCE"], CLUSTER_SOURCE_EPHC)


if __name__ == "__main__":
    unittest.main()
