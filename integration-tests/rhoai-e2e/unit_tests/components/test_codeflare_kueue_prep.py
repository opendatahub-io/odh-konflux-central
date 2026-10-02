#!/usr/bin/env python3
"""Unit tests for codeflare_sdk Kueue cluster prep."""

from __future__ import annotations

import json
import unittest
from unittest import mock

from components.codeflare_sdk.kueue_prep import (
    _clear_stuck_openshift_kueue_cluster,
    _kueue_dsc_management_state,
    ensure_codeflare_kueue_ready,
)

class CodeflareKueuePrepTest(unittest.TestCase):
    def test_kueue_state_unmanaged_on_35(self) -> None:
        self.assertEqual(_kueue_dsc_management_state("3.5.0-ea.2"), "Unmanaged")

    def test_kueue_state_managed_pre_35(self) -> None:
        self.assertEqual(_kueue_dsc_management_state("3.4.0"), "Managed")

    def test_clear_stuck_kueue_patches_finalizers(self) -> None:
        stuck = {
            "metadata": {
                "name": "cluster",
                "deletionTimestamp": "2026-06-28T19:55:17Z",
            }
        }
        with mock.patch(
            "components.codeflare_sdk.kueue_prep.oc_run",
            side_effect=[
                mock.Mock(returncode=0, stdout=json.dumps(stuck)),
                mock.Mock(returncode=0, stdout="", stderr=""),
            ],
        ) as oc_run:
            _clear_stuck_openshift_kueue_cluster()
        patch_call = oc_run.call_args_list[-1].args[0]
        self.assertIn("kueue.kueue.openshift.io/cluster", patch_call)

    def test_kueue_state_unmanaged_when_version_unknown(self) -> None:
        with mock.patch(
            "components.codeflare_sdk.kueue_prep.probe_operator_version_from_cluster",
            return_value="",
        ):
            self.assertEqual(_kueue_dsc_management_state(), "Unmanaged")

    def test_ensure_codeflare_kueue_ready_skips_when_smoke_ready(self) -> None:
        with mock.patch(
            "components.codeflare_sdk.kueue_prep._kueue_smoke_ready",
            return_value=True,
        ):
            with mock.patch(
                "components.codeflare_sdk.kueue_prep.ensure_dsc_component_management_state"
            ) as patch_state:
                ensure_codeflare_kueue_ready()
        patch_state.assert_not_called()

    def test_ensure_codeflare_kueue_ready_waits_for_api_and_webhook(self) -> None:
        with mock.patch(
            "components.codeflare_sdk.kueue_prep._kueue_dsc_management_state",
            return_value="Unmanaged",
        ):
            with mock.patch(
                "components.codeflare_sdk.kueue_prep.ensure_dsc_component_management_state"
            ) as patch_state:
                with mock.patch(
                    "components.codeflare_sdk.kueue_prep._clear_stuck_openshift_kueue_cluster"
                ):
                    with mock.patch(
                        "components.codeflare_sdk.kueue_prep._kueue_smoke_ready",
                        side_effect=[False, True],
                    ):
                        with mock.patch(
                            "components.codeflare_sdk.kueue_prep._kueue_api_available",
                            return_value=True,
                        ):
                            with mock.patch(
                                "components.codeflare_sdk.kueue_prep._kueue_webhook_ready",
                                return_value=False,
                            ):
                                with mock.patch(
                                    "components.codeflare_sdk.kueue_prep._dsc_condition",
                                    return_value=("False", "Progressing", ""),
                                ):
                                    with mock.patch(
                                        "components.codeflare_sdk.kueue_prep.time.sleep"
                                    ):
                                        ensure_codeflare_kueue_ready(timeout_sec=30)
        patch_state.assert_called_once_with("kueue", "Unmanaged")

if __name__ == "__main__":
    raise SystemExit(unittest.main())
