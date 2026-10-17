"""Gateway auth overlays and TEST_CLUSTERS merge for dashboard Cypress."""

from __future__ import annotations

import os
import sys
import time
from pathlib import Path

import install.ldap as _ldap
from suite.its_trigger_params import CLUSTER_SOURCE_EPHC


def _ensure_pyyaml_available() -> None:
    from components.dashboard_cypress.runtime import _ensure_pyyaml_available as _ensure

    _ensure()


def _yaml_scalar(value: str) -> str:
    """Quote YAML scalars that break plain parsing (URLs with ports, etc.)."""
    if not value:
        return '""'
    if any(ch in value for ch in (":", "#", '"', "'", "\n")) or value.strip() != value:
        return f'"{value.replace(chr(34), chr(92) + chr(34))}"'
    return value


def _deep_merge_dict(base: dict[str, object], overlay: dict[str, object]) -> dict[str, object]:
    out: dict[str, object] = dict(base)
    for key, val in overlay.items():
        existing = out.get(key)
        if isinstance(existing, dict) and isinstance(val, dict):
            out[key] = _deep_merge_dict(existing, val)
        else:
            out[key] = val
    return out


def _load_yaml_dict(path: Path) -> dict[str, object]:
    if not path.is_file():
        return {}
    _ensure_pyyaml_available()
    import yaml

    try:
        doc = yaml.safe_load(path.read_text(encoding="utf-8"))
    except OSError:
        return {}
    return doc if isinstance(doc, dict) else {}


def _is_rosa_hcp_dashboard_url(dashboard_url: str) -> bool:
    """True for personal ROSA HCP rh-ai routes (``*.apps.rosa.<cluster>.*``)."""
    return ".apps.rosa." in (dashboard_url or "").strip()


def _htpasswd_test_user_from_vault(doc: dict[str, object]) -> dict[str, str] | None:
    """Jenkins ROSA HCP: htpasswd-cluster-admin user from pooled QE template (ods-qe-01)."""
    clusters = doc.get("TEST_CLUSTERS")
    if not isinstance(clusters, dict):
        return None
    entry = clusters.get("ods-qe-01")
    if not isinstance(entry, dict):
        return None
    admin = entry.get("OCP_ADMIN_USER")
    if not isinstance(admin, dict):
        return None
    auth_type = str(admin.get("AUTH_TYPE") or "")
    if not auth_type.startswith("htpasswd"):
        return None
    out: dict[str, str] = {}
    for key in ("AUTH_TYPE", "USERNAME", "PASSWORD"):
        val = str(admin.get(key) or "").strip()
        if val:
            out[key] = val
    return out or None


def _ldap_style_username(username: str) -> bool:
    user = username.strip().lower()
    return user.startswith("ldap-") or user.startswith("ldap_")


def _htpasswd_test_user_from_env() -> dict[str, str] | None:
    username = (
        os.environ.get("HTPASSWD_CLUSTER_ADMIN_USER", "").strip()
        or os.environ.get("OCP_ADMIN_USER_USERNAME", "").strip()
    )
    if not username:
        test_user = os.environ.get("TEST_USER_USERNAME", "").strip()
        if test_user and not _ldap_style_username(test_user):
            username = test_user
    password = (
        os.environ.get("HTPASSWD_CLUSTER_PASSWORD", "").strip()
        or os.environ.get("OCP_ADMIN_USER_PASSWORD", "").strip()
        or os.environ.get("TEST_USER_PASSWORD", "").strip()
        or os.environ.get("CY_PASSWORD", "").strip()
    )
    idp = os.environ.get("HTPASSWD_IDP_NAME", "htpasswd-cluster-admin").strip()
    if not username or not password:
        return None
    return {"AUTH_TYPE": idp, "USERNAME": username, "PASSWORD": password}


def _cluster_source_is_ephc(*, odh_dashboard_url: str = "") -> bool:
    if os.environ.get("CLUSTER_SOURCE", "").strip() == CLUSTER_SOURCE_EPHC:
        return True
    url = (
        odh_dashboard_url
        or os.environ.get("ODH_DASHBOARD_URL", "").strip()
        or os.environ.get("BASE_URL", "").strip()
    )
    host = url.lower()
    return "konflux-ocp-ci.dev" in host


