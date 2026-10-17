"""Tests for operator-sample DSC policy validation."""

from __future__ import annotations

import unittest
from unittest import mock

from install.rhoai_dsc_chart_validate import (
    OPERATOR_DSC_SAMPLE_V2_PATH,
    OPERATOR_DSC_SAMPLE_V3_PATH,
    _urlopen_timeout_sec,
    apply_component_policy_overrides,
    build_policy_from_operator_sample,
    candidate_sample_paths_for_operator_ref,
    discover_operator_sample,
    infer_operator_git_ref,
    install_removed_dsc_keys_from_policy_map,
    install_removed_keys_from_promotion_policy,
    parse_component_names_policy,
    resolve_operator_dsc_policy_for_version,
    validate_dsc_keys_supported_by_sample_policy,
    validate_smoke_managed_keys_for_operator_version,
)
from suite.errors import AppError

OPERATOR_SAMPLE_V2 = """
apiVersion: datasciencecluster.opendatahub.io/v2
kind: DataScienceCluster
spec:
  components:
    aigateway:
      managementState: "Removed"
      batchGateway:
        managementState: "Removed"
    dashboard:
      managementState: "Managed"
    kserve:
      managementState: "Managed"
      modelsAsService:
        managementState: "Removed"
    kueue:
      managementState: "Removed"
    trainingoperator:
      managementState: "Removed"
    ogx:
      managementState: "Managed"
    sparkoperator:
      managementState: "Removed"
"""

OPERATOR_SAMPLE_V3 = """
apiVersion: datasciencecluster.opendatahub.io/v3
kind: DataScienceCluster
spec:
  components:
    dashboard:
      standard:
        managementState: "Managed"
    aigateway:
      managementState: "Removed"
      modelsAsAService:
        managementState: "Removed"
    aiHub:
      managementState: "Managed"
    data:
      featureStore:
        managementState: "Managed"
    kueue:
      managementState: "Removed"
"""


