"""Kubeconfig materialization helpers for Tekton pytest tasks."""

from __future__ import annotations

import os
import stat
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from steps import tekton_util as tu  # noqa: E402

class SyncMaterializedKubeconfigTests(unittest.TestCase):
    def test_read_only_dest_does_not_raise(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            src = Path(tmp) / "writable-kubeconfig"
            dst = Path(tmp) / "readonly-kubeconfig"
            src.write_text("apiVersion: v1\n", encoding="utf-8")
            dst.write_text("old\n", encoding="utf-8")
            os.chmod(dst, stat.S_IRUSR)

            with mock.patch.dict(
                os.environ,
                {"KUBECONFIG": str(src)},
                clear=False,
            ):
                tu.sync_materialized_kubeconfig_to(str(dst))

            self.assertTrue(src.is_file())

    def test_synced_dest_is_world_readable_for_cross_step_handoff(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            src = Path(tmp) / "writable-kubeconfig"
            dst = Path(tmp) / "shared-kubeconfig"
            src.write_text("apiVersion: v1\n", encoding="utf-8")

            with mock.patch.dict(
                os.environ,
                {"KUBECONFIG": str(src)},
                clear=False,
            ):
                tu.sync_materialized_kubeconfig_to(str(dst))

            mode = dst.stat().st_mode
            self.assertEqual(mode & stat.S_IRUSR, stat.S_IRUSR)
            self.assertEqual(mode & stat.S_IRGRP, stat.S_IRGRP)
            self.assertEqual(mode & stat.S_IROTH, stat.S_IROTH)


class HtpasswdKubeconfigLoginTests(unittest.TestCase):
    def test_uses_password_flag_when_stdin_unsupported(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            kc = Path(tmp) / "kubeconfig"
            kc.write_text(
                "apiVersion: v1\n"
                "clusters:\n"
                "- cluster:\n"
                "    server: https://api.example:6443\n"
                "  name: default\n"
                "contexts:\n"
                "- context:\n"
                "    cluster: default\n"
                "    user: admin\n"
                "  name: default\n"
                "current-context: default\n"
                "kind: Config\n"
                "users:\n"
                "- name: admin\n"
                "  user:\n"
                "    token: tok\n",
                encoding="utf-8",
            )
            env = {"KUBECONFIG": str(kc)}
            login_calls: list[list[str]] = []

            def fake_run(cmd, **kwargs):
                login_calls.append(list(cmd))
                if cmd[1:3] == ["login", "--help"]:
                    return mock.Mock(returncode=0, stdout="Usage: oc login -u USER -p PASS", stderr="")
                if len(cmd) >= 2 and cmd[1] == "login":
                    return mock.Mock(returncode=0, stdout="", stderr="")
                if cmd[-1:] == ["whoami"]:
                    return mock.Mock(returncode=0, stdout="htpasswd-user\n", stderr="")
                return mock.Mock(returncode=0, stdout="", stderr="")

            with (
                mock.patch("steps.tekton_util.run", side_effect=fake_run),
                mock.patch("steps.tekton_util._resolve_oc_binary", return_value="/usr/bin/oc"),
                mock.patch("steps.tekton_util.ensure_writable_kubeconfig"),
                mock.patch("steps.tekton_util.backup_kubeconfig_for_admin_restore"),
            ):
                ok = tu.materialize_htpasswd_kubeconfig_login(
                    "htpasswd-user",
                    "secret",
                    environ=env,
                )
            self.assertTrue(ok)
            login_cmd = next(c for c in login_calls if len(c) >= 2 and c[1] == "login" and c[2] != "--help")
            self.assertIn("-p", login_cmd)
            self.assertIn("secret", login_cmd)
            self.assertNotIn("--password-stdin", login_cmd)

    def test_uses_password_stdin_when_supported(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            kc = Path(tmp) / "kubeconfig"
            kc.write_text(
                "apiVersion: v1\n"
                "clusters:\n"
                "- cluster:\n"
                "    server: https://api.example:6443\n"
                "  name: default\n"
                "contexts:\n"
                "- context:\n"
                "    cluster: default\n"
                "    user: admin\n"
                "  name: default\n"
                "current-context: default\n"
                "kind: Config\n"
                "users:\n"
                "- name: admin\n"
                "  user:\n"
                "    token: tok\n",
                encoding="utf-8",
            )
            env = {"KUBECONFIG": str(kc)}
            login_calls: list[list[str]] = []
            stdin_password: list[str | None] = []

            def fake_run(cmd, **kwargs):
                login_calls.append(list(cmd))
                stdin_password.append(kwargs.get("input_text"))
                if cmd[1:3] == ["login", "--help"]:
                    return mock.Mock(
                        returncode=0,
                        stdout="Usage: oc login --password-stdin",
                        stderr="",
                    )
                if len(cmd) >= 2 and cmd[1] == "login":
                    return mock.Mock(returncode=0, stdout="", stderr="")
                if cmd[-1:] == ["whoami"]:
                    return mock.Mock(returncode=0, stdout="htpasswd-user\n", stderr="")
                return mock.Mock(returncode=0, stdout="", stderr="")

            with (
                mock.patch("steps.tekton_util.run", side_effect=fake_run),
                mock.patch("steps.tekton_util._resolve_oc_binary", return_value="/usr/bin/oc"),
                mock.patch("steps.tekton_util.ensure_writable_kubeconfig"),
                mock.patch("steps.tekton_util.backup_kubeconfig_for_admin_restore"),
            ):
                ok = tu.materialize_htpasswd_kubeconfig_login(
                    "htpasswd-user",
                    "secret",
                    environ=env,
                )
            self.assertTrue(ok)
            login_cmd = next(c for c in login_calls if "--password-stdin" in c)
            self.assertIn("--password-stdin", login_cmd)
            login_idx = login_calls.index(login_cmd)
            self.assertEqual(stdin_password[login_idx], "secret")

