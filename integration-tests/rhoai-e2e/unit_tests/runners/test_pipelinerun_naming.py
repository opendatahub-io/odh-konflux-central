"""Unit tests for PipelineRun generateName prefix builder."""

from __future__ import annotations

import unittest

from suite.pipelinerun_naming import (
    build_diagnostic_artifact_log_name,
    build_rhoai_e2e_generate_prefix,
    cluster_segment_for_name,
    compact_version_for_name,
    diagnostic_version_segment,
    gates_segment_for_name,
    is_rhoai_e2e_pipelinerun_name,
)

class TestPipelinerunNaming(unittest.TestCase):
    def test_compact_version_short(self) -> None:
        self.assertEqual(compact_version_for_name("3.5"), "3.5")
        self.assertEqual(compact_version_for_name("rhoai-v3-5-ea-2"), "3.5ea2")
        self.assertEqual(compact_version_for_name("2.13.0-rc.2"), "2.13rc2")

    def test_compact_version_placeholder(self) -> None:
        self.assertEqual(compact_version_for_name("unspecified (default)"), "")
        self.assertEqual(compact_version_for_name("latest (default)"), "")
        self.assertEqual(compact_version_for_name("n/a"), "")

    def test_gates_segment(self) -> None:
        self.assertEqual(gates_segment_for_name("bvt,smoke"), "smoke")
        self.assertEqual(gates_segment_for_name("bvt"), "bvt")
        self.assertEqual(gates_segment_for_name("smoke"), "smoke")
        self.assertEqual(gates_segment_for_name("bvt,smoke,tier1"), "smoke-tier1")

    def test_single_component_after_gates(self) -> None:
        prefix = build_rhoai_e2e_generate_prefix(
            product="rhoai",
            cluster_source="EPHC",
            target_type="ephc",
            tests_csv="bvt,smoke",
            components_csv="maas_billing",
            run_owner="nmanos",
        )
        self.assertEqual(prefix, "e2e-cli-nmanos-ephc-rhoai-smoke-maas-billing-")

    def test_multiple_or_all_components_omitted(self) -> None:
        multi = build_rhoai_e2e_generate_prefix(
            product="rhoai",
            cluster_source="EPHC",
            target_type="ephc",
            tests_csv="bvt,smoke",
            components_csv="maas_billing,platform",
            run_owner="nmanos",
        )
        self.assertEqual(multi, "e2e-cli-nmanos-ephc-rhoai-smoke-")
        all_ids = build_rhoai_e2e_generate_prefix(
            product="rhoai",
            cluster_source="EPHC",
            target_type="ephc",
            tests_csv="bvt,smoke",
            components_csv="all",
            run_owner="nmanos",
        )
        self.assertEqual(all_ids, "e2e-cli-nmanos-ephc-rhoai-smoke-")

    def test_full_rhoai_ephc_fbc_catalog_line(self) -> None:
        prefix = build_rhoai_e2e_generate_prefix(
            product="rhoai",
            version="3.5-ea.2",
            cluster_source="EPHC",
            target_type="ephc",
            tests_csv="bvt,smoke",
            run_owner="nmanos@redhat.com",
        )
        self.assertEqual(prefix, "e2e-cli-nmanos-ephc-rhoai-3.5ea2-smoke-")

    def test_full_rhoai_ephc(self) -> None:
        prefix = build_rhoai_e2e_generate_prefix(
            product="rhoai",
            version="rhoai-v3-5-ea-2",
            cluster_source="EPHC",
            target_type="ephc",
            tests_csv="bvt,smoke",
            run_owner="nmanos@redhat.com",
        )
        self.assertEqual(prefix, "e2e-cli-nmanos-ephc-rhoai-3.5ea2-smoke-")

    def test_existing_omits_product(self) -> None:
        prefix = build_rhoai_e2e_generate_prefix(
            product="",
            tests_csv="bvt,smoke",
            run_owner="nmanos",
        )
        self.assertEqual(prefix, "e2e-cli-nmanos-smoke-")

    def test_rhoai_ephc_without_version(self) -> None:
        prefix = build_rhoai_e2e_generate_prefix(
            product="rhoai",
            version="n/a",
            cluster_source="EPHC",
            target_type="ephc",
            tests_csv="bvt,smoke",
            run_owner="jdoe",
        )
        self.assertEqual(prefix, "e2e-cli-jdoe-ephc-rhoai-smoke-")

    def test_external_cluster_label(self) -> None:
        prefix = build_rhoai_e2e_generate_prefix(
            product="rhoai",
            version="3.5",
            cluster_source="my-kubeconfig-secret",
            cluster_label="ods-qe-psi-09",
            target_type="external",
            tests_csv="bvt",
            run_owner="alice",
        )
        self.assertEqual(prefix, "e2e-cli-alice-ods-qe-psi-09-rhoai-3.5-bvt-")

    def test_existing_external_cluster_label(self) -> None:
        prefix = build_rhoai_e2e_generate_prefix(
            product="",
            cluster_source="rhoai-e2e-kubeconfig-nmanos",
            cluster_label="nmanos-konflux1",
            target_type="external",
            tests_csv="smoke",
            run_owner="nmanos",
        )
        self.assertEqual(prefix, "e2e-cli-nmanos-nmanos-konflux1-smoke-")

    def test_cluster_segment_skips_auto_kubeconfig_secret_name(self) -> None:
        self.assertEqual(
            cluster_segment_for_name(
                cluster_source="rhoai-e2e-kubeconfig-nmanos",
                cluster_label="",
                target_type="external",
            ),
            "",
        )
        self.assertEqual(
            cluster_segment_for_name(
                cluster_source="nmanos-konflux1",
                cluster_label="",
                target_type="external",
            ),
            "nmanos-konflux1",
        )

    def test_missing_version_and_cluster(self) -> None:
        prefix = build_rhoai_e2e_generate_prefix(
            product="rhoai",
            version="unspecified (default)",
            tests_csv="bvt",
            run_owner="bob",
        )
        self.assertEqual(prefix, "e2e-cli-bob-rhoai-bvt-")

    def test_rhoai_version_and_cluster_keeps_descriptive_name(self) -> None:
        prefix = build_rhoai_e2e_generate_prefix(
            product="rhoai",
            version="3.5",
            cluster_label="nmanos-konflux",
            cluster_source="rhoai-e2e-kubeconfig-nmanos-konflux-nmanos",
            target_type="external",
            tests_csv="bvt,smoke",
            run_owner="nmanos",
        )
        self.assertEqual(
            prefix,
            "e2e-cli-nmanos-nmanos-konflux-rhoai-3.5-smoke-",
        )

    def test_overflow_drops_version_before_cluster(self) -> None:
        prefix = build_rhoai_e2e_generate_prefix(
            product="rhoai",
            version="rhoai-v3-5-ea-2",
            cluster_label="rh-nightly-pm-staging-01",
            target_type="external",
            tests_csv="bvt,smoke,tier1,tier2,tier3",
            run_owner="nmanos",
        )
        self.assertIn("rh-nightly-pm-stagin", prefix)
        self.assertNotIn("3.5ea2", prefix)
        self.assertTrue(prefix.startswith("e2e-cli-nmanos-rh-nightly-pm-stagin-"))
        self.assertNotEqual(prefix, "e2e-cli-nmanos-smoke-tier1-tier2-tier3-")

    def test_is_rhoai_e2e_pipelinerun_name(self) -> None:
        self.assertTrue(is_rhoai_e2e_pipelinerun_name("e2e-cli-nmanos-bvt-smoke-abc"))
        self.assertTrue(is_rhoai_e2e_pipelinerun_name("rhoai-e2e-its-rh-nightly-pm-bvt-smoke-abc"))
        self.assertFalse(is_rhoai_e2e_pipelinerun_name("other-pipeline-abc"))
        self.assertFalse(is_rhoai_e2e_pipelinerun_name(""))

    def test_diagnostic_artifact_log_name_existing_cluster(self) -> None:
        name = build_diagnostic_artifact_log_name(
            since_time="2026-06-24T11:25:10Z",
            installed_product="rhoai",
            operator_version="2.4.1",
            cluster_label="ods-qe-psi-07",
            pipeline_product="",
        )
        self.assertEqual(name, "rhoai-2.4.1-ods-qe-psi-07-diagnostic-2026-06-24T112510Z.log")

    def test_diagnostic_artifact_log_name_trigger_version(self) -> None:
        name = build_diagnostic_artifact_log_name(
            since_time="2026-06-24T11:25:10Z",
            installed_product="rhoai",
            operator_version="rhoai-v3-5-ea-2",
            cluster_label="ods-qe-psi-07",
            pipeline_product="",
        )
        self.assertEqual(name, "rhoai-3.5ea2-ods-qe-psi-07-diagnostic-2026-06-24T112510Z.log")

    def test_diagnostic_artifact_log_name_uses_pipeline_product_when_installed_unknown(
        self,
    ) -> None:
        name = build_diagnostic_artifact_log_name(
            since_time="2026-06-24T11:25:10Z",
            installed_product="unknown",
            operator_version="",
            cluster_label="",
            pipeline_product="rhoai",
        )
        self.assertEqual(name, "rhoai-diagnostic-2026-06-24T112510Z.log")

    def test_diagnostic_version_segment_semver(self) -> None:
        self.assertEqual(diagnostic_version_segment("2.4.1"), "2.4.1")
        self.assertEqual(diagnostic_version_segment("rhoai-v3-5-ea-2"), "3.5ea2")

