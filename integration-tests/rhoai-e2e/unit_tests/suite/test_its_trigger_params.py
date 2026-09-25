"""Tests for ITS / PipelineRun trigger param helpers."""

from __future__ import annotations

import os
import unittest
from unittest import mock

from suite.its_trigger_params import (CLUSTER_SOURCE_EPHC,
                                      external_kubeconfig_secret_name,
                                      is_ephemeral_hosted_cluster_source,
                                      is_external_cluster_source,
                                      is_known_shared_external_cluster_secret,
                                      is_pooled_external_cluster_source,
                                      ocp_install_prefix,
                                      ocp_version_from_rhoai_fbc_name,
                                      resolve_cluster_source_for_trigger,
                                      resolve_ocp_version_display,
                                      resolve_rhoai_fbc_image,
                                      resolve_rhoai_version_display,
                                      resolve_version_display_params,
                                      rhoai_version_from_app,
                                      rhoai_version_label_from_app,
                                      validate_cluster_source,
                                      with_default_suffix)


class ItsTriggerParamsTests(unittest.TestCase):
    def test_external_secret_name(self) -> None:
        self.assertTrue(is_external_cluster_source("rhoai-e2e-kubeconfig-test"))
        self.assertEqual(
            external_kubeconfig_secret_name("rhoai-e2e-kubeconfig-test"),
            "rhoai-e2e-kubeconfig-test",
        )

    def test_ephc_not_external(self) -> None:
        self.assertFalse(is_external_cluster_source(CLUSTER_SOURCE_EPHC))
        self.assertEqual(external_kubeconfig_secret_name(CLUSTER_SOURCE_EPHC), "")

    def test_ephemeral_hosted_cluster_source(self) -> None:
        self.assertTrue(is_ephemeral_hosted_cluster_source(CLUSTER_SOURCE_EPHC))
        self.assertFalse(is_ephemeral_hosted_cluster_source(""))
        self.assertFalse(is_ephemeral_hosted_cluster_source("rhoai-e2e-kubeconfig-rh-nightly-pm"))

    def test_known_shared_external_cluster_secret(self) -> None:
        self.assertTrue(is_known_shared_external_cluster_secret("rhoai-e2e-kubeconfig-rh-nightly-pm"))
        self.assertTrue(is_known_shared_external_cluster_secret("rhoai-e2e-kubeconfig-ods-qe-psi-23"))
        self.assertFalse(
            is_known_shared_external_cluster_secret("kubeconfig-rh-nightly-pm-user")
        )
        self.assertFalse(is_known_shared_external_cluster_secret("rhoai-e2e-kubeconfig-nmanos-konflux1-nmanos"))
        self.assertFalse(is_known_shared_external_cluster_secret(CLUSTER_SOURCE_EPHC))
        self.assertFalse(is_known_shared_external_cluster_secret(""))

    def test_pooled_external_cluster_source(self) -> None:
        # Requires both RUN_COMPONENT_CLUSTER_PREP_IN_DEP_OPERATORS and CLUSTER_POOL_MARKER.
        env = {
            "RUN_COMPONENT_CLUSTER_PREP_IN_DEP_OPERATORS": "true",
            "CLUSTER_POOL_MARKER": "true",
        }
        with mock.patch.dict(os.environ, env, clear=False):
            # Any external secret is pooled when both flags are set.
            self.assertTrue(is_pooled_external_cluster_source("rhoai-e2e-kubeconfig-rh-nightly-pm"))
            self.assertTrue(is_pooled_external_cluster_source("rhoai-e2e-kubeconfig-ods-qe-psi-23"))
            self.assertTrue(is_pooled_external_cluster_source("custom-user-secret"))
        # Missing RUN_COMPONENT_CLUSTER_PREP_IN_DEP_OPERATORS.
        env_no_prep = {"CLUSTER_POOL_MARKER": "true"}
        with mock.patch.dict(os.environ, env_no_prep, clear=False):
            self.assertFalse(is_pooled_external_cluster_source("rhoai-e2e-kubeconfig-rh-nightly-pm"))
        # Missing CLUSTER_POOL_MARKER.
        env_no_marker = {"RUN_COMPONENT_CLUSTER_PREP_IN_DEP_OPERATORS": "true"}
        with mock.patch.dict(os.environ, env_no_marker, clear=False):
            self.assertFalse(is_pooled_external_cluster_source("rhoai-e2e-kubeconfig-rh-nightly-pm"))
        # EPHC and empty are never pooled.
        self.assertFalse(is_pooled_external_cluster_source(CLUSTER_SOURCE_EPHC))
        self.assertFalse(is_pooled_external_cluster_source(""))

    def test_resolve_cluster_source_for_trigger(self) -> None:
        self.assertEqual(
            resolve_cluster_source_for_trigger(product="rhoai", external_secret=""),
            CLUSTER_SOURCE_EPHC,
        )
        self.assertEqual(
            resolve_cluster_source_for_trigger(product="", external_secret=""),
            "",
        )
        self.assertEqual(
            resolve_cluster_source_for_trigger(
                product="rhoai",
                external_secret="rhoai-e2e-kubeconfig-foo",
            ),
            "rhoai-e2e-kubeconfig-foo",
        )

    def test_validate_rejects_invalid_secret_name(self) -> None:
        with self.assertRaises(ValueError):
            validate_cluster_source("Not_A_Secret")

    def test_with_default_suffix(self) -> None:
        self.assertEqual(with_default_suffix("3.5", explicit=True), "3.5")
        self.assertEqual(with_default_suffix("3.5", explicit=False), "3.5 (default)")

    def test_rhoai_version_from_app(self) -> None:
        self.assertEqual(rhoai_version_from_app("rhoai-v3-5-ea-1"), "3.5")

    def test_rhoai_version_label_from_app(self) -> None:
        self.assertEqual(rhoai_version_label_from_app("rhoai-v3-5-ea-1"), "rhoai-v3-5-ea-1")
        self.assertEqual(rhoai_version_label_from_app("rhoai-v3-5-ea-2"), "rhoai-v3-5-ea-2")
        self.assertEqual(rhoai_version_label_from_app("rhoai-v3-5"), "rhoai-v3-5")
        self.assertEqual(rhoai_version_label_from_app(""), "")

    def test_rhoai_version_explicit(self) -> None:
        self.assertEqual(
            resolve_rhoai_version_display(
                product="rhoai",
                cli_version="3.5",
                resolved_app="",
                update_channel="beta",
                explicit_cli=True,
            ),
            "3.5",
        )
        self.assertEqual(
            resolve_rhoai_version_display(
                product="rhoai",
                cli_version="3.5",
                resolved_app="rhoai-v3-5-ea-1",
                update_channel="beta",
                explicit_cli=True,
            ),
            "rhoai-v3-5-ea-1",
        )

    def test_rhoai_version_default_from_app(self) -> None:
        self.assertEqual(
            resolve_rhoai_version_display(
                product="rhoai",
                cli_version="",
                resolved_app="rhoai-v3-5-ea-1",
                update_channel="beta",
                explicit_cli=False,
            ),
            "rhoai-v3-5-ea-1 (default)",
        )

    def test_rhoai_version_existing(self) -> None:
        self.assertEqual(
            resolve_rhoai_version_display(
                product="",
                cli_version="",
                resolved_app="",
                update_channel="beta",
                explicit_cli=False,
            ),
            "n/a",
        )

    def test_ocp_version_ephc_default(self) -> None:
        self.assertEqual(
            resolve_ocp_version_display(
                product="rhoai",
                cluster_source=CLUSTER_SOURCE_EPHC,
                cli_ocp="",
                explicit_cli=False,
            ),
            "latest (default)",
        )

    def test_ocp_install_prefix_ignores_display_placeholders(self) -> None:
        self.assertEqual(ocp_install_prefix("latest (default)"), "")
        self.assertEqual(ocp_install_prefix("unspecified (default)"), "")
        self.assertEqual(ocp_install_prefix("4.21"), "4.21")
        self.assertEqual(ocp_install_prefix(" 4.20 "), "4.20")

    def test_ocp_version_explicit(self) -> None:
        self.assertEqual(
            resolve_ocp_version_display(
                product="rhoai",
                cluster_source=CLUSTER_SOURCE_EPHC,
                cli_ocp="4.19",
                explicit_cli=True,
            ),
            "4.19",
        )

    def test_ocp_version_from_rhoai_fbc_name(self) -> None:
        self.assertEqual(
            ocp_version_from_rhoai_fbc_name("rhoai-fbc-fragment-ocp-421"),
            "4.21",
        )
        self.assertEqual(ocp_version_from_rhoai_fbc_name("odh-operator-catalog"), "")

    def test_ocp_version_external(self) -> None:
        self.assertEqual(
            resolve_ocp_version_display(
                product="",
                cluster_source="rhoai-e2e-kubeconfig-test",
                cli_ocp="",
                explicit_cli=False,
            ),
            "n/a",
        )
        self.assertEqual(
            resolve_ocp_version_display(
                product="rhoai",
                cluster_source="rhoai-e2e-kubeconfig-ods-qe-psi-23",
                cli_ocp="",
                explicit_cli=False,
                rhoai_fbc_name="rhoai-fbc-fragment-ocp-421",
            ),
            "4.21 (default)",
        )

    def test_rhoai_fbc_image(self) -> None:
        pullspec = "quay.io/rhoai/rhoai-fbc-fragment@sha256:ab0042e79c995ace875bf5624c6a7e98fe082c833b39bbc0ea9b0c16399496a9"
        self.assertEqual(
            resolve_rhoai_fbc_image(fbc_image=pullspec, explicit_cli=False),
            f"{pullspec} (default)",
        )
        self.assertEqual(resolve_rhoai_fbc_image(fbc_image=pullspec, explicit_cli=True), pullspec)
        self.assertEqual(
            resolve_rhoai_fbc_image(fbc_image="", explicit_cli=False),
            "unspecified (default)",
        )

    def test_resolve_version_display_params(self) -> None:
        display = resolve_version_display_params(
            product="rhoai",
            cli_version="3.5",
            resolved_app="rhoai-v3-5-ea-2",
            update_channel="beta",
            cluster_source=CLUSTER_SOURCE_EPHC,
            cli_ocp="4.19",
            ocp_explicit=True,
            fbc_image="quay.io/rhoai/rhoai-fbc-fragment@sha256:deadbeef",
            fbc_image_explicit=True,
        )
        self.assertEqual(display["RHOAI_VERSION"], "rhoai-v3-5-ea-2")
        self.assertEqual(display["OCP_VERSION"], "4.19")
        self.assertEqual(display["RHOAI_FBC_IMAGE"], "quay.io/rhoai/rhoai-fbc-fragment@sha256:deadbeef")

