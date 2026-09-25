"""Tests for cluster_registry safe pull-secret merge."""

from __future__ import annotations

import base64
import unittest
from unittest.mock import patch

from install import cluster_registry as cr
from install.cluster_registry import merge_docker_auths, rhoai_scoped_dockerconfig


def _b64_user_pass(user: str, password: str = "x") -> str:
    return base64.standard_b64encode(f"{user}:{password}".encode()).decode()


class RhoaiScopedDockerconfigTest(unittest.TestCase):
    def test_strips_bare_quay_io_from_overlay(self) -> None:
        quay = {
            "auths": {
                "quay.io": {"auth": "cmhvYWk="},
                "quay.io/rhoai": {"auth": "cmhvYWk="},
                "registry.redhat.io": {"auth": "cmg="},
            }
        }
        scoped = rhoai_scoped_dockerconfig(quay)
        self.assertIn("quay.io/rhoai", scoped["auths"])
        self.assertNotIn("quay.io", scoped["auths"])
        self.assertNotIn("registry.redhat.io", scoped["auths"])

    def test_merge_preserves_existing_bare_quay_io(self) -> None:
        existing = {"auths": {"quay.io": {"auth": "broad"}, "registry.redhat.io": {"auth": "rh"}}}
        overlay = rhoai_scoped_dockerconfig(
            {"auths": {"quay.io": {"auth": "rhoai-only"}, "quay.io/rhoai": {"auth": "rhoai-only"}}}
        )
        merged = merge_docker_auths(existing, overlay)
        self.assertEqual(merged["auths"]["quay.io"]["auth"], "broad")
        self.assertEqual(merged["auths"]["quay.io/rhoai"]["auth"], "rhoai-only")


class OpenshiftReleaseDevAuthTest(unittest.TestCase):
    def test_rhoai_bare_quay_does_not_cover_release_dev(self) -> None:
        auths = {"quay.io": {"auth": _b64_user_pass("rhoai+rhoai_devops_konfux_bot")}}
        self.assertFalse(cr.quay_auth_covers_openshift_release_dev(auths))

    def test_openshift_release_dev_user_on_bare_quay_covers(self) -> None:
        auths = {"quay.io": {"auth": _b64_user_pass("openshift-release-dev+abc")}}
        self.assertTrue(cr.quay_auth_covers_openshift_release_dev(auths))

    def test_scoped_openshift_release_dev_key_covers(self) -> None:
        auths = {
            "quay.io": {"auth": _b64_user_pass("rhoai+bot")},
            "quay.io/openshift-release-dev": {
                "auth": _b64_user_pass("openshift-release-dev+abc")
            },
        }
        self.assertTrue(cr.quay_auth_covers_openshift_release_dev(auths))

    def test_ensure_copies_cloud_openshift_auth(self) -> None:
        cloud_auth = _b64_user_pass("openshift-release-dev+abc")
        existing = {
            "auths": {
                "quay.io": {"auth": _b64_user_pass("rhoai+bot")},
                "cloud.openshift.com": {"auth": cloud_auth},
            }
        }
        pull_secret = {
            "apiVersion": "v1",
            "kind": "Secret",
            "metadata": {"name": "pull-secret", "namespace": "openshift-config"},
            "data": {
                ".dockerconfigjson": base64.standard_b64encode(
                    __import__("json").dumps(existing).encode()
                ).decode()
            },
        }
        applied: list[str] = []

        def _oc(args: list[str], **kwargs: object) -> object:
            if args[:2] == ["get", "secret/pull-secret"]:
                return type(
                    "R",
                    (),
                    {"returncode": 0, "stdout": __import__("json").dumps(pull_secret)},
                )()
            if args[:2] == ["apply", "-f"]:
                applied.append(str(kwargs.get("stdin_text") or ""))
                return type("R", (), {"returncode": 0, "stdout": ""})()
            raise AssertionError(args)

        with patch.object(cr, "run_oc", side_effect=_oc):
            changed = cr.ensure_openshift_release_dev_pull_auth()
        self.assertTrue(changed)
        self.assertEqual(len(applied), 1)
        patched = __import__("json").loads(applied[0])
        cfg = __import__("json").loads(
            base64.standard_b64decode(patched["data"][".dockerconfigjson"])
        )
        self.assertEqual(
            cfg["auths"]["quay.io/openshift-release-dev"]["auth"],
            cloud_auth,
        )
        self.assertEqual(cfg["auths"]["quay.io"]["auth"], _b64_user_pass("rhoai+bot"))


if __name__ == "__main__":
    unittest.main()
