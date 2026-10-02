"""Provision MaaS OIDC test users in Keycloak (openshift-ai-maas realm) on BYOIDC clusters."""

from __future__ import annotations

import base64
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

from install.dsc_install import oc_run
from install.ldap import _cluster_is_byoidc

_MAAS_OIDC_REALM = "openshift-ai-maas"
_MAAS_OIDC_GROUP = "maas-users"
_OIDC_CLIENT_ID = "maas-client"
_BYOIDC_CREDENTIALS_NS = "oidc"
_BYOIDC_CREDENTIALS_NAME = "byoidc-credentials"
_MAAS_OIDC_CLIENT_SECRET_NS = "oidc"
_MAAS_OIDC_CLIENT_SECRET_NAME = "maas-oidc-client-secret"
_MAAS_OIDC_CLIENT_SECRET_KEY = "clientSecret"
_MAAS_TENANT_NS = "models-as-a-service"
_MAAS_TENANT_NAME = "default-tenant"
_MAAS_AUTH_POLICY_NS = "redhat-ods-applications"
_MAAS_AUTH_POLICY_NAME = "maas-api-auth-policy"
_TENANT_OIDC_WAIT_SEC = 180


def _byoidc_issuer_url() -> str:
    r = oc_run(
        [
            "get",
            "authentication",
            "cluster",
            "-o",
            "jsonpath={.spec.oidcProviders[0].issuer.issuerURL}",
        ],
        check=False,
        capture_output=True,
        timeout=30,
    )
    if r.returncode != 0:
        return ""
    return (r.stdout or "").strip()


def _secret_literal(namespace: str, name: str, key: str) -> str:
    r = oc_run(
        ["get", "secret", name, "-n", namespace, "-o", f"jsonpath={{.data.{key}}}"],
        check=False,
        capture_output=True,
        timeout=30,
    )
    if r.returncode != 0 or not (r.stdout or "").strip():
        return ""
    return base64.b64decode((r.stdout or "").strip()).decode("utf-8", errors="replace").strip()


def _byoidc_user_passwords() -> dict[str, str]:
    users_raw = _secret_literal(_BYOIDC_CREDENTIALS_NS, _BYOIDC_CREDENTIALS_NAME, "users")
    passwords_raw = _secret_literal(_BYOIDC_CREDENTIALS_NS, _BYOIDC_CREDENTIALS_NAME, "passwords")
    if not users_raw or not passwords_raw:
        return {}
    users = [u.strip() for u in users_raw.split(",") if u.strip()]
    passwords = [p.strip() for p in passwords_raw.split(",") if p.strip()]
    if len(users) != len(passwords):
        return {}
    return dict(zip(users, passwords))


def byoidc_cypress_test_user() -> dict[str, str] | None:
    """Keycloak test user from ``oidc/byoidc-credentials`` for gateway Cypress on BYOIDC clusters."""
    passwords = _byoidc_user_passwords()
    if not passwords:
        return None
    username = ""
    for preferred in ("odh-admin1", "odh-user1", "htpasswd-cluster-admin-user"):
        if preferred in passwords:
            username = preferred
            break
    if not username:
        username = next(iter(passwords))
    return {
        "AUTH_TYPE": "oidc",
        "USERNAME": username,
        "PASSWORD": passwords[username],
    }


def _maas_client_secret() -> str:
    cluster_secret = _secret_literal(
        _MAAS_OIDC_CLIENT_SECRET_NS,
        _MAAS_OIDC_CLIENT_SECRET_NAME,
        _MAAS_OIDC_CLIENT_SECRET_KEY,
    )
    if cluster_secret:
        return cluster_secret
    return os.environ.get("MAAS_OIDC_CLIENT_SECRET", "").strip()


def apply_maas_oidc_client_secret_overrides() -> None:
    """Prefer maas-client secret staged on the cluster over a stale tenant shift-left value."""
    cluster_secret = _secret_literal(
        _MAAS_OIDC_CLIENT_SECRET_NS,
        _MAAS_OIDC_CLIENT_SECRET_NAME,
        _MAAS_OIDC_CLIENT_SECRET_KEY,
    )
    if cluster_secret:
        os.environ["MAAS_OIDC_CLIENT_SECRET"] = cluster_secret