class RhoaiDscChartValidateTest(unittest.TestCase):
    def test_urlopen_timeout_uses_single_float(self) -> None:
        self.assertEqual(_urlopen_timeout_sec(30, 120), 150.0)

    def test_infer_operator_git_ref(self) -> None:
        self.assertEqual(infer_operator_git_ref("3.5.1"), "rhoai-3.5")
        self.assertEqual(infer_operator_git_ref("v3.5.2"), "rhoai-3.5")
        self.assertEqual(infer_operator_git_ref(""), "main")

    def test_candidate_sample_paths_for_operator_ref(self) -> None:
        self.assertEqual(
            candidate_sample_paths_for_operator_ref("rhoai-3.5"),
            (OPERATOR_DSC_SAMPLE_V2_PATH, OPERATOR_DSC_SAMPLE_V3_PATH),
        )
        self.assertEqual(
            candidate_sample_paths_for_operator_ref("rhoai-3.6"),
            (OPERATOR_DSC_SAMPLE_V3_PATH, OPERATOR_DSC_SAMPLE_V2_PATH),
        )
        self.assertEqual(
            candidate_sample_paths_for_operator_ref("rhoai-3.6-ea.1"),
            (OPERATOR_DSC_SAMPLE_V2_PATH, OPERATOR_DSC_SAMPLE_V3_PATH),
        )

    def test_discover_operator_sample_probes_v3_first_on_36(self) -> None:
        def fetch(url: str) -> str:
            if "datasciencecluster_v3" in url:
                return OPERATOR_SAMPLE_V3
            raise AppError("HTTP GET failed: 404. Not Found", 2)

        ctx = discover_operator_sample("rhoai-3.6", "", fetch)
        self.assertIn("datasciencecluster_v3", ctx.dsc_sample_path)
        self.assertTrue(ctx.sample_discovery.startswith("probed="))

    def test_discover_operator_sample_falls_back_to_v2_on_ea(self) -> None:
        def fetch(url: str) -> str:
            if "datasciencecluster_v2" in url:
                return OPERATOR_SAMPLE_V2
            raise AppError("HTTP GET failed: 404. Not Found", 2)

        ctx = discover_operator_sample("rhoai-3.6-ea.1", "", fetch)
        self.assertIn("datasciencecluster_v2", ctx.dsc_sample_path)

    def test_build_policy_from_operator_sample_v2(self) -> None:
        import yaml

        doc = yaml.safe_load(OPERATOR_SAMPLE_V2)
        derived = build_policy_from_operator_sample(doc)
        self.assertEqual(derived["dashboard"], "Managed")
        self.assertEqual(derived["aigateway"], "Removed")
        self.assertEqual(derived["batchgateway"], "Removed")
        self.assertEqual(derived["modelsasservice"], "Removed")
        self.assertEqual(derived["trainingoperator"], "Removed")
        self.assertEqual(derived["kueue"], "Removed")
        self.assertEqual(derived["ogx"], "Managed")
        self.assertEqual(derived["sparkoperator"], "Removed")
        self.assertEqual(derived["mcplifecycleoperator"], "Removed")

    def test_build_policy_from_operator_sample_v3(self) -> None:
        import yaml

        doc = yaml.safe_load(OPERATOR_SAMPLE_V3)
        derived = build_policy_from_operator_sample(doc)
        self.assertEqual(derived["dashboard"], "Managed")
        self.assertEqual(derived["modelregistry"], "Managed")
        self.assertEqual(derived["feastoperator"], "Managed")
        self.assertEqual(derived["trainingoperator"], "Removed")

    def test_apply_component_policy_overrides(self) -> None:
        import yaml

        base = build_policy_from_operator_sample(yaml.safe_load(OPERATOR_SAMPLE_V2))
        merged = apply_component_policy_overrides(base, "kueue:Unmanaged")
        self.assertEqual(merged["kueue"], "Unmanaged")
        self.assertEqual(merged["dashboard"], "Managed")

    def test_validate_sample_policy_blocks_removed_keys(self) -> None:
        import yaml

        doc = yaml.safe_load(OPERATOR_SAMPLE_V2)
        policy = build_policy_from_operator_sample(doc)
        validate_dsc_keys_supported_by_sample_policy({"dashboard", "ogx"}, policy, doc)
        with self.assertRaises(AppError):
            validate_dsc_keys_supported_by_sample_policy({"sparkoperator"}, policy, doc)

    def test_validate_sample_policy_allows_extra_spec_key_in_sample(self) -> None:
        import yaml

        doc = yaml.safe_load(
            OPERATOR_SAMPLE_V2
            + """
    codeflare:
      managementState: "Managed"
"""
        )
        policy = build_policy_from_operator_sample(doc)
        validate_dsc_keys_supported_by_sample_policy({"codeflare"}, policy, doc)

    def test_resolve_operator_dsc_policy_with_override(self) -> None:
        import yaml

        doc = yaml.safe_load(OPERATOR_SAMPLE_V2)
        policy = resolve_operator_dsc_policy_for_version(
            "3.5.2",
            components_override="kueue:Unmanaged",
            operator_git_ref="rhoai-3.5",
            explicit_sample_path=OPERATOR_DSC_SAMPLE_V2_PATH,
            fetch_text_fn=lambda _url: OPERATOR_SAMPLE_V2,
        )
        self.assertEqual(policy["kueue"], "Unmanaged")
        self.assertEqual(policy["trainingoperator"], "Removed")

    def test_validate_smoke_managed_keys_reports(self) -> None:
        def fetch(url: str) -> str:
            if "rhods-operator" in url and "samples" in url:
                return OPERATOR_SAMPLE_V2
            raise AppError("HTTP GET failed: 404", 2)

        report = validate_smoke_managed_keys_for_operator_version(
            {"dashboard", "ogx"},
            "3.5.1",
            fetch_text_fn=fetch,
        )
        self.assertTrue(any(line.startswith("dscSampleUrl=") for line in report))

    def test_install_removed_from_policy_map(self) -> None:
        removed = install_removed_dsc_keys_from_policy_map(
            {"trainer": "Managed", "sparkoperator": "Removed", "trainingoperator": "Removed"}
        )
        self.assertIn("sparkoperator", removed)
        self.assertIn("trainingoperator", removed)

    def test_install_removed_from_promotion_policy_string(self) -> None:
        removed = install_removed_keys_from_promotion_policy(
            "trainer:Managed,sparkoperator:Removed,trainingoperator:Removed"
        )
        self.assertIn("sparkoperator", removed)

    @mock.patch("install.dsc_install_policy._maybe_validate_managed_keys_against_chart")
    def test_resolve_managed_dsc_keys_skips_validate_without_version(self, _validate: object) -> None:
        from install.dsc_install_policy import resolve_managed_dsc_keys

        keys = resolve_managed_dsc_keys("trainer", "", for_install=False)
        self.assertIn("trainer", keys)


if __name__ == "__main__":
    unittest.main()