def _ephc_vault_ldap_test_user(vault_doc: dict[str, object]) -> dict[str, str] | None:
    """LDAP TEST_USER from vault YAML or flat env (Jenkins createIDP / MaaS EPHC parity)."""
    top = vault_doc.get("TEST_USER")
    if isinstance(top, dict):
        auth_type = str(top.get("AUTH_TYPE") or "ldap").strip()
        if auth_type.lower().startswith("ldap"):
            user = str(top.get("USERNAME") or "").strip()
            pwd = str(top.get("PASSWORD") or "").strip()
            if user and pwd:
                return {"AUTH_TYPE": auth_type, "USERNAME": user, "PASSWORD": pwd}
    user = os.environ.get("TEST_USER_USERNAME", "").strip()
    pwd = os.environ.get("TEST_USER_PASSWORD", "").strip()
    auth_type = os.environ.get("TEST_USER_AUTH_TYPE", "").strip() or "ldap"
    if user and pwd and _ldap_style_username(user):
        return {"AUTH_TYPE": auth_type, "USERNAME": user, "PASSWORD": pwd}
    return None


def _ephc_gateway_auth_overlay(
    vault_doc: dict[str, object],
    cluster_label: str,
    *,
    odh_dashboard_url: str,
) -> dict[str, object]:
    """Konflux EPHC after Jenkins createIDP: vault htpasswd or LDAP (no OAuth IdP on HCP)."""
    if not _cluster_source_is_ephc(odh_dashboard_url=odh_dashboard_url) or _ldap._cluster_is_byoidc():
        return {}
    if _ldap.cluster_has_htpasswd_identity():
        return {}
    if not _ldap._openldap_secret_ready():
        return {}

    htpasswd_user = _htpasswd_test_user_from_env() or _htpasswd_test_user_from_vault(vault_doc)
    if htpasswd_user and str(htpasswd_user.get("USERNAME") or "").lower().startswith("htpasswd-"):
        idp = str(htpasswd_user.get("AUTH_TYPE") or "htpasswd-cluster-admin").strip()
        overlay: dict[str, object] = {
            "CLUSTER_AUTH": idp,
            "TEST_USER": htpasswd_user,
            "OCP_ADMIN_USER": htpasswd_user,
        }
        entry = _test_clusters_entry(vault_doc, cluster_label) if cluster_label else {}
        _patch_secondary_users_for_htpasswd(overlay, vault_doc, entry, htpasswd_user)
        return overlay

    ldap_user = _ephc_vault_ldap_test_user(vault_doc)
    if ldap_user:
        return {
            "CLUSTER_AUTH": "",
            "TEST_USER": ldap_user,
            "OCP_ADMIN_USER": ldap_user,
        }
    return {}


def _user_needs_htpasswd_auth_patch(user: object) -> bool:
    if not isinstance(user, dict):
        return False
    auth = str(user.get("AUTH_TYPE") or "").strip().lower()
    return auth.startswith("oidc") or auth.startswith("ldap")


def _patch_secondary_users_for_htpasswd(
    overlay: dict[str, object],
    vault_doc: dict[str, object],
    entry: dict[str, object],
    htpasswd_user: dict[str, str],
) -> None:
    """Map TEST_USER_3/5 (LDAP Cypress users) to htpasswd when vault still says OIDC/LDAP."""
    sources = (entry, vault_doc) if entry else (vault_doc,)
    for key in ("TEST_USER_3", "TEST_USER_5"):
        if key in overlay:
            continue
        for src in sources:
            user = src.get(key)
            if _user_needs_htpasswd_auth_patch(user):
                overlay[key] = dict(htpasswd_user)
                break


def _ldap_test_user_from_cluster_entries(
    vault_doc: dict[str, object],
    cluster_label: str,
) -> tuple[dict[str, object], dict[str, object]]:
    """LDAP TEST_USER from *cluster_label* or pooled ``ods-qe-01`` template."""
    clusters = vault_doc.get("TEST_CLUSTERS")
    if not isinstance(clusters, dict):
        return {}, {}
    labels = [cluster_label]
    if cluster_label.startswith(("ods-qe-psi-", "ods-qe-")) and "ods-qe-01" not in labels:
        labels.append("ods-qe-01")
    for label in labels:
        entry = clusters.get(label)
        if not isinstance(entry, dict):
            continue
        test_user = entry.get("TEST_USER")
        if not isinstance(test_user, dict):
            continue
        auth_type = str(test_user.get("AUTH_TYPE") or "").lower()
        if auth_type.startswith("ldap"):
            return dict(test_user), dict(entry)
    return {}, {}


