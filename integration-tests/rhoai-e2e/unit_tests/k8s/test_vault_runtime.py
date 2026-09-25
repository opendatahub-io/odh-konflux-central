"""Unit tests for runtime Vault shift-left staging (no live Vault)."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from k8s.vault_runtime import (
    VAULT_APPROLE_SECRET,
    load_hcp_install_aws_credentials,
    merge_model_serving_env,
    parse_env_file_blob,
    shift_left_vault_blob_key,
    stage_shift_left_files,
    vault_login_and_read_shift_left,
)


class ParseEnvFileBlobTest(unittest.TestCase):
    def test_parses_key_value_lines_and_skips_comments(self) -> None:
        blob = (
            "# header\n"
            "CI_S3_BUCKET_NAME=ods-ci-s3\n"
            "AWS_ACCESS_KEY_ID=AKIAEXAMPLE\n"
            "\n"
            "export MODELS_S3_BUCKET_NAME=ods-ci-wisdom\n"
        )
        parsed = parse_env_file_blob(blob)
        self.assertEqual(parsed["CI_S3_BUCKET_NAME"], "ods-ci-s3")
        self.assertEqual(parsed["AWS_ACCESS_KEY_ID"], "AKIAEXAMPLE")
        self.assertEqual(parsed["MODELS_S3_BUCKET_NAME"], "ods-ci-wisdom")
        self.assertNotIn("# header", parsed)


class ShiftLeftVaultBlobKeyTest(unittest.TestCase):
    def test_maps_cloned_tenant_secret_names(self) -> None:
        self.assertEqual(shift_left_vault_blob_key("envfile-mlflow"), "envFileMlflow")
        self.assertEqual(shift_left_vault_blob_key("envfile-ogx"), "envFileOGX")
        self.assertEqual(shift_left_vault_blob_key("envfile-pipelines"), "envFilePipelines")
        self.assertEqual(
            shift_left_vault_blob_key("envfile-codeflare-sdk"), "envFileCodeflareSdk"
        )
        self.assertEqual(
            shift_left_vault_blob_key("envfile-dashboard-cypress"),
            "volumeFileTestVariables",
        )
        self.assertEqual(
            shift_left_vault_blob_key("shiftleft-envfile-model-serving"),
            "envFileModelServing",
        )

    def test_passes_through_vault_blob_key_names(self) -> None:
        self.assertEqual(shift_left_vault_blob_key("envFileMlflow"), "envFileMlflow")
        self.assertEqual(VAULT_APPROLE_SECRET, "vault-approle")


class MergeModelServingEnvTest(unittest.TestCase):
    def test_serving_buckets_win_over_mlflow_and_use_common_aws(self) -> None:
        data = {
            "envFileCommon": "AWS_ACCESS_KEY_ID=AKIA_COMMON\nAWS_SECRET_ACCESS_KEY=common-secret\n",
            "envFileModelServing": (
                "CI_S3_BUCKET_NAME=ods-ci-s3\n"
                "MODELS_S3_BUCKET_NAME=ods-ci-wisdom\n"
            ),
            "envFileMlflow": "BUCKET=mlflow-e2e\nAWS_ACCESS_KEY_ID=AKIA_MLFLOW\n",
            "envFile-for-rhelaiteam": (
                "AWS_ACCESS_KEY_ID=AKIA_RHEL\n"
                "AWS_SECRET_ACCESS_KEY=rhel-secret\n"
                "CI_S3_BUCKET_NAME=should-not-win\n"
            ),
        }
        merged = merge_model_serving_env(data)
        self.assertEqual(merged["CI_S3_BUCKET_NAME"], "ods-ci-s3")
        self.assertEqual(merged["MODELS_S3_BUCKET_NAME"], "ods-ci-wisdom")
        self.assertEqual(merged["AWS_ACCESS_KEY_ID"], "AKIA_COMMON")
        self.assertNotEqual(merged.get("CI_S3_BUCKET_NAME"), "mlflow-e2e")
        self.assertNotEqual(merged.get("CI_S3_BUCKET_NAME"), "should-not-win")

    def test_falls_back_to_rhelaiteam_aws_when_common_missing(self) -> None:
        data = {
            "envFileCommon": "UNRELATED=1\n",
            "envFileModelServing": "CI_S3_BUCKET_NAME=ods-ci-s3\n",
            "envFile-for-rhelaiteam": (
                "AWS_ACCESS_KEY_ID=AKIA_RHEL\nAWS_SECRET_ACCESS_KEY=rhel-secret\n"
            ),
        }
        merged = merge_model_serving_env(data)
        self.assertEqual(merged["AWS_ACCESS_KEY_ID"], "AKIA_RHEL")
        self.assertEqual(merged["CI_S3_BUCKET_NAME"], "ods-ci-s3")

    def test_preserves_session_token_from_common_aws(self) -> None:
        data = {
            "envFileCommon": (
                "AWS_ACCESS_KEY_ID=AKIA_COMMON\n"
                "AWS_SECRET_ACCESS_KEY=common-secret\n"
                "AWS_SESSION_TOKEN=session-token\n"
            ),
            "envFileModelServing": "CI_S3_BUCKET_NAME=ods-ci-s3\n",
        }
        merged = merge_model_serving_env(data)
        self.assertEqual(merged["AWS_SESSION_TOKEN"], "session-token")


class StageShiftLeftFilesTest(unittest.TestCase):
    def test_writes_one_file_per_key_and_cypress_yaml(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            dest = Path(tmp)
            data = {
                "envFileMlflow": "BUCKET=mlflow-e2e\nAWS_ACCESS_KEY_ID=AKIA_MF\n",
                "volumeFileTestVariables": "cluster:\n  auth: htpasswd\n",
            }
            written = stage_shift_left_files(
                data,
                dest,
                blob_key="envFileMlflow",
                include_model_serving=False,
            )
            self.assertEqual((dest / "BUCKET").read_text(encoding="utf-8").strip(), "mlflow-e2e")
            self.assertEqual(
                (dest / "AWS_ACCESS_KEY_ID").read_text(encoding="utf-8").strip(), "AKIA_MF"
            )
            self.assertIn("BUCKET", written)
            self.assertFalse((dest / "test-variables.yml").exists())

            stage_shift_left_files(
                data,
                dest,
                blob_key="volumeFileTestVariables",
                include_model_serving=False,
            )
            self.assertIn("auth: htpasswd", (dest / "test-variables.yml").read_text())


class SecretSourceTest(unittest.TestCase):
    def test_resolve_secret_source_env_and_workspace(self) -> None:
        from k8s.vault_runtime import resolve_secret_source

        self.assertEqual(resolve_secret_source({"SECRET_SOURCE": "vault"}), "vault")
        self.assertEqual(resolve_secret_source({"SECRET_SOURCE": "tenant"}), "tenant")
        self.assertEqual(resolve_secret_source({"SECRET_SOURCE": "konflux"}), "tenant")
        self.assertEqual(resolve_secret_source({}), "vault")

    def test_copy_tenant_secret_files(self) -> None:
        from k8s.vault_runtime import copy_tenant_secret_files

        with tempfile.TemporaryDirectory() as tmp:
            src = Path(tmp) / "src"
            dest = Path(tmp) / "dest"
            src.mkdir()
            (src / "AWS_ACCESS_KEY_ID").write_text("AKIA_TENANT\n", encoding="utf-8")
            written = copy_tenant_secret_files(src, dest)
            self.assertIn("AWS_ACCESS_KEY_ID", written)
            self.assertEqual(
                (dest / "AWS_ACCESS_KEY_ID").read_text(encoding="utf-8").strip(),
                "AKIA_TENANT",
            )


class VaultHttpTest(unittest.TestCase):
    def test_login_and_kv_get_uses_cacert_and_returns_data(self) -> None:
        payloads = [
            json.dumps({"auth": {"client_token": "s." + "x" * 20}}).encode(),
            json.dumps(
                {
                    "data": {
                        "data": {
                            "envFileCommon": "AWS_ACCESS_KEY_ID=AKIA_HTTP\n",
                            "envFileModelServing": "CI_S3_BUCKET_NAME=ods-ci-s3\n",
                        }
                    }
                }
            ).encode(),
        ]

        class _Resp:
            def __init__(self, body: bytes) -> None:
                self._body = body

            def read(self) -> bytes:
                return self._body

            def __enter__(self) -> "_Resp":
                return self

            def __exit__(self, *args: object) -> None:
                return None

        opener = mock.Mock(side_effect=[_Resp(p) for p in payloads])
        with tempfile.TemporaryDirectory() as tmp:
            ca = Path(tmp) / "ca.crt"
            ca.write_text("-----BEGIN CERTIFICATE-----\nM\n-----END CERTIFICATE-----\n")
            data = vault_login_and_read_shift_left(
                vault_addr="https://vault.example:8200",
                role_id="rhods-ci",
                secret_id="unused-in-test",
                ca_path=ca,
                urlopen=opener,
            )
        self.assertEqual(data["envFileModelServing"].strip(), "CI_S3_BUCKET_NAME=ods-ci-s3")
        self.assertEqual(opener.call_count, 2)
        login_req = opener.call_args_list[0][0][0]
        self.assertIn("/v1/auth/approle/login", login_req.full_url)
        kv_req = opener.call_args_list[1][0][0]
        self.assertIn("/v1/apps/data/rhods-ci/shift-left", kv_req.full_url)
        self.assertEqual(kv_req.get_header("X-vault-token"), "s." + "x" * 20)


class LoadHcpInstallAwsCredentialsTest(unittest.TestCase):
    def test_prefers_environment_when_set(self) -> None:
        creds = load_hcp_install_aws_credentials(
            auth_dir=Path("/missing"),
            environ={
                "AWS_ACCESS_KEY_ID": "AKIA_ENV",
                "AWS_SECRET_ACCESS_KEY": "env-secret",
            },
        )
        self.assertEqual(creds["AWS_ACCESS_KEY_ID"], "AKIA_ENV")

    def test_reads_openshift_vault_keys(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            auth = Path(tmp)
            auth.joinpath("VAULT_ADDR").write_text("https://vault.example:8200\n", encoding="utf-8")
            auth.joinpath("role_id").write_text("role\n", encoding="utf-8")
            auth.joinpath("secret_id").write_text("secret\n", encoding="utf-8")
            auth.joinpath("ca.crt").write_text(
                "-----BEGIN CERTIFICATE-----\nM\n-----END CERTIFICATE-----\n",
                encoding="utf-8",
            )
            with mock.patch(
                "k8s.vault_runtime.vault_login_and_read_kv_data",
                return_value={
                    "aws_access_key_id": "AKIA_OPENSHIFT",
                    "aws_secret_access_key": "openshift-secret",
                },
            ):
                creds = load_hcp_install_aws_credentials(auth_dir=auth, environ={})
        self.assertEqual(creds["AWS_ACCESS_KEY_ID"], "AKIA_OPENSHIFT")
        self.assertEqual(creds["AWS_SECRET_ACCESS_KEY"], "openshift-secret")


if __name__ == "__main__":
    raise SystemExit(unittest.main())
