"""Tests for ensure_kubeconfig_bearer_token (EPHC exec-auth kubeconfig → pytest token)."""

from __future__ import annotations

import os
import tempfile
import unittest
from contextlib import ExitStack
from pathlib import Path
from unittest import mock

from steps import tekton_util

class EnsureKubeconfigBearerTokenTests(unittest.TestCase):
    def _kubeconfig_with_cluster_server(self, lines: list[str]) -> str:
        body = list(lines)
        if not any(line.strip().startswith("clusters:") for line in body):
            insert_at = next(
                (index for index, line in enumerate(body) if line.strip().startswith("users:")),
                len(body),
            )
            body[insert_at:insert_at] = [
                "clusters:",
                "  - name: c",
                "    cluster:",
                "      server: https://api.test:6443",
                "      certificate-authority-data: Y2E=",
            ]
        return "\n".join(body) + "\n"

    def _admin_token_patches(self) -> ExitStack:
        stack = ExitStack()
        stack.enter_context(mock.patch.object(tekton_util, "_oc_has_cluster_admin", return_value=True))
        stack.enter_context(mock.patch.object(tekton_util, "_token_has_cluster_admin", return_value=True))
        return stack

    def test_skips_when_token_already_present(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            kc = Path(tmp) / "kc"
            kc.write_text(
                "\n".join(
                    [
                        "current-context: ctx",
                        "contexts:",
                        "  - name: ctx",
                        "    context:",
                        "      cluster: c",
                        "      user: u",
                        "users:",
                        "  - name: u",
                        "    user:",
                        "      token: existing",
                    ]
                )
                + "\n",
                encoding="utf-8",
            )
            env = {"KUBECONFIG": str(kc)}
            whoami_t = mock.Mock(returncode=0, stdout="existing\n")
            with mock.patch.object(tekton_util, "_resolve_oc_binary", return_value="/usr/bin/oc"), mock.patch.object(
                tekton_util, "run", return_value=whoami_t
            ) as run_mock, mock.patch.object(
                tekton_util, "_token_authenticated", return_value=True
            ):
                tekton_util.ensure_kubeconfig_bearer_token(env)
            self.assertGreaterEqual(run_mock.call_count, 1)
            self.assertEqual(tekton_util._kubeconfig_bearer_token(Path(env["KUBECONFIG"])), "existing")

    def test_materializes_token_from_oc(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            kc = Path(tmp) / "kc"
            kc.write_text(
                "\n".join(
                    [
                        "current-context: ctx",
                        "contexts:",
                        "  - name: ctx",
                        "    context:",
                        "      cluster: c",
                        "      user: u",
                        "users:",
                        "  - name: u",
                        "    user:",
                        "      exec:",
                        "        command: oc",
                    ]
                )
                + "\n",
                encoding="utf-8",
            )
            env = {"KUBECONFIG": str(kc), "ARTIFACTS_DIR": tmp}
            proc = mock.Mock(returncode=0, stdout="fresh-token\n")
            with ExitStack() as stack:
                stack.enter_context(mock.patch.object(tekton_util, "_resolve_oc_binary", return_value="/usr/bin/oc"))
                stack.enter_context(mock.patch.object(tekton_util, "run", return_value=proc))
                stack.enter_context(mock.patch.object(tekton_util, "_oc_has_cluster_admin", return_value=True))
                stack.enter_context(mock.patch.object(tekton_util, "_oc_authenticated", return_value=True))
                stack.enter_context(mock.patch.object(tekton_util, "_ensure_rhoai_e2e_cluster_admin_sa", return_value=True))
                stack.enter_context(mock.patch.object(tekton_util, "_token_has_cluster_admin", return_value=True))
                stack.enter_context(mock.patch.object(tekton_util, "_token_authenticated", return_value=True))
                tekton_util.ensure_kubeconfig_bearer_token(env)
            target = Path(env["KUBECONFIG"])
            self.assertEqual(tekton_util._kubeconfig_bearer_token(target), "fresh-token")

    def test_mints_token_for_client_cert_when_whoami_t_empty(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            kc = Path(tmp) / "kc"
            kc.write_text(
                self._kubeconfig_with_cluster_server(
                    [
                        "current-context: ctx",
                        "contexts:",
                        "  - name: ctx",
                        "    context:",
                        "      cluster: c",
                        "      user: u",
                        "users:",
                        "  - name: u",
                        "    user:",
                        "      client-certificate-data: Y2VydA==",
                        "      client-key-data: a2V5",
                    ]
                ),
                encoding="utf-8",
            )
            env = {"KUBECONFIG": str(kc), "ARTIFACTS_DIR": tmp}
            whoami_t = mock.Mock(returncode=0, stdout="\n")
            whoami = mock.Mock(returncode=0, stdout="kube:admin\n")
            create_token = mock.Mock(returncode=0, stdout="minted-token\n")

            def run_side_effect(cmd, **kwargs):
                if cmd[-2:] == ["whoami", "-t"]:
                    return whoami_t
                if cmd[-1] == "whoami":
                    return whoami
                if "rhoai-e2e-cluster-admin" in cmd and "create" in cmd and "token" in cmd:
                    return create_token
                return mock.Mock(returncode=1, stdout="")

            with ExitStack() as stack:
                stack.enter_context(mock.patch.object(tekton_util, "_resolve_oc_binary", return_value="/usr/bin/oc"))
                stack.enter_context(mock.patch.object(tekton_util, "run", side_effect=run_side_effect))
                stack.enter_context(mock.patch.object(tekton_util, "_oc_has_cluster_admin", return_value=True))
                stack.enter_context(mock.patch.object(tekton_util, "_oc_authenticated", return_value=True))
                stack.enter_context(mock.patch.object(tekton_util, "_ensure_rhoai_e2e_cluster_admin_sa", return_value=True))
                stack.enter_context(mock.patch.object(tekton_util, "_token_has_cluster_admin", return_value=True))
                tekton_util.ensure_kubeconfig_bearer_token(env)
            self.assertEqual(tekton_util._kubeconfig_bearer_token(Path(env["KUBECONFIG"])), "minted-token")
            self.assertEqual(env.get("OPENSHIFT_TOKEN"), "minted-token")

    def test_replaces_stale_user_token_when_whoami_t_empty(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            kc = Path(tmp) / "kc"
            kc.write_text(
                "\n".join(
                    [
                        "current-context: ctx",
                        "contexts:",
                        "  - name: ctx",
                        "    context:",
                        "      cluster: c",
                        "      user: u",
                        "users:",
                        "  - name: u",
                        "    user:",
                        "      token: stale-ephc-token",
                    ]
                )
                + "\n",
                encoding="utf-8",
            )
            env = {"KUBECONFIG": str(kc), "ARTIFACTS_DIR": tmp, "CLUSTER_SOURCE": "EPHC"}
            whoami_t = mock.Mock(returncode=0, stdout="\n")
            whoami = mock.Mock(returncode=0, stdout="kube:admin\n")
            create_token = mock.Mock(returncode=0, stdout="fresh-minted\n")

            def run_side_effect(cmd, **kwargs):
                if cmd[-2:] == ["whoami", "-t"]:
                    return whoami_t
                if cmd[-1] == "whoami":
                    return whoami
                if "rhoai-e2e-cluster-admin" in cmd and "create" in cmd and "token" in cmd:
                    return create_token
                return mock.Mock(returncode=1, stdout="")

            with ExitStack() as stack:
                stack.enter_context(mock.patch.object(tekton_util, "_resolve_oc_binary", return_value="/usr/bin/oc"))
                stack.enter_context(mock.patch.object(tekton_util, "run", side_effect=run_side_effect))
                stack.enter_context(mock.patch.object(tekton_util, "_oc_has_cluster_admin", return_value=True))
                stack.enter_context(mock.patch.object(tekton_util, "_oc_authenticated", return_value=True))
                stack.enter_context(mock.patch.object(tekton_util, "_ensure_rhoai_e2e_cluster_admin_sa", return_value=True))
                stack.enter_context(mock.patch.object(tekton_util, "_token_has_cluster_admin", return_value=True))
                tekton_util.ensure_kubeconfig_bearer_token(env)
            self.assertEqual(tekton_util._kubeconfig_bearer_token(Path(env["KUBECONFIG"])), "fresh-minted")
            self.assertEqual(env.get("OPENSHIFT_TOKEN"), "fresh-minted")

    def test_mints_on_ephc_when_whoami_t_returns_stale_embedded_token(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            kc = Path(tmp) / "kc"
            kc.write_text(
                self._kubeconfig_with_cluster_server(
                    [
                        "current-context: ctx",
                        "contexts:",
                        "  - name: ctx",
                        "    context:",
                        "      cluster: c",
                        "      user: u",
                        "users:",
                        "  - name: u",
                        "    user:",
                        "      token: stale-ephc-token",
                    ]
                ),
                encoding="utf-8",
            )
            env = {"KUBECONFIG": str(kc), "ARTIFACTS_DIR": tmp, "CLUSTER_SOURCE": "EPHC"}
            whoami_t = mock.Mock(returncode=0, stdout="stale-ephc-token\n")
            whoami = mock.Mock(returncode=0, stdout="kube:admin\n")
            create_token = mock.Mock(returncode=0, stdout="ephc-fresh-mint\n")

            def run_side_effect(cmd, **kwargs):
                if cmd[-2:] == ["whoami", "-t"]:
                    return whoami_t
                if cmd[-1] == "whoami":
                    return whoami
                if "rhoai-e2e-cluster-admin" in cmd and "create" in cmd and "token" in cmd:
                    return create_token
                return mock.Mock(returncode=1, stdout="")

            with ExitStack() as stack:
                stack.enter_context(mock.patch.object(tekton_util, "_resolve_oc_binary", return_value="/usr/bin/oc"))
                stack.enter_context(mock.patch.object(tekton_util, "run", side_effect=run_side_effect))
                stack.enter_context(mock.patch.object(tekton_util, "_oc_has_cluster_admin", return_value=True))
                stack.enter_context(mock.patch.object(tekton_util, "_oc_authenticated", return_value=True))
                stack.enter_context(mock.patch.object(tekton_util, "_ensure_rhoai_e2e_cluster_admin_sa", return_value=True))
                stack.enter_context(mock.patch.object(tekton_util, "_token_has_cluster_admin", return_value=True))
                tekton_util.ensure_kubeconfig_bearer_token(env)
            self.assertEqual(tekton_util._kubeconfig_bearer_token(Path(env["KUBECONFIG"])), "ephc-fresh-mint")
            self.assertEqual(env.get("OPENSHIFT_TOKEN"), "ephc-fresh-mint")

    def test_materializes_minted_token_when_token_post_check_fails_but_admin_mint_trusted(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            kc = Path(tmp) / "kc"
            kc.write_text(
                self._kubeconfig_with_cluster_server(
                    [
                        "current-context: ctx",
                        "contexts:",
                        "  - name: ctx",
                        "    context:",
                        "      cluster: c",
                        "      user: u",
                        "users:",
                        "  - name: u",
                        "    user:",
                        "      client-certificate-data: Y2VydA==",
                        "      client-key-data: a2V5",
                    ]
                ),
                encoding="utf-8",
            )
            admin_kc = Path(tmp) / "admin-kc"
            admin_kc.write_text(kc.read_text(encoding="utf-8"), encoding="utf-8")
            env = {"KUBECONFIG": str(kc), "ARTIFACTS_DIR": tmp, "CLUSTER_SOURCE": "EPHC"}
            whoami_t = mock.Mock(returncode=0, stdout="\n")
            whoami = mock.Mock(returncode=0, stdout="kube:admin\n")
            create_token = mock.Mock(returncode=0, stdout="minted-token\n")

            def run_side_effect(cmd, **kwargs):
                if cmd[-2:] == ["whoami", "-t"]:
                    return whoami_t
                if cmd[-1] == "whoami":
                    return whoami
                if "rhoai-e2e-cluster-admin" in cmd and "create" in cmd and "token" in cmd:
                    return create_token
                return mock.Mock(returncode=1, stdout="")

            def token_has_cluster_admin_side_effect(oc, token, kubeconfig_path, check_env):
                return False

            with ExitStack() as stack:
                stack.enter_context(mock.patch.object(tekton_util, "_resolve_oc_binary", return_value="/usr/bin/oc"))
                stack.enter_context(mock.patch.object(tekton_util, "run", side_effect=run_side_effect))
                stack.enter_context(mock.patch.object(tekton_util, "_oc_has_cluster_admin", return_value=True))
                stack.enter_context(mock.patch.object(tekton_util, "_oc_authenticated", return_value=True))
                stack.enter_context(mock.patch.object(tekton_util, "_ensure_rhoai_e2e_cluster_admin_sa", return_value=True))
                stack.enter_context(
                    mock.patch.object(
                        tekton_util,
                        "_token_has_cluster_admin",
                        side_effect=token_has_cluster_admin_side_effect,
                    )
                )
                stack.enter_context(mock.patch.object(tekton_util, "_admin_kubeconfig_path", return_value=admin_kc))
                tekton_util.ensure_kubeconfig_bearer_token(env)
            self.assertEqual(tekton_util._kubeconfig_bearer_token(Path(env["KUBECONFIG"])), "minted-token")

    def test_refreshes_stale_embedded_token_on_external_cluster(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            kc = Path(tmp) / "kc"
            kc.write_text(
                self._kubeconfig_with_cluster_server(
                    [
                        "current-context: ctx",
                        "contexts:",
                        "  - name: ctx",
                        "    context:",
                        "      cluster: c",
                        "      user: u",
                        "users:",
                        "  - name: u",
                        "    user:",
                        "      token: stale-token",
                    ]
                ),
                encoding="utf-8",
            )
            env = {
                "KUBECONFIG": str(kc),
                "ARTIFACTS_DIR": tmp,
                "CLUSTER_SOURCE": "rhoai-e2e-kubeconfig-ods-qe-psi-07",
            }
            whoami_t = mock.Mock(returncode=0, stdout="stale-token\n")

            def token_auth_side_effect(oc, token, kubeconfig_path, check_env):
                return token == "fresh-exec-token"

            with ExitStack() as stack:
                stack.enter_context(mock.patch.object(tekton_util, "_resolve_oc_binary", return_value="/usr/bin/oc"))
                stack.enter_context(mock.patch.object(tekton_util, "run", return_value=whoami_t))
                stack.enter_context(
                    mock.patch.object(
                        tekton_util,
                        "_token_from_exec_user_block",
                        return_value="fresh-exec-token",
                    )
                )
                stack.enter_context(
                    mock.patch.object(
                        tekton_util,
                        "_token_authenticated",
                        side_effect=token_auth_side_effect,
                    )
                )
                tekton_util.ensure_kubeconfig_bearer_token(env)
            self.assertEqual(
                tekton_util._kubeconfig_bearer_token(Path(env["KUBECONFIG"])),
                "fresh-exec-token",
            )

    def test_skips_bearer_when_htpasswd_pytest_login_active(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            kc = Path(tmp) / "kc"
            kc.write_text(
                self._kubeconfig_with_cluster_server(
                    [
                        "current-context: ctx",
                        "contexts:",
                        "  - name: ctx",
                        "    context:",
                        "      cluster: c",
                        "      user: u",
                        "users:",
                        "  - name: u",
                        "    user:",
                        "      token: htpasswd-session",
                    ]
                ),
                encoding="utf-8",
            )
            env = {
                "KUBECONFIG": str(kc),
                tekton_util.OLMINSTALL_HTPASSWD_KUBECONFIG_ENV: "1",
            }
            with mock.patch.object(tekton_util, "_resolve_bearer_token_from_kubeconfig") as resolve:
                tekton_util.ensure_kubeconfig_bearer_token(env)
            resolve.assert_not_called()
            self.assertEqual(
                tekton_util._kubeconfig_bearer_token(kc),
                "htpasswd-session",
            )

    def test_ensure_rhoai_e2e_sa_skips_bind_when_cluster_admin_ready(self) -> None:
        calls: list[list[str]] = []

        def run_side_effect(cmd, **kwargs):
            calls.append(list(cmd))
            if len(cmd) >= 3 and cmd[1:3] == ["get", "sa"]:
                return mock.Mock(returncode=0, stdout="", stderr="")
            if len(cmd) >= 4 and cmd[1:4] == ["auth", "can-i", "*"]:
                return mock.Mock(returncode=0, stdout="yes\n", stderr="")
            raise AssertionError(f"unexpected oc command: {cmd}")

        with mock.patch.object(tekton_util, "run", side_effect=run_side_effect):
            self.assertTrue(
                tekton_util._ensure_rhoai_e2e_cluster_admin_sa("/usr/bin/oc", {"KUBECONFIG": "/tmp/kc"})
            )
        self.assertFalse(
            any("adm" in cmd and "policy" in cmd for cmd in calls),
            "bind should be skipped when SA already has cluster-admin",
        )