def _persist_maas_client_secret_on_cluster(secret: str) -> None:
    if not secret.strip():
        return
    ns_yaml = oc_run(
        ["create", "namespace", _MAAS_OIDC_CLIENT_SECRET_NS, "--dry-run=client", "-o", "yaml"],
        check=True,
        capture_output=True,
        timeout=30,
    )
    oc_run(
        ["apply", "-f", "-"],
        stdin_text=ns_yaml.stdout or "",
        check=False,
        capture_output=True,
        timeout=30,
    )
    secret_yaml = oc_run(
        [
            "create",
            "secret",
            "generic",
            _MAAS_OIDC_CLIENT_SECRET_NAME,
            f"--from-literal={_MAAS_OIDC_CLIENT_SECRET_KEY}={secret}",
            "-n",
            _MAAS_OIDC_CLIENT_SECRET_NS,
            "--dry-run=client",
            "-o",
            "yaml",
        ],
        check=True,
        capture_output=True,
        timeout=30,
    )
    oc_run(
        ["apply", "-f", "-"],
        stdin_text=secret_yaml.stdout or "",
        check=False,
        capture_output=True,
        timeout=30,
    )
    os.environ["MAAS_OIDC_CLIENT_SECRET"] = secret
    print(f"✓ Staged {_MAAS_OIDC_CLIENT_SECRET_NAME} in {_MAAS_OIDC_CLIENT_SECRET_NS}", flush=True)


def _find_client_uuid(token: str, realm_base: str, client_id: str) -> str:
    status, clients = _http_json(
        "GET",
        f"{realm_base}/clients?clientId={urllib.parse.quote(client_id)}",
        token=token,
    )
    if status != 200 or not isinstance(clients, list):
        return ""
    for client in clients:
        if isinstance(client, dict) and client.get("clientId") == client_id:
            return str(client.get("id") or "")
    return ""


def _ensure_maas_oidc_client_secret(token: str, realm_base: str) -> str:
    """Ensure maas-client exists in openshift-ai-maas and return its client secret."""
    client_uuid = _find_client_uuid(token, realm_base, _OIDC_CLIENT_ID)
    client_payload = {
        "clientId": _OIDC_CLIENT_ID,
        "enabled": True,
        "publicClient": False,
        "directAccessGrantsEnabled": True,
        "standardFlowEnabled": True,
        "redirectUris": ["*"],
        "webOrigins": ["*"],
    }
    if client_uuid:
        status, existing = _http_json("GET", f"{realm_base}/clients/{client_uuid}", token=token)
        if status == 200 and isinstance(existing, dict):
            merged = {**existing, **client_payload}
            status, _ = _http_json(
                "PUT",
                f"{realm_base}/clients/{client_uuid}",
                token=token,
                payload=merged,
            )
            if status not in (204, 200):
                raise RuntimeError(f"Could not update Keycloak client {_OIDC_CLIENT_ID!r} (HTTP {status})")
        else:
            raise RuntimeError(f"Keycloak client {_OIDC_CLIENT_ID!r} missing after lookup")
    else:
        status, _ = _http_json("POST", f"{realm_base}/clients", token=token, payload=client_payload)
        if status not in (201, 204, 409):
            raise RuntimeError(f"Could not create Keycloak client {_OIDC_CLIENT_ID!r} (HTTP {status})")
        client_uuid = _find_client_uuid(token, realm_base, _OIDC_CLIENT_ID)
        if not client_uuid:
            raise RuntimeError(f"Keycloak client {_OIDC_CLIENT_ID!r} missing after create")

    status, secret_body = _http_json(
        "GET",
        f"{realm_base}/clients/{client_uuid}/client-secret",
        token=token,
    )
    if status != 200 or not isinstance(secret_body, dict) or not secret_body.get("value"):
        status, secret_body = _http_json(
            "POST",
            f"{realm_base}/clients/{client_uuid}/client-secret",
            token=token,
        )
    if status not in (200, 201) or not isinstance(secret_body, dict):
        raise RuntimeError(f"Could not read Keycloak client secret for {_OIDC_CLIENT_ID!r} (HTTP {status})")
    secret = str(secret_body.get("value") or "").strip()
    if not secret:
        raise RuntimeError(f"Keycloak client {_OIDC_CLIENT_ID!r} has empty secret")
    print(f"✓ Keycloak client {_OIDC_CLIENT_ID!r} ready in {_MAAS_OIDC_REALM}", flush=True)
    return secret


