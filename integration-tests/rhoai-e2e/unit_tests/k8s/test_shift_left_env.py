"""Tests for Jenkins shift-left env loading."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from k8s.shift_left_env import (
    is_stageable_smoke_secret_key,
    load_shift_left_env_from_mount,
    promote_shift_left_aws_env,
    resolve_ci_s3_smoke_fields,
    suppress_ephemeral_jira_env,
)

class ShiftLeftEnvTests(unittest.TestCase):
    def test_is_stageable_smoke_secret_key(self) -> None:
        self.assertTrue(is_stageable_smoke_secret_key("CI_S3_BUCKET_NAME"))
        self.assertTrue(is_stageable_smoke_secret_key("AWS_ACCESS_KEY_ID"))
        self.assertFalse(is_stageable_smoke_secret_key("openldap"))

    def test_load_vault_key_names(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            (base / "CI_S3_BUCKET_NAME").write_text("ods-ci-s3\n", encoding="utf-8")
            (base / "CI_S3_BUCKET_REGION").write_text("us-east-1", encoding="utf-8")
            (base / "CI_S3_BUCKET_ENDPOINT").write_text(
                "https://s3.us-east-1.amazonaws.com", encoding="utf-8"
            )
            env: dict[str, str] = {}
            load_shift_left_env_from_mount(base, environ=env)
            self.assertEqual(env["CI_S3_BUCKET_NAME"], "ods-ci-s3")
            self.assertEqual(env["CI_S3_BUCKET_REGION"], "us-east-1")
            self.assertEqual(env["CI_S3_BUCKET_ENDPOINT"], "https://s3.us-east-1.amazonaws.com")

    def test_ca_bundle_sets_ssl_cert_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            (base / "AWS_CA_BUNDLE").write_text(
                "-----BEGIN CERTIFICATE-----\nTEST\n-----END CERTIFICATE-----\n",
                encoding="utf-8",
            )
            env: dict[str, str] = {
                "SSL_CERT_DIR": "/tekton-custom-certs:/etc/ssl/certs:/etc/pki/tls/certs",
            }
            load_shift_left_env_from_mount(base, environ=env)
            self.assertEqual(env["SSL_CERT_FILE"], "/tmp/olminstall-trust-bundle.pem")
            self.assertNotIn(":", env.get("SSL_CERT_DIR", ""))

    def test_legacy_aws_mapping(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            (base / "AWS_S3_BUCKET").write_text("legacy-bucket", encoding="utf-8")
            (base / "AWS_S3_ENDPOINT").write_text("https://s3.example.com", encoding="utf-8")
            (base / "AWS_DEFAULT_REGION").write_text("eu-west-1", encoding="utf-8")
            env: dict[str, str] = {}
            load_shift_left_env_from_mount(base, environ=env)
            self.assertEqual(env["CI_S3_BUCKET_NAME"], "legacy-bucket")
            self.assertEqual(env["CI_S3_BUCKET_ENDPOINT"], "https://s3.example.com")
            self.assertEqual(env["CI_S3_BUCKET_REGION"], "eu-west-1")

    def test_resolve_ci_s3_smoke_fields_legacy_aws_mapping(self) -> None:
        fields = resolve_ci_s3_smoke_fields(
            {
                "AWS_ACCESS_KEY_ID": "ak",
                "AWS_SECRET_ACCESS_KEY": "sk",
                "AWS_S3_BUCKET": "legacy-bucket",
                "AWS_S3_ENDPOINT": "https://s3.example.com",
                "AWS_DEFAULT_REGION": "eu-west-1",
            }
        )
        self.assertIsNotNone(fields)
        assert fields is not None
        self.assertEqual(fields["NAME"], "legacy-bucket")
        self.assertEqual(fields["ENDPOINT"], "https://s3.example.com")
        self.assertEqual(fields["REGION"], "eu-west-1")

    def test_promote_shift_left_aws_env_from_mount_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            (base / "aws-access-key-id").write_text("AKIA_TEST", encoding="utf-8")
            (base / "aws-secret-access-key").write_text("secret", encoding="utf-8")
            env: dict[str, str] = {}
            promote_shift_left_aws_env(base, environ=env)
            self.assertEqual(env["AWS_ACCESS_KEY_ID"], "AKIA_TEST")
            self.assertEqual(env["AWS_SECRET_ACCESS_KEY"], "secret")

    def test_suppress_ephemeral_jira_env_localhost(self) -> None:
        env = {
            "PYTEST_JIRA_URL": "http://localhost:2990/jira",
            "PYTEST_JIRA_TOKEN": "tok",
        }
        suppress_ephemeral_jira_env(env)
        self.assertEqual(env.get("PYTEST_JIRA_URL"), "")
        self.assertEqual(env.get("PYTEST_JIRA_DISABLE"), "1")
        self.assertNotIn("PYTEST_JIRA_TOKEN", env)

    def test_suppress_ephemeral_jira_env_when_unset(self) -> None:
        env: dict[str, str] = {}
        suppress_ephemeral_jira_env(env)
        self.assertEqual(env.get("PYTEST_JIRA_DISABLE"), "1")

