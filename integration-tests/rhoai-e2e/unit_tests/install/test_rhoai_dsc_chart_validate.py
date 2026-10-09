"""Tests for operator-pinned chart DSC validation."""

from __future__ import annotations

import unittest
from unittest import mock

from install.rhoai_dsc_chart_validate import (
    _urlopen_timeout_sec,
    apply_component_policy_overrides,
    build_release_eligible_policy_from_chart,
    fetch_manifests_config,
    infer_operator_git_ref,
    install_removed_dsc_keys_from_policy_map,
    install_removed_keys_from_promotion_policy,
    parse_build_config_pin,
    parse_component_names_policy,
    resolve_pinned_chart_context,
    validate_dsc_keys_supported_by_chart,
    validate_smoke_managed_keys_for_operator_version,
)
from suite.errors import AppError


MANIFESTS_CONFIG = """
buildConfig:
  rhoai:
    ref: rhoai-3.5@abc123def
components:
  ogx:
    rhoai:
      repo: red-hat-data-services/ogx-k8s-operator
"""

CHART_VALUES = """
components:
  dashboard:
    dsc:
      managementState: Managed
  kserve:
    dsc:
      managementState: Managed
  kueue:
    dsc:
      managementState: Managed
  trainer:
    dsc:
      managementState: Managed
  aigateway:
    dsc:
      managementState: Managed
      modelsAsAService:
        managementState: Managed
      batchGateway:
        managementState: Removed
  sparkoperator:
    dsc:
      managementState: Removed
"""


class RhoaiDscChartValidateTest(unittest.TestCase):
    def test_urlopen_timeout_py39_uses_single_float(self) -> None:
        with mock.patch("install.rhoai_dsc_chart_validate.sys.version_info", (3, 9, 18)):
            self.assertEqual(_urlopen_timeout_sec(30, 120), 150.0)

    def test_infer_operator_git_ref(self) -> None:
        self.assertEqual(infer_operator_git_ref("3.5.1"), "rhoai-3.5")
        self.assertEqual(infer_operator_git_ref("v3.5.2"), "rhoai-3.5")
        self.assertEqual(infer_operator_git_ref(""), "main")

    def test_parse_build_config_pin(self) -> None:
        display, fetch = parse_build_config_pin(MANIFESTS_CONFIG)
        self.assertEqual(display, "rhoai-3.5@abc123def")
        self.assertEqual(fetch, "abc123def")

    def test_fetch_manifests_config_fallback_path(self) -> None:
        def fetch(url: str) -> str:
            if "/manifests-config.yaml" in url and "/build/" not in url:
                raise AppError("HTTP GET failed: 404. Not Found", 2)
            return MANIFESTS_CONFIG

        yaml_text, url = fetch_manifests_config("rhoai-3.5", fetch)
        self.assertIn("/build/manifests-config.yaml", url)
        self.assertIn("buildConfig", yaml_text)

    def test_build_release_eligible_policy_from_chart(self) -> None:
        import yaml

        doc = yaml.safe_load(CHART_VALUES)
        derived = build_release_eligible_policy_from_chart(doc)
        self.assertEqual(derived["dashboard"], "Managed")
        self.assertEqual(derived["kserve"], "Managed")
        self.assertEqual(derived["modelsasservice"], "Managed")
        self.assertEqual(derived["batchgateway"], "Removed")
        self.assertEqual(derived["sparkoperator"], "Removed")
        self.assertEqual(derived["trainer"], "Managed")

    def test_apply_component_policy_overrides_replaces_by_key(self) -> None:
        import yaml

        base = build_release_eligible_policy_from_chart(yaml.safe_load(CHART_VALUES))
        merged = apply_component_policy_overrides(base, "kueue:Unmanaged")
        self.assertEqual(merged["kueue"], "Unmanaged")
        self.assertEqual(merged["dashboard"], "Managed")

    def test_validate_chart_supports_keys(self) -> None:
        import yaml

        doc = yaml.safe_load(CHART_VALUES)
        validate_dsc_keys_supported_by_chart({"dashboard", "kserve", "trainer"}, doc)
        with self.assertRaises(AppError):
            validate_dsc_keys_supported_by_chart({"ogx"}, doc)

    def test_validate_chart_accepts_aipipelines_alias(self) -> None:
        import yaml

        doc = yaml.safe_load(
            """
components:
  datasciencepipelines:
    dsc: {}
"""
        )
        validate_dsc_keys_supported_by_chart({"aipipelines"}, doc)

    def test_validate_chart_accepts_codeflare_when_in_chart(self) -> None:
        import yaml

        doc = yaml.safe_load(
            """
components:
  codeflare:
    dsc: {}
"""
        )
        validate_dsc_keys_supported_by_chart({"codeflare"}, doc)

    def test_validate_chart_accepts_codeflare_via_ray_alias(self) -> None:
        import yaml

        doc = yaml.safe_load(
            """
components:
  ray:
    dsc: {}
"""
        )
        validate_dsc_keys_supported_by_chart({"codeflare"}, doc)

    def test_resolve_pinned_chart_context_with_mock_fetch(self) -> None:
        def fetch(url: str) -> str:
            if "manifests-config" in url or "build/manifests-config" in url:
                return MANIFESTS_CONFIG
            return CHART_VALUES

        ctx = resolve_pinned_chart_context("3.5.1", fetch_text_fn=fetch, enrich=False)
        self.assertEqual(ctx.operator_git_ref, "rhoai-3.5")
        self.assertEqual(ctx.build_config_fetch_ref, "abc123def")
        self.assertIn("/abc123def/", ctx.values_yaml_url)

    def test_validate_smoke_managed_keys_reports(self) -> None:
        def fetch(url: str) -> str:
            if "manifests-config" in url or "build/manifests-config" in url:
                return MANIFESTS_CONFIG
            return CHART_VALUES

        report = validate_smoke_managed_keys_for_operator_version(
            {"dashboard", "trainer"},
            "3.5.1",
            fetch_text_fn=fetch,
        )
        self.assertTrue(any(line.startswith("valuesYamlUrl=") for line in report))

    def test_parse_component_names_policy(self) -> None:
        policy = parse_component_names_policy("trainer:Managed,sparkoperator:Removed")
        self.assertEqual(policy["trainer"], "Managed")
        self.assertEqual(policy["sparkoperator"], "Removed")

    def test_install_removed_from_policy_map(self) -> None:
        removed = install_removed_dsc_keys_from_policy_map(
            {"trainer": "Managed", "sparkoperator": "Removed", "trainingoperator": "Removed"}
        )
        self.assertIn("sparkoperator", removed)
        self.assertIn("trainingoperator", removed)
        self.assertNotIn("trainer", removed)

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