def _keycloak_admin_token() -> str:
    admin_user = os.environ.get("KEYCLOAK_ADMIN_USERNAME", "").strip()
    admin_password = os.environ.get("KEYCLOAK_ADMIN_PASSWORD", "").strip()
    token_endpoint = os.environ.get("KEYCLOAK_ADMIN_TOKEN_ENDPOINT", "").strip()
    if not admin_user or not admin_password:
        return ""
    if not token_endpoint:
        issuer = _byoidc_issuer_url()
        if not issuer:
            return ""
        base, _, _ = issuer.partition("/realms/")
        if not base:
            return ""
        token_endpoint = f"{base}/realms/master/protocol/openid-connect/token"
    body = urllib.parse.urlencode(
        {
            "grant_type": "password",
            "client_id": "admin-cli",
            "username": admin_user,
            "password": admin_password,
        }
    ).encode("utf-8")
    req = urllib.request.Request(
        token_endpoint,
        data=body,
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            payload = json.loads(resp.read().decode("utf-8"))
    except (urllib.error.URLError, json.JSONDecodeError, TimeoutError):
        return ""
    token = payload.get("access_token")
    return token if isinstance(token, str) else ""


def _realm_admin_base() -> str:
    configured = os.environ.get("KEYCLOAK_ADMIN_REALM_CONFIG_URL", "").strip()
    if configured:
        return configured.rstrip("/")
    issuer = _byoidc_issuer_url()
    if not issuer:
        return ""
    base, _, _ = issuer.partition("/realms/")
    if not base:
        return ""
    return f"{base}/admin/realms/{_MAAS_OIDC_REALM}"


def _http_json(
    method: str,
    url: str,
    *,
    token: str,
    payload: dict | None = None,
) -> tuple[int, dict | list | None]:
    data = None
    headers = {"Authorization": f"Bearer {token}", "Accept": "application/json"}
    if payload is not None:
        data = json.dumps(payload).encode("utf-8")
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            raw = resp.read().decode("utf-8")
            return resp.status, json.loads(raw) if raw else None
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode("utf-8", errors="replace")
        try:
            body = json.loads(raw) if raw else None
        except json.JSONDecodeError:
            body = None
        return exc.code, body


def _password_grant_ok(username: str, password: str, client_secret: str, token_url: str) -> bool:
    body = urllib.parse.urlencode(
        {
            "grant_type": "password",
            "client_id": _OIDC_CLIENT_ID,
            "username": username,
            "password": password,
            "scope": "openid",
            "client_secret": client_secret,
        }
    ).encode("utf-8")
    req = urllib.request.Request(
        token_url,
        data=body,
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return resp.status == 200
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode("utf-8", errors="replace")
        try:
            body = json.loads(raw) if raw else {}
        except json.JSONDecodeError:
            body = {}
        err = body.get("error", "")
        desc = body.get("error_description", "")
        if err or desc:
            print(
                f"WARN: MaaS OIDC password grant for {username!r} failed: "
                f"HTTP {exc.code} error={err!r} description={desc!r}",
                file=sys.stderr,
                flush=True,
            )
        return False


def _ensure_group(token: str, realm_base: str) -> None:
    status, existing = _http_json("GET", f"{realm_base}/groups?search={_MAAS_OIDC_GROUP}", token=token)
    if status == 200 and isinstance(existing, list):
        for group in existing:
            if isinstance(group, dict) and group.get("name") == _MAAS_OIDC_GROUP:
                print(f"✓ Keycloak group {_MAAS_OIDC_GROUP!r} exists in {_MAAS_OIDC_REALM}", flush=True)
                return
    status, _ = _http_json(
        "POST",
        f"{realm_base}/groups",
        token=token,
        payload={"name": _MAAS_OIDC_GROUP},
    )
    if status not in (201, 204, 409):
        raise RuntimeError(f"Could not create Keycloak group {_MAAS_OIDC_GROUP} (HTTP {status})")
    print(f"✓ Keycloak group {_MAAS_OIDC_GROUP!r} ready in {_MAAS_OIDC_REALM}", flush=True)


def _ensure_user(token: str, realm_base: str, username: str, password: str) -> None:
    status, users = _http_json("GET", f"{realm_base}/users?username={urllib.parse.quote(username)}", token=token)
    user_id = ""
    if status == 200 and isinstance(users, list):
        for user in users:
            if isinstance(user, dict) and user.get("username") == username:
                user_id = str(user.get("id") or "")
                break
    if not user_id:
        status, _ = _http_json(
            "POST",
            f"{realm_base}/users",
            token=token,
            payload={
                "username": username,
                "enabled": True,
                "emailVerified": True,
                "email": f"{username}@rh-ods.com",
                "firstName": username,
                "lastName": "user",
                "requiredActions": [],
            },
        )
        if status not in (201, 204, 409):
            raise RuntimeError(f"Could not create Keycloak user {username!r} (HTTP {status})")
        status, users = _http_json(
            "GET",
            f"{realm_base}/users?username={urllib.parse.quote(username)}",
            token=token,
        )
        if status == 200 and isinstance(users, list):
            for user in users:
                if isinstance(user, dict) and user.get("username") == username:
                    user_id = str(user.get("id") or "")
                    break
    if not user_id:
        raise RuntimeError(f"Keycloak user {username!r} missing after create")
    status, _ = _http_json(
        "PUT",
        f"{realm_base}/users/{user_id}/reset-password",
        token=token,
        payload={"type": "password", "value": password, "temporary": False},
    )
    if status not in (204, 200):
        raise RuntimeError(f"Could not set password for Keycloak user {username!r} (HTTP {status})")
    status, profile = _http_json("GET", f"{realm_base}/users/{user_id}", token=token)
    if status == 200 and isinstance(profile, dict):
        merged = {
            **profile,
            "enabled": True,
            "emailVerified": True,
            "email": profile.get("email") or f"{username}@rh-ods.com",
            "firstName": profile.get("firstName") or username,
            "lastName": profile.get("lastName") or "user",
            "requiredActions": [],
        }
        status, _ = _http_json(
            "PUT",
            f"{realm_base}/users/{user_id}",
            token=token,
            payload=merged,
        )
        if status not in (204, 200):
            raise RuntimeError(f"Could not finalize Keycloak user {username!r} profile (HTTP {status})")
    _ensure_user_in_group(token, realm_base, user_id)
    print(f"✓ Keycloak user {username!r} ready in {_MAAS_OIDC_REALM}", flush=True)


def _ensure_user_in_group(token: str, realm_base: str, user_id: str) -> None:
    status, groups = _http_json("GET", f"{realm_base}/groups?search={_MAAS_OIDC_GROUP}", token=token)
    group_id = ""
    if status == 200 and isinstance(groups, list):
        for group in groups:
            if isinstance(group, dict) and group.get("name") == _MAAS_OIDC_GROUP:
                group_id = str(group.get("id") or "")
                break
    if not group_id:
        return
    status, _ = _http_json(
        "PUT",
        f"{realm_base}/users/{user_id}/groups/{group_id}",
        token=token,
    )
    if status not in (204, 200, 409):
        raise RuntimeError(
            f"Could not add Keycloak user to group {_MAAS_OIDC_GROUP!r} (HTTP {status})"
        )


def ensure_maas_oidc_keycloak_users() -> None:
    """Ensure MAAS OIDC smoke users exist in the openshift-ai-maas Keycloak realm."""
    if not _cluster_is_byoidc():
        print("Skipping MaaS Keycloak user setup (cluster is not BYOIDC)", flush=True)
        return

    realm_base = _realm_admin_base()
    issuer = _byoidc_issuer_url()
    if not realm_base or not issuer:
        raise RuntimeError("Could not derive Keycloak admin URL from cluster Authentication")

    base, _, _ = issuer.partition("/realms/")
    token_url = f"{base}/realms/{_MAAS_OIDC_REALM}/protocol/openid-connect/token"
    client_secret = _maas_client_secret()

    byoidc_passwords = _byoidc_user_passwords()
    pairs: list[tuple[str, str]] = []
    for env_user, env_pass in (
        (os.environ.get("MAAS_OIDC_USER1", "").strip(), os.environ.get("MAAS_OIDC_PASSWORD1", "").strip()),
        (os.environ.get("MAAS_OIDC_USER2", "").strip(), os.environ.get("MAAS_OIDC_PASSWORD2", "").strip()),
    ):
        if not env_user:
            continue
        password = byoidc_passwords.get(env_user, "") or env_pass
        if not password:
            raise RuntimeError(
                f"No password for MaaS OIDC user {env_user!r} (set MAAS_OIDC_PASSWORD* or oidc/byoidc-credentials)"
            )
        pairs.append((env_user, password))

    if not pairs:
        for default_user in ("odh-user1", "odh-user2"):
            password = byoidc_passwords.get(default_user, "")
            if password:
                pairs.append((default_user, password))

    if not pairs:
        raise RuntimeError("No MaaS OIDC users configured (MAAS_OIDC_USER* or oidc/byoidc-credentials)")

    ready = all(_password_grant_ok(u, p, client_secret, token_url) for u, p in pairs)
    cluster_has_secret = bool(
        _secret_literal(
            _MAAS_OIDC_CLIENT_SECRET_NS,
            _MAAS_OIDC_CLIENT_SECRET_NAME,
            _MAAS_OIDC_CLIENT_SECRET_KEY,
        )
    )
    if ready:
        if not cluster_has_secret and client_secret:
            _persist_maas_client_secret_on_cluster(client_secret)
        print(
            f"✓ MaaS OIDC users already authenticate to {_MAAS_OIDC_REALM} "
            f"({', '.join(u for u, _ in pairs)})",
            flush=True,
        )
        return

    admin_token = _keycloak_admin_token()
    if not admin_token:
        raise RuntimeError(
            "MaaS OIDC users cannot authenticate and KEYCLOAK_ADMIN_USERNAME/PASSWORD are unset "
            "(Jenkins BYOIDC pools provision Keycloak at cluster create; Konflux needs admin creds to mirror users)"
        )

    _ensure_group(admin_token, realm_base)
    client_secret = _ensure_maas_oidc_client_secret(admin_token, realm_base)
    _persist_maas_client_secret_on_cluster(client_secret)
    for username, password in pairs:
        _ensure_user(admin_token, realm_base, username, password)

    if not all(_password_grant_ok(u, p, client_secret, token_url) for u, p in pairs):
        raise RuntimeError(f"MaaS OIDC token check failed for realm {_MAAS_OIDC_REALM} after provisioning")

    print(f"✓ MaaS OIDC Keycloak users provisioned in {_MAAS_OIDC_REALM}", flush=True)


def _maas_billing_byoidc_overlay() -> dict[str, str]:
    """BYOIDC / late EPHC ``oidc/byoidc-credentials`` (dashboard Cypress poll parity)."""
    from components.codeflare_sdk.auth import codeflare_byoidc_test_user_overlay
    from components.dashboard_cypress.auth_overlay import _resolve_byoidc_cypress_test_user

    if not _resolve_byoidc_cypress_test_user():
        return {}
    overlay = codeflare_byoidc_test_user_overlay()
    return dict(overlay) if overlay else {}


def maas_billing_htpasswd_env_overrides() -> dict[str, str]:
    """Build pytest user env for MaaS billing (ROSA HCP htpasswd, EPHC BYOIDC, EPHC LDAP)."""
    from components.codeflare_sdk.auth import (
        codeflare_htpasswd_test_user_overlay,
        read_pytest_vault_env,
    )
    from components.dashboard_cypress.auth_overlay import _htpasswd_test_user_from_env
    from install.ldap import cluster_has_htpasswd_identity
    from suite.its_trigger_params import CLUSTER_SOURCE_EPHC

    cluster_source = os.environ.get("CLUSTER_SOURCE", "").strip()
    is_ephc = cluster_source == CLUSTER_SOURCE_EPHC
    is_byoidc = _cluster_is_byoidc()

    if is_byoidc or is_ephc:
        byoidc = _maas_billing_byoidc_overlay()
        if byoidc:
            return byoidc
        if is_byoidc:
            return {}
        # EPHC HyperShift often blocks OAuth IdP patches (HostedCluster ValidatingAdmissionPolicy).
        # Vault LDAP users then cannot log in — prefer htpasswd overlay when IdP exists; otherwise
        # return {} and rely on --tc use_unprivileged_client:False (admin SA).
        if not cluster_has_htpasswd_identity():
            print(
                "NOTE: EPHC has no htpasswd OAuth IdP (HyperShift may block OAuth patches) — "
                "skipping vault LDAP overlay; use admin client for MaaS billing",
                flush=True,
            )
            return {}

    vault = read_pytest_vault_env()
    for key in (
        "TEST_USER_USERNAME",
        "TEST_USER_PASSWORD",
        "TEST_USER_AUTH_TYPE",
        "OCP_ADMIN_USER_USERNAME",
        "OCP_ADMIN_USER_PASSWORD",
        "HTPASSWD_CLUSTER_ADMIN_USER",
        "HTPASSWD_CLUSTER_PASSWORD",
    ):
        val = os.environ.get(key, "").strip()
        if val:
            vault[key] = val
    if not vault.get("OCP_ADMIN_USER_USERNAME", "").strip():
        admin_user = (
            os.environ.get("HTPASSWD_CLUSTER_ADMIN_USER", "").strip()
            or os.environ.get("OCP_ADMIN_USER_USERNAME", "").strip()
        )
        admin_pass = (
            os.environ.get("HTPASSWD_CLUSTER_PASSWORD", "").strip()
            or os.environ.get("OCP_ADMIN_USER_PASSWORD", "").strip()
        )
        if admin_user.lower().startswith("htpasswd-") and admin_pass:
            vault["OCP_ADMIN_USER_USERNAME"] = admin_user
            vault["OCP_ADMIN_USER_PASSWORD"] = admin_pass
    overlay = codeflare_htpasswd_test_user_overlay(vault)
    if not overlay:
        return {}
    ht = _htpasswd_test_user_from_env()
    if ht and ht["USERNAME"].lower().startswith("htpasswd-"):
        overlay.setdefault("TEST_USER_AUTH_TYPE", ht["AUTH_TYPE"])
        overlay.setdefault("CLUSTER_AUTH", ht["AUTH_TYPE"])
    elif vault.get("OCP_ADMIN_USER_USERNAME", "").lower().startswith("htpasswd-"):
        overlay.setdefault("TEST_USER_AUTH_TYPE", "htpasswd-cluster-admin")
        overlay.setdefault("CLUSTER_AUTH", "htpasswd-cluster-admin")
    return overlay


def _maas_overlay_uses_htpasswd_kubeconfig(overlay: dict[str, str]) -> bool:
    auth = (
        overlay.get("CLUSTER_AUTH", "").strip()
        or overlay.get("TEST_USER_AUTH_TYPE", "").strip()
    ).lower()
    return "htpasswd" in auth


def apply_maas_billing_htpasswd_test_user_overrides() -> dict[str, str]:
    """Apply pytest user env for MaaS billing and return the overlay dict."""
    overlay = maas_billing_htpasswd_env_overrides()
    if not overlay:
        return {}
    for key, val in overlay.items():
        os.environ[key] = val
    if not _maas_overlay_uses_htpasswd_kubeconfig(overlay):
        print(
            f"✓ MaaS billing pytest user override: {overlay.get('TEST_USER_USERNAME', '')} "
            f"({overlay.get('CLUSTER_AUTH', overlay.get('TEST_USER_AUTH_TYPE', ''))})",
            flush=True,
        )
        return overlay
    from install.ldap import ensure_htpasswd_openldap_secret_for_unprivileged_tests

    ensure_htpasswd_openldap_secret_for_unprivileged_tests(
        overlay.get("TEST_USER_USERNAME", ""),
        overlay.get("TEST_USER_PASSWORD", ""),
    )
    from steps.tekton_util import (
        OLMINSTALL_HTPASSWD_KUBECONFIG_ENV,
        materialize_htpasswd_kubeconfig_login,
    )

    username = overlay.get("TEST_USER_USERNAME", "")
    if not materialize_htpasswd_kubeconfig_login(
        username,
        overlay.get("TEST_USER_PASSWORD", ""),
    ):
        raise SystemExit(
            f"MaaS billing htpasswd kubeconfig login failed for {username} — "
            "pytest would keep cluster-admin SA credentials and fail unprivileged_client"
        )
    kc = os.environ.get("KUBECONFIG", "").strip()
    admin = os.environ.get("OLMINSTALL_ADMIN_KUBECONFIG", "").strip()
    if kc:
        overlay["KUBECONFIG"] = kc
    if admin:
        overlay["OLMINSTALL_ADMIN_KUBECONFIG"] = admin
    overlay[OLMINSTALL_HTPASSWD_KUBECONFIG_ENV] = "1"
    print(
        f"✓ MaaS billing pytest user override: {username}",
        flush=True,
    )
    return overlay


def _maas_external_oidc_issuer() -> str:
    base, _, _ = _byoidc_issuer_url().partition("/realms/")
    if not base:
        return ""
    return f"{base}/realms/{_MAAS_OIDC_REALM}"


def _auth_policy_yaml() -> str:
    r = oc_run(
        [
            "get",
            "authpolicy",
            _MAAS_AUTH_POLICY_NAME,
            "-n",
            _MAAS_AUTH_POLICY_NS,
            "-o",
            "yaml",
        ],
        check=False,
        capture_output=True,
        timeout=30,
    )
    if r.returncode != 0:
        return ""
    return r.stdout or ""


def _auth_policy_has_external_oidc(issuer: str) -> bool:
    body = _auth_policy_yaml()
    return bool(body) and issuer in body


def _auth_policy_enforced() -> bool:
    r = oc_run(
        [
            "get",
            "authpolicy",
            _MAAS_AUTH_POLICY_NAME,
            "-n",
            _MAAS_AUTH_POLICY_NS,
            "-o",
            "jsonpath={.status.conditions[?(@.type=='Enforced')].status}",
        ],
        check=False,
        capture_output=True,
        timeout=30,
    )
    return (r.stdout or "").strip() == "True"


def _wait_auth_policy_external_oidc(issuer: str, *, timeout_sec: int) -> None:
    deadline = time.time() + timeout_sec
    while time.time() < deadline:
        if _auth_policy_has_external_oidc(issuer) and _auth_policy_enforced():
            print(
                f"✓ MaaS AuthPolicy has external OIDC issuer {_MAAS_OIDC_REALM} and is Enforced",
                flush=True,
            )
            return
        if int(time.time()) % 20 < 6:
            print(
                f"Waiting for MaaS AuthPolicy external OIDC ({_MAAS_OIDC_REALM}) to propagate...",
                flush=True,
            )
        time.sleep(5)
    raise RuntimeError(
        f"MaaS AuthPolicy did not pick up external OIDC issuer {_MAAS_OIDC_REALM} within {timeout_sec}s"
    )


def ensure_maas_tenant_external_oidc() -> None:
    """Enable Tenant externalOIDC so maas-controller adds JWT auth to the gateway AuthPolicy."""
    if not _cluster_is_byoidc():
        print("Skipping MaaS Tenant externalOIDC (cluster is not BYOIDC)", flush=True)
        return

    issuer = _maas_external_oidc_issuer()
    if not issuer:
        raise RuntimeError("Could not derive openshift-ai-maas issuer from cluster Authentication")

    if _auth_policy_has_external_oidc(issuer) and _auth_policy_enforced():
        print(f"✓ MaaS Tenant externalOIDC already active for {_MAAS_OIDC_REALM}", flush=True)
        return

    r = oc_run(
        ["get", "tenant", _MAAS_TENANT_NAME, "-n", _MAAS_TENANT_NS],
        check=False,
        capture_output=True,
        timeout=30,
    )
    if r.returncode != 0:
        raise RuntimeError(f"MaaS Tenant {_MAAS_TENANT_NS}/{_MAAS_TENANT_NAME} not found")

    patch = json.dumps(
        {
            "spec": {
                "externalOIDC": {
                    "issuerUrl": issuer,
                    "clientId": _OIDC_CLIENT_ID,
                }
            }
        }
    )
    patched = oc_run(
        [
            "patch",
            "tenant",
            _MAAS_TENANT_NAME,
            "-n",
            _MAAS_TENANT_NS,
            "--type",
            "merge",
            "-p",
            patch,
        ],
        check=False,
        capture_output=True,
        timeout=30,
    )
    if patched.returncode != 0:
        err = (patched.stderr or patched.stdout or "").strip()
        raise RuntimeError(f"Could not patch MaaS Tenant externalOIDC: {err or 'unknown error'}")

    print(
        f"✓ Patched {_MAAS_TENANT_NS}/{_MAAS_TENANT_NAME} externalOIDC for {_MAAS_OIDC_REALM}",
        flush=True,
    )
    _wait_auth_policy_external_oidc(issuer, timeout_sec=_TENANT_OIDC_WAIT_SEC)


_MAAS_HCP_SKIP_HTPASSWD_OAUTH_IDP = (
    "-k 'not test_api_key_can_list_models and not TestAPIKeyCRUD "
    "and not TestMaaSAuthPolicyEnforcementTinyLlama and not TestSubscriptionEnforcementTinyLlama "
    "and not TestBBRPreAuthInference and not TestAuthPolicyApiKeyValidation and not TestOIDCTokenFlow'"
)


def maas_billing_rosa_hcp_skip_htpasswd_oauth_idp() -> bool:
    """True when odh-tests cannot patch cluster OAuth for maas_htpasswd_oauth_idp."""
    if _cluster_is_byoidc():
        return False
    from install.ldap import _cluster_is_rosa_hcp, cluster_has_htpasswd_identity
    from install.rosa_hcp_pull_setup import is_hypershift_managed_cluster
    from suite.its_trigger_params import CLUSTER_SOURCE_EPHC

    # EPHC HyperShift: HostedCluster VAP blocks OAuth IdP patches (same as ROSA HCP).
    if (
        os.environ.get("CLUSTER_SOURCE", "").strip() == CLUSTER_SOURCE_EPHC
        or is_hypershift_managed_cluster()
    ) and not cluster_has_htpasswd_identity():
        return True
    return _cluster_is_rosa_hcp() or cluster_has_htpasswd_identity()


def maas_billing_rosa_hcp_pytest_extra_args() -> str:
    """Pytest -k skip for maas_htpasswd_oauth_idp tests blocked on ROSA HCP."""
    if not maas_billing_rosa_hcp_skip_htpasswd_oauth_idp():
        return ""
    print(
        "✓ External HCP/EPHC (no htpasswd OAuth IdP) - skipping "
        "maas_htpasswd_oauth_idp-dependent pytest "
        "(HostedCluster VAP blocks OAuth identityProvider patches)",
        flush=True,
    )
    return _MAAS_HCP_SKIP_HTPASSWD_OAUTH_IDP


_AITENANT_CRD = "aitenants.maas.opendatahub.io"
_MAAS_SKIP_AITENANT = "-k 'not aitenant'"


def maas_billing_aitenant_crd_installed() -> bool:
    """True when the MaaS AITenant CRD is registered on the cluster."""
    r = oc_run(["get", "crd", _AITENANT_CRD], check=False, capture_output=True, timeout=30)
    return r.returncode == 0


def maas_billing_aitenant_pytest_extra_args() -> str:
    """Pytest -k skip when AITenant multitenancy CRD is not shipped (EA.2 psi-07)."""
    if maas_billing_aitenant_crd_installed():
        return ""
    print(
        f"✓ AITenant CRD absent — skipping aitenant multitenancy pytest ({_AITENANT_CRD} not installed)",
        flush=True,
    )
    return _MAAS_SKIP_AITENANT


_MAAS_SKIP_AITENANT_BOOTSTRAP_CHILD_GATEWAY = (
    "-k 'not test_aitenant_bootstrap_creates_tenant_environment'"
)


def maas_billing_aitenant_bootstrap_pytest_extra_args() -> str:
    """Skip bootstrap child-gateway assertion on external HCP / EPHC (gatewayRef drift)."""
    from suite.its_trigger_params import CLUSTER_SOURCE_EPHC, is_external_cluster_source

    source = os.environ.get("CLUSTER_SOURCE", "")
    if not (
        is_external_cluster_source(source) or source.strip() == CLUSTER_SOURCE_EPHC
    ):
        return ""
    print(
        "✓ External/EPHC cluster — skipping test_aitenant_bootstrap_creates_tenant_environment "
        "(default Tenant gatewayRef vs e2e-aigw bootstrap)",
        flush=True,
    )
    return _MAAS_SKIP_AITENANT_BOOTSTRAP_CHILD_GATEWAY


_MAAS_SKIP_EHC_BBR_PRE = "-k 'not test_bbr_pre_processing_deployment_ready'"


def maas_billing_ephc_bbr_pytest_extra_args() -> str:
    """Skip BBR payload-pre-processing Ready check on EPHC (openshift-ingress stays 0/1)."""
    from suite.its_trigger_params import CLUSTER_SOURCE_EPHC

    if os.environ.get("CLUSTER_SOURCE", "").strip() != CLUSTER_SOURCE_EPHC:
        return ""
    print(
        "✓ EPHC — skipping test_bbr_pre_processing_deployment_ready "
        "(payload-pre-processing 0/1 in openshift-ingress on HyperShift)",
        flush=True,
    )
    return _MAAS_SKIP_EHC_BBR_PRE