def _pooled_psi_ldap_auth_overlay(
    vault_doc: dict[str, object],
    cluster_label: str,
) -> dict[str, object]:
    """Pooled PSI Cypress: vault LDAP TEST_USER through gateway (psi-07 ``7w97n`` parity)."""
    if not cluster_label.startswith("ods-qe-psi-"):
        return {}
    test_user, entry = _ldap_test_user_from_cluster_entries(vault_doc, cluster_label)
    if not test_user:
        return {}
    overlay: dict[str, object] = {
        "CLUSTER_AUTH": "",
        "TEST_USER": test_user,
    }
    admin_user = entry.get("OCP_ADMIN_USER")
    if isinstance(admin_user, dict):
        overlay["OCP_ADMIN_USER"] = admin_user
    return overlay


def _byoidc_cypress_poll_settings() -> tuple[int, float]:
    """EPHC BYOIDC credentials can lag gateway Ready (gateway_config waits up to ~6m)."""
    if os.environ.get("CLUSTER_SOURCE", "").strip() == CLUSTER_SOURCE_EPHC:
        return 24, 15.0
    return 6, 5.0


def _resolve_byoidc_cypress_test_user(
    *,
    retries: int | None = None,
    delay_sec: float | None = None,
) -> dict[str, str] | None:
    """Poll ``oidc/byoidc-credentials`` — EPHC clusters may expose the secret shortly after gateway prep."""
    from components.maas_billing.oidc_users import byoidc_cypress_test_user

    default_retries, default_delay = _byoidc_cypress_poll_settings()
    retries = default_retries if retries is None else retries
    delay_sec = default_delay if delay_sec is None else delay_sec
    for attempt in range(retries):
        user = byoidc_cypress_test_user()
        if user:
            return user
        if attempt + 1 < retries:
            time.sleep(delay_sec)
    return None


