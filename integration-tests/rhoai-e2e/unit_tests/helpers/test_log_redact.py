"""Unit tests for helpers.log_redact."""

from __future__ import annotations

import unittest

from helpers.log_redact import redact_command_for_log

_JWT = (
    "eyJhbGciOiJSUzI1NiIsInR5cCI6IkpXVCJ9."
    "eyJzdWIiOiIxMjM0NTY3ODkwIn0."
    "signaturepart"
)


class LogRedactTest(unittest.TestCase):
    def test_redacts_sensitive_assignments(self) -> None:
        cases = (
            (
                'export TEST_USER_PASSWORD="s3cret!"; npx cypress run',
                "s3cret!",
                'TEST_USER_PASSWORD="***"',
            ),
            (
                f'npx cypress run --env OC_TOKEN="{_JWT}",CLUSTER_AUTH=""',
                _JWT,
                'OC_TOKEN="***"',
            ),
            (
                "export CYPRESS_OC_TOKEN=tokensecret PATH=/bin",
                "tokensecret",
                "CYPRESS_OC_TOKEN=***",
            ),
        )
        for cmd, secret, redacted in cases:
            with self.subTest(redacted=redacted):
                out = redact_command_for_log(cmd)
                self.assertNotIn(secret, out)
                self.assertIn(redacted, out)

    def test_preserves_non_secret_flags(self) -> None:
        cmd = 'npx cypress run --env skipTags="@Smoke",OPERATOR_NAMESPACE="ns"'
        self.assertEqual(redact_command_for_log(cmd), cmd)

    def test_empty_passthrough(self) -> None:
        self.assertEqual(redact_command_for_log(""), "")


if __name__ == "__main__":
    unittest.main()
