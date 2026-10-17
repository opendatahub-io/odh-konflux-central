"""CodeFlare SDK EPHC auth wrapper when HyperShift blocks htpasswd OAuth registration."""

from __future__ import annotations

import os
import shlex

from install.ldap import _cluster_is_byoidc, cluster_has_htpasswd_identity
from suite.its_trigger_params import CLUSTER_SOURCE_EPHC

_RUN_TESTS = "run-tests.sh"
_BYOIDC_INIT = "CLUSTER_IS_BYOIDC=false"
_EHC_BYOIDC_APPEND = (
    'if [ "${CLUSTER_AUTH:-}" = "openshift" ] && oc whoami >/dev/null 2>&1; then '
    'CLUSTER_IS_BYOIDC=true; export TEST_USER_USERNAME="${TEST_USER_USERNAME:-$(oc whoami)}"; '
    'echo "codeflare: EPHC CLUSTER_AUTH=openshift kubeconfig auth"; fi'
)


def codeflare_ephc_kubeconfig_run_prefix() -> str:
    """Unset vault legacy creds and oc-login with materialized bearer token before run-tests."""
    if os.environ.get("CLUSTER_SOURCE", "").strip() != CLUSTER_SOURCE_EPHC:
        return ""
    if _cluster_is_byoidc() or cluster_has_htpasswd_identity():
        return ""
    token = os.environ.get("OPENSHIFT_TOKEN") or os.environ.get("OC_TOKEN")
    if not token:
        return ""
    return (
        "unset OCP_ADMIN_USER_USERNAME OCP_ADMIN_USER_PASSWORD "
        "TEST_USER_USERNAME TEST_USER_PASSWORD; "
        f"export CLUSTER_AUTH=openshift OPENSHIFT_TOKEN={shlex.quote(token)} OC_TOKEN={shlex.quote(token)}; "
        'if [ -n "${OC_SERVER:-}" ] && [ -n "${OPENSHIFT_TOKEN:-}" ]; then '
        'oc login --token="$OPENSHIFT_TOKEN" --server="$OC_SERVER" --insecure-skip-tls-verify=true; '
        "fi; "
    )


def codeflare_ephc_run_tests_auth_patch_shell() -> str:
    """Patch bundled run-tests.sh so CLUSTER_AUTH=openshift uses byoidc kubeconfig path."""
    return (
        f'if [ -f {_RUN_TESTS} ] && grep -Fq {_BYOIDC_INIT!r} {_RUN_TESTS}; then '
        f"sed -i '/^CLUSTER_IS_BYOIDC=false$/a\\"
        f"{_EHC_BYOIDC_APPEND}' {_RUN_TESTS} && "
        f'echo "codeflare: patched {_RUN_TESTS} for EPHC openshift auth"; '
        "fi"
    )


def prepend_codeflare_ephc_kubeconfig_auth(run_command: str) -> str:
    cmd = (run_command or "").strip()
    if not cmd:
        return cmd
    prefix = codeflare_ephc_kubeconfig_run_prefix()
    if not prefix:
        return cmd
    patch = codeflare_ephc_run_tests_auth_patch_shell()
    return f"{prefix}{patch} && {cmd}"