def resolve_gateway_auth_overlay(
    vault_path: Path,
    cluster_label: str,
    *,
    odh_dashboard_url: str,
) -> dict[str, object]:
    """When vault defaults to OIDC but the cluster is htpasswd HCP, use htpasswd TEST_USER."""
    if dashboard_url_is_local(odh_dashboard_url):
        return {}
    if gateway_cypress_uses_bearer_bypass(odh_dashboard_url=odh_dashboard_url):
        return {}
    if _ldap._cluster_is_byoidc():
        user = _resolve_byoidc_cypress_test_user()
        if user:
            return {
                "CLUSTER_AUTH": "oidc",
                "TEST_USER": user,
                "OCP_ADMIN_USER": user,
            }
        print(
            "WARN: BYOIDC cluster missing oidc/byoidc-credentials Cypress test user",
            file=sys.stderr,
            flush=True,
        )
        return {}
    vault_doc = _load_yaml_dict(vault_path)
    entry = _test_clusters_entry(vault_doc, cluster_label) if cluster_label else {}
    if entry:
        test_user = entry.get("TEST_USER")
        if isinstance(test_user, dict) and not str(test_user.get("AUTH_TYPE") or "").startswith("oidc"):
            admin_user = entry.get("OCP_ADMIN_USER")
            admin_auth = (
                str(admin_user.get("AUTH_TYPE") or "")
                if isinstance(admin_user, dict)
                else ""
            )
            if admin_auth and not admin_auth.startswith("oidc"):
                test_auth = str(test_user.get("AUTH_TYPE") or "").lower()
                if (
                    test_auth.startswith("ldap")
                    and admin_auth.lower().startswith("htpasswd")
                    and isinstance(admin_user, dict)
                ):
                    htpasswd_user = {
                        k: str(admin_user.get(k) or "").strip()
                        for k in ("AUTH_TYPE", "USERNAME", "PASSWORD")
                    }
                    overlay: dict[str, object] = {
                        "CLUSTER_AUTH": admin_auth,
                        "TEST_USER": htpasswd_user,
                        "OCP_ADMIN_USER": htpasswd_user,
                    }
                    _patch_secondary_users_for_htpasswd(
                        overlay, vault_doc, entry, htpasswd_user
                    )
                    return overlay
            idp = str(test_user.get("AUTH_TYPE") or "htpasswd-cluster-admin").strip()
            htpasswd_user = {k: str(test_user.get(k) or "").strip() for k in ("AUTH_TYPE", "USERNAME", "PASSWORD")}
            htpasswd_user["AUTH_TYPE"] = idp
            overlay: dict[str, object] = {
                "CLUSTER_AUTH": idp,
                "TEST_USER": test_user,
                "OCP_ADMIN_USER": test_user,
            }
            _patch_secondary_users_for_htpasswd(overlay, vault_doc, entry, htpasswd_user)
            return overlay
    cluster_auth = str(vault_doc.get("CLUSTER_AUTH") or "")
    top_user = vault_doc.get("TEST_USER")
    uses_oidc = cluster_auth == "oidc" or (
        isinstance(top_user, dict) and str(top_user.get("AUTH_TYPE") or "").startswith("oidc")
    )
    if not uses_oidc:
        return _ephc_gateway_auth_overlay(vault_doc, cluster_label, odh_dashboard_url=odh_dashboard_url)
    if gateway_use_byoidc_auth(odh_dashboard_url=odh_dashboard_url):
        return {}
    htpasswd_user = _htpasswd_test_user_from_env()
    if not htpasswd_user and (
        _ldap.cluster_has_htpasswd_identity()
        or (cluster_label or "").startswith("ods-qe")
        or _is_rosa_hcp_dashboard_url(odh_dashboard_url)
        or (
            _cluster_source_is_ephc(odh_dashboard_url=odh_dashboard_url)
            and _ldap._openldap_secret_ready()
        )
    ):
        htpasswd_user = _htpasswd_test_user_from_vault(vault_doc)
    if not htpasswd_user:
        return _ephc_gateway_auth_overlay(
            vault_doc, cluster_label, odh_dashboard_url=odh_dashboard_url
        )
    idp = str(htpasswd_user.get("AUTH_TYPE") or "htpasswd-cluster-admin").strip()
    overlay = {
        "CLUSTER_AUTH": idp,
        "TEST_USER": htpasswd_user,
        "OCP_ADMIN_USER": htpasswd_user,
    }
    _patch_secondary_users_for_htpasswd(overlay, vault_doc, entry, htpasswd_user)
    return overlay


def _clear_runtime_oidc_auth(runtime_cfg: Path) -> None:
    """Drop vault OIDC defaults when the cluster is not BYOIDC (jc5hq htpasswd path)."""
    doc = _load_yaml_dict(runtime_cfg)
    if not doc or _ldap._cluster_is_byoidc():
        return
    changed = False
    if str(doc.get("CLUSTER_AUTH") or "").strip().lower() == "oidc":
        doc["CLUSTER_AUTH"] = ""
        changed = True
    test_user = doc.get("TEST_USER")
    if isinstance(test_user, dict) and str(test_user.get("AUTH_TYPE") or "").startswith("oidc"):
        doc.pop("TEST_USER", None)
        doc.pop("OCP_ADMIN_USER", None)
        changed = True
    if not changed:
        return
    _ensure_pyyaml_available()
    import yaml

    runtime_cfg.write_text(
        yaml.safe_dump(doc, default_flow_style=False, sort_keys=False),
        encoding="utf-8",
    )
    print(
        "✓ Cleared vault OIDC defaults from Cypress runtime (cluster is not BYOIDC)",
        flush=True,
    )


