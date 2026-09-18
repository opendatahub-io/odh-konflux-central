"""Tests for tenant shift-left smoke secret maintenance."""

from __future__ import annotations

import base64
import json
import unittest
from unittest import mock

from k8s.smoke_aws_credentials import (
    MLFLOW_ENVFILE_SECRET,
    backfill_shift_left_smoke_secret_from_mlflow,
)


def _b64(value: str) -> str:
    return base64.b64encode(value.encode("utf-8")).decode("ascii")


class BackfillShiftLeftSmokeSecretTest(unittest.TestCase):
    def test_backfills_empty_aws_and_ci_s3_from_mlflow(self) -> None:
        shiftleft = {
            "AWS_ACCESS_KEY_ID": _b64(""),
            "AWS_SECRET_ACCESS_KEY": _b64(""),
            "CI_S3_BUCKET_NAME": _b64(""),
            "AWS_CA_BUNDLE": _b64("pem"),
        }
        mlflow = {
            "AWS_ACCESS_KEY_ID": _b64("AKIA_TEST"),
            "AWS_SECRET_ACCESS_KEY": _b64("secret"),
            "AWS_DEFAULT_REGION": _b64("us-east-1"),
            "BUCKET": _b64("ods-ci-s3"),
            "ENDPOINT": _b64("https://s3.us-east-1.amazonaws.com"),
        }

        def fake_run_cmd(argv, *, capture=False, check=False):
            _ = capture, check
            if argv[:4] == ["oc", "get", "secret", "shiftleft-envfile-model-serving"]:
                return mock.Mock(returncode=0, stdout=json.dumps({"data": shiftleft}))
            if argv[:4] == ["oc", "get", "secret", MLFLOW_ENVFILE_SECRET]:
                return mock.Mock(returncode=0, stdout=json.dumps({"data": mlflow}))
            if argv[:3] == ["oc", "patch", "secret"]:
                self.patch_payload = json.loads(argv[8])
                return mock.Mock(returncode=0, stdout="", stderr="")
            raise AssertionError(f"unexpected oc argv: {argv}")

        self.patch_payload = None
        with mock.patch("k8s.smoke_aws_credentials.run_cmd", side_effect=fake_run_cmd):
            changed = backfill_shift_left_smoke_secret_from_mlflow(
                tenant_namespace="rhoai-tenant",
                secret_name="shiftleft-envfile-model-serving",
            )
        self.assertTrue(changed)
        assert self.patch_payload is not None
        data = self.patch_payload["data"]
        self.assertEqual(base64.b64decode(data["AWS_ACCESS_KEY_ID"]).decode(), "AKIA_TEST")
        self.assertEqual(base64.b64decode(data["CI_S3_BUCKET_NAME"]).decode(), "ods-ci-s3")
        self.assertEqual(
            base64.b64decode(data["MODELS_S3_BUCKET_ENDPOINT"]).decode(),
            "https://s3.us-east-1.amazonaws.com",
        )

    def test_noop_when_shiftleft_already_has_aws(self) -> None:
        shiftleft = {"AWS_ACCESS_KEY_ID": _b64("AKIA_EXISTING")}
        mlflow = {"AWS_ACCESS_KEY_ID": _b64("AKIA_OTHER")}

        def fake_run_cmd(argv, *, capture=False, check=False):
            _ = capture, check
            if argv[3] == "shiftleft-envfile-model-serving":
                return mock.Mock(returncode=0, stdout=json.dumps({"data": shiftleft}))
            if argv[3] == MLFLOW_ENVFILE_SECRET:
                return mock.Mock(returncode=0, stdout=json.dumps({"data": mlflow}))
            raise AssertionError(argv)

        with mock.patch("k8s.smoke_aws_credentials.run_cmd", side_effect=fake_run_cmd):
            self.assertFalse(
                backfill_shift_left_smoke_secret_from_mlflow(
                    tenant_namespace="rhoai-tenant",
                    secret_name="shiftleft-envfile-model-serving",
                )
            )


if __name__ == "__main__":
    raise SystemExit(unittest.main())
