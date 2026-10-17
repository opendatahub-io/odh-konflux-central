"""Regression tests for EPHC OLM bundle-unpack diagnostic mode (IT review @ 933ba999)."""

from __future__ import annotations

import json
import unittest
from unittest.mock import patch

from install import install_and_verify as iav


class OlmBundleUnpackDiagnosticTests(unittest.TestCase):
    def test_count_jobs_returns_none_on_api_error(self) -> None:
        with patch.object(
            iav,
            "oc_run",
            return_value=type("R", (), {"returncode": 1, "stdout": "", "stderr": "forbidden"})(),
        ):
            self.assertIsNone(iav.count_olm_bundle_unpack_jobs(include_active=True))

    def test_diagnostic_only_from_env(self) -> None:
        with patch.dict("os.environ", {"OLM_BUNDLE_UNPACK_DIAGNOSTIC_ONLY": "1"}, clear=False):
            self.assertTrue(iav.olm_bundle_unpack_diagnostic_only())
        with patch.dict("os.environ", {"OLM_BUNDLE_UNPACK_DIAGNOSTIC_ONLY": "0"}, clear=False):
            self.assertFalse(iav.olm_bundle_unpack_diagnostic_only())

    def test_zero_no_job_kicks_does_not_instant_fail_in_wait(self) -> None:
        """max_no_job_kicks=0 must observe until deadline, not fail on first zero-count poll."""
        with patch.dict(
            "os.environ",
            {
                "OLM_BUNDLE_UNPACK_DIAGNOSTIC_ONLY": "1",
                "OLM_BUNDLE_UNPACK_NO_JOB_KICKS": "0",
                "OLM_BUNDLE_UNPACK_STALL_RECOVERIES": "0",
            },
            clear=False,
        ):
            with patch.object(iav, "ensure_operatorgroup_bundle_unpack_annotations"):
                with patch.object(iav, "subscription_bundle_unpack_in_progress", return_value=True):
                    with patch.object(
                        iav,
                        "count_olm_bundle_unpack_jobs",
                        return_value=0,
                    ):
                        with patch.object(iav, "_subscription_status_last_updated", return_value="t0"):
                            with patch.object(iav, "time") as mock_time:
                                mock_time.time.side_effect = [0.0, 1.0, 2.0, 999999.0]
                                with patch.object(iav, "log_marketplace_bundle_unpack_state"):
                                    result = iav.wait_subscription_bundle_unpacked(
                                        "rhods-operator",
                                        "redhat-ods-operator",
                                        1.0,
                                        marketplace_namespace="openshift-marketplace",
                                    )
        self.assertFalse(result)


if __name__ == "__main__":
    unittest.main()