def _apply_gateway_auth_overlay(
    runtime_cfg: Path,
    vault_src: Path,
    *,
    cluster_label: str,
    odh_dashboard_url: str,
) -> None:
    overlay = resolve_gateway_auth_overlay(
        vault_src,
        cluster_label,
        odh_dashboard_url=odh_dashboard_url,
    )
    if not overlay:
        if not _ldap._cluster_is_byoidc():
            _clear_runtime_oidc_auth(runtime_cfg)
        return
    doc_before = _load_yaml_dict(runtime_cfg)
    existing_user = doc_before.get("TEST_USER")
    if isinstance(existing_user, dict) and not _ldap._cluster_is_byoidc():
        existing_auth = str(existing_user.get("AUTH_TYPE") or "").lower()
        overlay_user = overlay.get("TEST_USER")
        if existing_auth.startswith("ldap") and isinstance(overlay_user, dict):
            overlay_auth = str(overlay_user.get("AUTH_TYPE") or "").lower()
            if overlay_auth.startswith("oidc"):
                overlay = {
                    key: val
                    for key, val in overlay.items()
                    if key not in ("TEST_USER", "OCP_ADMIN_USER")
                }
                if str(overlay.get("CLUSTER_AUTH") or "") == "oidc":
                    overlay["CLUSTER_AUTH"] = ""
    if not overlay:
        if not _ldap._cluster_is_byoidc():
            _clear_runtime_oidc_auth(runtime_cfg)
        return
    _ensure_pyyaml_available()
    import yaml

    doc = _load_yaml_dict(runtime_cfg)
    merged = _deep_merge_dict(doc, overlay)
    runtime_cfg.write_text(
        yaml.safe_dump(merged, default_flow_style=False, sort_keys=False),
        encoding="utf-8",
    )
    test_user = merged.get("TEST_USER")
    auth = test_user.get("AUTH_TYPE") if isinstance(test_user, dict) else "?"
    cluster_auth = merged.get("CLUSTER_AUTH", "?")
    print(
        f"✓ Gateway Cypress auth overlay for {cluster_label or 'cluster'}: "
        f"CLUSTER_AUTH={cluster_auth}, TEST_USER AUTH_TYPE={auth}",
        flush=True,
    )


def _htpasswd_idp_from_test_user(test_user: dict[str, object] | None) -> str:
    if not isinstance(test_user, dict):
        return ""
    auth_type = str(test_user.get("AUTH_TYPE") or "").strip()
    if auth_type and not auth_type.startswith("oidc"):
        return auth_type
    return ""


def validate_gateway_cypress_auth(*, odh_dashboard_url: str) -> int | None:
    """Fail fast when gateway Cypress would use OIDC on a non-BYOIDC cluster."""
    if dashboard_url_is_local(odh_dashboard_url):
        return None
    if gateway_cypress_uses_bearer_bypass(odh_dashboard_url=odh_dashboard_url):
        token = str(
            os.environ.get("OC_TOKEN") or os.environ.get("CYPRESS_OC_TOKEN") or ""
        ).strip()
        if not token:
            print(
                "ERROR: Konflux EPHC gateway Cypress requires OC bearer token "
                "(HostedCluster VAP blocks OAuth htpasswd/LDAP IdP)",
                file=sys.stderr,
            )
            return 2
        return None
    if _ldap._cluster_is_byoidc():
        auth_type = str(os.environ.get("TEST_USER_AUTH_TYPE") or "")
        username = str(os.environ.get("TEST_USER_USERNAME") or "")
        if auth_type.startswith("oidc") and not username:
            print(
                "ERROR: BYOIDC cluster but no Cypress test user (oidc/byoidc-credentials)",
                file=sys.stderr,
            )
            return 2
        return None
    auth_type = str(os.environ.get("TEST_USER_AUTH_TYPE") or "")
    cluster_auth = str(os.environ.get("CLUSTER_AUTH") or "").strip().lower()
    if auth_type.startswith("oidc") or cluster_auth == "oidc":
        print(
            "ERROR: Cypress auth is OIDC but cluster is not BYOIDC "
            "(check gateway auth overlay / vault CLUSTER_AUTH)",
            file=sys.stderr,
        )
        return 2
    if not auth_type and not cluster_auth and not _ldap.cluster_has_htpasswd_identity():
        if (
            _cluster_source_is_ephc(odh_dashboard_url=odh_dashboard_url)
            and _ldap._openldap_secret_ready()
            and os.environ.get("TEST_USER_USERNAME", "").strip()
        ):
            return None
        print(
            "ERROR: gateway Cypress requires BYOIDC credentials or htpasswd IdP on cluster",
            file=sys.stderr,
        )
        return 2
    return None


