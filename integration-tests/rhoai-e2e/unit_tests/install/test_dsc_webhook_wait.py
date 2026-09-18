#!/usr/bin/env python3
"""Unit tests for operator admission webhook wait during DSC setup."""

from __future__ import annotations

import unittest
from unittest import mock

from install import dsc_install

class DscWebhookWaitTest(unittest.TestCase):
    def test_apply_cr_retries_on_webhook_no_endpoints(self) -> None:
        responses = [
            type("R", (), {"returncode": 1, "stdout": "", "stderr": "no endpoints available for service"})(),
            type("R", (), {"returncode": 0, "stdout": "", "stderr": ""})(),
        ]
        with (
            mock.patch.object(dsc_install, "oc_run", side_effect=responses) as oc_mock,
            mock.patch.object(dsc_install.time, "sleep"),
        ):
            dsc_install._apply_cr("dscinitialization", "default-dsci", "apiVersion: v1\n", timeout_sec=60)
        self.assertEqual(oc_mock.call_count, 2)

    def test_wait_operator_admission_webhook_succeeds_when_endpoints_exist(self) -> None:
        with (
            mock.patch.dict("os.environ", {"OPERATOR_NAMESPACE": "redhat-ods-operator"}, clear=False),
            mock.patch.object(
                dsc_install,
                "oc_run",
                return_value=type("R", (), {"returncode": 0, "stdout": "10.0.0.1", "stderr": ""})(),
            ),
        ):
            dsc_install.wait_operator_admission_webhook(timeout_sec=30)

    def test_patch_dsc_merge_retries_on_webhook_no_endpoints(self) -> None:
        responses = [
            type(
                "R",
                (),
                {
                    "returncode": 1,
                    "stdout": "",
                    "stderr": "failed calling webhook: no endpoints available for service",
                },
            )(),
            type("R", (), {"returncode": 0, "stdout": "", "stderr": ""})(),
        ]
        with (
            mock.patch.object(dsc_install, "oc_run", side_effect=responses) as oc_mock,
            mock.patch.object(dsc_install.time, "sleep"),
        ):
            dsc_install._patch_dsc_merge_with_webhook_retry(
                '{"spec":{"components":{"aipipelines":{"managementState":"Managed"}}}}',
                label="aipipelines=Managed",
                timeout_sec=60,
            )
        self.assertEqual(oc_mock.call_count, 2)