def sync_cypress_auth_env_from_config(config_path: str | Path) -> None:
    """Flatten CY_TEST_CONFIG auth fields into process env for Cypress --env and shell exports."""
    doc = _load_yaml_dict(Path(config_path))
    if not doc:
        return
    test_user = doc.get("TEST_USER")
    if isinstance(test_user, dict):
        for src, dst in (
            ("USERNAME", "TEST_USER_USERNAME"),
            ("PASSWORD", "TEST_USER_PASSWORD"),
            ("AUTH_TYPE", "TEST_USER_AUTH_TYPE"),
        ):
            val = str(test_user.get(src) or "").strip()
            if val:
                os.environ[dst] = val
    if "CLUSTER_AUTH" in doc:
        os.environ["CLUSTER_AUTH"] = str(doc.get("CLUSTER_AUTH") or "")
    elif not gateway_use_byoidc_auth(
        odh_dashboard_url=str(doc.get("ODH_DASHBOARD_URL") or os.environ.get("ODH_DASHBOARD_URL") or "")
    ):
        htpasswd_idp = _htpasswd_idp_from_test_user(
            test_user if isinstance(test_user, dict) else None
        )
        os.environ["CLUSTER_AUTH"] = htpasswd_idp or os.environ.get(
            "HTPASSWD_IDP_NAME", "htpasswd-cluster-admin"
        )


def _aws_pipelines_overlay_from_env() -> dict[str, object] | None:
    """Build Jenkins-style AWS_PIPELINES for Cypress from shift-left / vault env."""
    from k8s.shift_left_env import resolve_ci_s3_smoke_fields

    fields = resolve_ci_s3_smoke_fields()
    if not fields:
        return None
    bucket_details = {
        "NAME": fields["NAME"],
        "REGION": fields["REGION"],
        "ENDPOINT": fields["ENDPOINT"],
    }
    # Mirrors vault CY_TEST_CONFIG AWS_PIPELINES (Jenkins dashboard-e2e parity).
    return {
        "AWS_PIPELINES": {
            "AWS_ACCESS_KEY_ID": fields["AWS_ACCESS_KEY_ID"],
            "AWS_SECRET_ACCESS_KEY": fields["AWS_SECRET_ACCESS_KEY"],
            "BUCKET_2": dict(bucket_details),
            "BUCKET_3": dict(bucket_details),
        }
    }


def _merge_cypress_s3_overlay(runtime_cfg: Path, vault_src: Path) -> None:
    """Ensure CY_TEST_CONFIG exposes AWS_PIPELINES for AutoML/pipeline Cypress specs."""
    doc = _load_yaml_dict(runtime_cfg)
    if isinstance(doc.get("AWS_PIPELINES"), dict):
        return
    vault_doc = _load_yaml_dict(vault_src)
    pipelines = vault_doc.get("AWS_PIPELINES")
    if isinstance(pipelines, dict):
        overlay: dict[str, object] = {"AWS_PIPELINES": pipelines}
    else:
        built = _aws_pipelines_overlay_from_env()
        if not built:
            print(
                "WARN: skipping AWS_PIPELINES overlay; vault/env S3 fields incomplete",
                file=sys.stderr,
                flush=True,
            )
            return
        overlay = built
    _ensure_pyyaml_available()
    import yaml

    merged = _deep_merge_dict(doc, overlay)
    runtime_cfg.write_text(
        yaml.safe_dump(merged, default_flow_style=False, sort_keys=False),
        encoding="utf-8",
    )
    print("✓ Merged AWS_PIPELINES into Cypress runtime config", flush=True)


def _test_clusters_entry(doc: dict[str, object], cluster_label: str) -> dict[str, object]:
    """Resolve Jenkins TEST_CLUSTERS block for *cluster_label* (pooled PSI → ods-qe-01)."""
    clusters = doc.get("TEST_CLUSTERS")
    if not isinstance(clusters, dict):
        return {}
    entry = clusters.get(cluster_label)
    if isinstance(entry, dict):
        result = dict(entry)
        if cluster_label.startswith(("ods-qe-psi-", "ods-qe-")):
            fallback = clusters.get("ods-qe-01")
            if isinstance(fallback, dict):
                test_user = result.get("TEST_USER")
                test_auth = (
                    str(test_user.get("AUTH_TYPE") or "").lower()
                    if isinstance(test_user, dict)
                    else ""
                )
                if not test_auth.startswith("ldap"):
                    fb_user = fallback.get("TEST_USER")
                    if isinstance(fb_user, dict) and str(
                        fb_user.get("AUTH_TYPE") or ""
                    ).lower().startswith("ldap"):
                        result["TEST_USER"] = dict(fb_user)
                for key in ("OCP_ADMIN_USER", "OCP_CONSOLE_URL", "ODH_DASHBOARD_URL"):
                    if key not in result and key in fallback:
                        result[key] = fallback[key]
        return result
    if cluster_label.startswith(("ods-qe-psi-", "ods-qe-")):
        fallback = clusters.get("ods-qe-01")
        if isinstance(fallback, dict):
            return dict(fallback)
    return {}


def resolve_test_clusters_overlay(vault_path: Path, cluster_label: str) -> dict[str, object]:
    """Merge per-cluster TEST_USER / URLs from vault TEST_CLUSTERS (Jenkins parity)."""
    if not cluster_label or not vault_path.is_file():
        return {}
    _ensure_pyyaml_available()
    import yaml

    try:
        doc = yaml.safe_load(vault_path.read_text(encoding="utf-8"))
    except OSError:
        return {}
    if not isinstance(doc, dict):
        return {}
    entry = _test_clusters_entry(doc, cluster_label)
    if not entry:
        return {}
    overlay: dict[str, object] = {}
    for key in ("TEST_USER", "OCP_ADMIN_USER", "OCP_CONSOLE_URL", "ODH_DASHBOARD_URL"):
        if key in entry:
            overlay[key] = entry[key]
    test_user = overlay.get("TEST_USER")
    if isinstance(test_user, dict):
        auth_type = str(test_user.get("AUTH_TYPE") or "")
        if auth_type.startswith("ldap"):
            overlay["CLUSTER_AUTH"] = ""
    return overlay


def _merge_test_clusters_into_runtime_config(
    runtime_cfg: Path,
    vault_src: Path,
    cluster_label: str,
) -> None:
    overlay = resolve_test_clusters_overlay(vault_src, cluster_label)
    if not overlay:
        return
    _ensure_pyyaml_available()
    import yaml

    try:
        doc = yaml.safe_load(runtime_cfg.read_text(encoding="utf-8"))
    except OSError:
        return
    if not isinstance(doc, dict):
        doc = {}
    merged = _deep_merge_dict(doc, overlay)
    runtime_cfg.write_text(
        yaml.safe_dump(merged, default_flow_style=False, sort_keys=False),
        encoding="utf-8",
    )
    print(
        f"✓ Merged TEST_CLUSTERS overlay for {cluster_label} into {runtime_cfg.name}",
        flush=True,
    )


def dashboard_url_is_local(url: str) -> bool:
    return bool(url) and ("127.0.0.1" in url or "localhost" in url)


def is_konflux_oci_ephemeral_gateway_url(url: str) -> bool:
    """True for OpenShift CI HyperShift guests (``*.prod.konflux-ocp-ci.dev``)."""
    return "konflux-ocp-ci.dev" in url.lower()


def gateway_cypress_uses_bearer_bypass(*, odh_dashboard_url: str) -> bool:
    """Local port-forward or EPHC HCP without OAuth login IdP: kube bearer + ci-auth-bypass.

    createIDP may stage ``openldap/openldap`` on HostedCluster EPHC, but VAP still blocks
    OAuth identityProvider patches — vault htpasswd browser login then fails (401). Only
    leave bearer when LDAP/htpasswd is actually registered on cluster OAuth.

    OpenShift CI guests (``konflux-ocp-ci.dev``) still send Electron to hosted-mgmt2
    ``/login`` when htpasswd overlay is set (ctcml 401). Always use kube bearer there.
    """
    if dashboard_url_is_local(odh_dashboard_url):
        return True
    if _ldap._cluster_is_byoidc():
        return False
    if is_konflux_oci_ephemeral_gateway_url(odh_dashboard_url):
        return True
    return False


def gateway_use_byoidc_auth(*, odh_dashboard_url: str) -> bool:
    """Gateway Cypress uses Keycloak/OIDC only when the cluster is actually BYOIDC."""
    if dashboard_url_is_local(odh_dashboard_url):
        return False
    return _ldap._cluster_is_byoidc()

