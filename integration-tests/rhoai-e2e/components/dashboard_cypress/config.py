"""Resolve dashboard Cypress cluster settings from the external OpenShift cluster."""

from __future__ import annotations

import json
import os
import re
import shutil
import sys
import xml.etree.ElementTree as ET
from collections.abc import Sequence
from pathlib import Path

from install.dsc_install import oc_run
from suite.component_catalog_models import (CypressParallelSet,
                                            CypressRunnerConfig)

_DASHBOARD_NS = "redhat-ods-applications"
_ROUTE_CANDIDATES = ("rhods-dashboard", "data-science-gateway")
_CONSOLELINK_INSTANCE = "default-dashboard"
_DASHBOARD_CYPRESS_CONFIG_DEFAULTS = {
    "OPERATOR_NAMESPACE": "redhat-ods-operator",
    "APPLICATIONS_NAMESPACE": _DASHBOARD_NS,
    "MONITORING_NAMESPACE": "redhat-ods-monitoring",
    "NOTEBOOKS_NAMESPACE": "rhods-notebooks",
    "OPERATOR_NAME": "rhods-operator",
}
_JUNIT_REPORT_REL_PATHS = (
    ("e2e", "junit-report.xml"),
    ("junit-report.xml",),
    ("junit", "junit-report.xml"),
)
_MOCHAWESOME_REL_PATHS = (
    ("e2e", ".jsons", "mochawesome.json"),
    ("e2e", "mochawesome.json"),
    (".jsons", "mochawesome.json"),
    ("mochawesome.json",),
)
_DASHBOARD_CYPRESS_RUNTIME_DEFAULTS = (
    ("CY_RETRY", "1"),
    ("CYPRESS_VERIFY_TIMEOUT", "180000"),
    ("LIBGL_ALWAYS_SOFTWARE", "1"),
    ("MESA_SHADER_CACHE_DISABLE", "true"),
    ("ELECTRON_DISABLE_GPU", "1"),
    ("UV_THREADPOOL_SIZE", "4"),
    ("ELECTRON_NO_ATTACH_CONSOLE", "1"),
)


def _consolelink_dashboard_href() -> str:
    """Jenkins dashboard-e2e-tests: consolelink href for default-dashboard instance."""
    r = oc_run(
        ["get", "consolelinks", "-A", "-o", "json"],
        check=False,
        capture_output=True,
        timeout=30,
    )
    if r.returncode != 0 or not (r.stdout or "").strip():
        return ""
    try:
        data = json.loads(r.stdout)
    except json.JSONDecodeError:
        return ""
    for item in data.get("items") or []:
        if not isinstance(item, dict):
            continue
        annotations = (item.get("metadata") or {}).get("annotations") or {}
        if annotations.get("platform.opendatahub.io/instance.name") != _CONSOLELINK_INSTANCE:
            continue
        href = ((item.get("spec") or {}).get("href") or "").strip()
        if href:
            return href.rstrip("/")
    return ""


def _route_host(route_name: str) -> str:
    r = oc_run(
        [
            "get",
            "route",
            route_name,
            "-n",
            _DASHBOARD_NS,
            "-o",
            "jsonpath={.spec.host}",
        ],
        check=False,
        capture_output=True,
        timeout=30,
    )
    return (r.stdout or "").strip()


def resolve_odh_dashboard_project_name(*, operator_namespace: str | None = None) -> str:
    """Return installed operator CSV displayName for CY_TEST_CONFIG (RHOAI vs ODH)."""
    op_ns = (operator_namespace or _DASHBOARD_CYPRESS_CONFIG_DEFAULTS["OPERATOR_NAMESPACE"]).strip()
    r = oc_run(
        ["get", "csv", "-n", op_ns, "-o", "json"],
        check=False,
        capture_output=True,
        timeout=30,
    )
    if r.returncode != 0 or not (r.stdout or "").strip():
        return ""
    try:
        doc = json.loads(r.stdout)
    except json.JSONDecodeError:
        return ""
    items = doc.get("items") if isinstance(doc, dict) else None
    if not isinstance(items, list):
        return ""
    for display in ("Red Hat OpenShift AI", "Open Data Hub"):
        for item in items:
            if not isinstance(item, dict):
                continue
            spec = item.get("spec") if isinstance(item.get("spec"), dict) else {}
            status = item.get("status") if isinstance(item.get("status"), dict) else {}
            if spec.get("displayName") == display and status.get("phase") == "Succeeded":
                return display
    return ""


def resolve_odh_dashboard_base_url() -> str:
    """Return https URL for the live dashboard (gateway consolelink, then route fallback)."""
    href = _consolelink_dashboard_href()
    if href:
        return href
    for route_name in _ROUTE_CANDIDATES:
        host = _route_host(route_name)
        if host:
            return f"https://{host}"
    return ""


def write_dashboard_cypress_test_config(path: Path, *, dashboard_url: str) -> None:
    """Minimal CY_TEST_CONFIG YAML when tenant vault secret is absent."""
    lines = [f"ODH_DASHBOARD_URL: {dashboard_url}"]
    project_name = resolve_odh_dashboard_project_name()
    if project_name:
        lines.append(f"ODH_DASHBOARD_PROJECT_NAME: {project_name}")
    for key, val in _DASHBOARD_CYPRESS_CONFIG_DEFAULTS.items():
        lines.append(f"{key}: {val}")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def bootstrap_dashboard_cypress_env(
    env_defaults: dict[str, str] | None,
    *,
    artifacts_dir: Path,
) -> dict[str, str]:
    """Resolve dashboard URL and optional CY_TEST_CONFIG for orchestrate step."""
    out = dict(env_defaults or {})
    staged_cfg = artifacts_dir / "dashboard-cypress-config.yml"
    if staged_cfg.is_file():
        out.setdefault("CY_TEST_CONFIG", str(staged_cfg))
        for line in staged_cfg.read_text(encoding="utf-8").splitlines():
            if line.startswith("ODH_DASHBOARD_URL:"):
                url = line.split(":", 1)[1].strip()
                if url:
                    out["ODH_DASHBOARD_URL"] = url
                    out.setdefault("BASE_URL", url)
                    print(f"Using dashboard URL from prepare step config: {url}", flush=True)
                    return out
    dashboard_url = resolve_odh_dashboard_base_url()
    if dashboard_url:
        out["ODH_DASHBOARD_URL"] = dashboard_url
        out.setdefault("BASE_URL", dashboard_url)
        if not out.get("CY_TEST_CONFIG"):
            cfg_path = artifacts_dir / "dashboard-cypress-config.yml"
            write_dashboard_cypress_test_config(cfg_path, dashboard_url=dashboard_url)
            out["CY_TEST_CONFIG"] = str(cfg_path)
        print(f"Resolved dashboard gateway URL: {dashboard_url}", flush=True)
        return out
    print(
        "WARN: could not resolve dashboard gateway URL; "
        "Cypress may use vault CY_TEST_CONFIG defaults",
        flush=True,
    )
    return out


def ensure_auth_overlay_before_cypress_runtime(env_defaults: dict[str, str]) -> None:
    """Ensure TEST_USER_AUTH_TYPE and credentials are set before apply_dashboard_cypress_runtime_env().

    Gateway auth overlay must complete before bearer token resolution, so Cypress knows
    whether to use LDAP, htpasswd, or OIDC login (which changes auth type expectations).
    """
    from components.dashboard_cypress.auth_overlay import \
      resolve_gateway_auth_overlay

    vault_path_raw = os.environ.get("VAULT_PATH", "").strip()
    if not vault_path_raw:
        return

    vault_path = Path(vault_path_raw)
    if not vault_path.is_file():
        return

    cluster_label = os.environ.get("CLUSTER_LABEL", "").strip()
    dashboard_url = env_defaults.get("ODH_DASHBOARD_URL") or os.environ.get("ODH_DASHBOARD_URL", "").strip()

    overlay = resolve_gateway_auth_overlay(vault_path, cluster_label, odh_dashboard_url=dashboard_url)
    if overlay:
        # Sync overlay into env so apply_dashboard_cypress_runtime_env reads correct auth type
        for key, val in overlay.items():
            if key == "TEST_USER" and isinstance(val, dict):
                for sub_key, sub_val in val.items():
                    env_var = f"TEST_USER_{sub_key.upper()}"
                    if isinstance(sub_val, str):
                        os.environ[env_var] = sub_val
                        env_defaults[env_var] = sub_val
            elif isinstance(val, str):
                os.environ[key] = val
                env_defaults[key] = val


def _token_from_kubeconfig(kubeconfig: str) -> str:
    """Read bearer token from kubeconfig user.token when oc whoami -t is unavailable."""
    path = Path(kubeconfig)
    if not kubeconfig or not path.is_file():
        return ""
    try:
        import yaml
    except ImportError:
        return ""
    try:
        doc = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return ""
    if not isinstance(doc, dict):
        return ""
    contexts = doc.get("contexts") if isinstance(doc.get("contexts"), list) else []
    users = doc.get("users") if isinstance(doc.get("users"), list) else []
    current = str(doc.get("current-context") or "").strip()
    if not current:
        return ""
    ctx = next((c for c in contexts if isinstance(c, dict) and c.get("name") == current), None)
    if not isinstance(ctx, dict):
        return ""
    context = ctx.get("context") if isinstance(ctx.get("context"), dict) else {}
    user_name = str(context.get("user") or "").strip()
    if not user_name:
        return ""
    user_entry = next((u for u in users if isinstance(u, dict) and u.get("name") == user_name), None)
    if not isinstance(user_entry, dict):
        return ""
    user = user_entry.get("user") if isinstance(user_entry.get("user"), dict) else {}
    token = str(user.get("token") or "").strip()
    # Sanity check: bearer tokens are typically long base64 or JWT
    if token and len(token) > 20:
        return token
    return ""


def _gateway_cypress_url(env_defaults: dict[str, str] | None = None) -> str:
    for source in (env_defaults or {}, os.environ):
        for key in ("ODH_DASHBOARD_URL", "BASE_URL"):
            val = str(source.get(key) or "").strip()
            if val:
                return val
    return ""


def _kuadrant_gateway_auth_ready() -> bool:
    """True when Kuadrant gateway + Authorino auth stack is Ready (avoids 503 errors on Cypress)."""
    # Check Kuadrant Gateway condition
    gw_r = oc_run(
        [
            "get",
            "gateway",
            "-A",
            "-o",
            "jsonpath={.items[*].status.conditions[?(@.type==\"Ready\")].status}",
        ],
        check=False,
        capture_output=True,
        timeout=30,
    )
    gw_ready = (gw_r.returncode == 0 and "True" in (gw_r.stdout or "").strip())

    # Check Authorino CR condition (RedHat authorino, not kuadrant-system)
    auth_r = oc_run(
        [
            "get",
            "authorino",
            "-A",
            "-o",
            "jsonpath={.items[*].status.conditions[?(@.type==\"Ready\")].status}",
        ],
        check=False,
        capture_output=True,
        timeout=30,
    )
    auth_ready = (auth_r.returncode == 0 and "True" in (auth_r.stdout or "").strip())

    return gw_ready and auth_ready


def _cypress_uses_bearer_bypass(*, env_defaults: dict[str, str] | None = None) -> bool:
    from components.dashboard_cypress.auth_overlay import \
      gateway_cypress_uses_bearer_bypass

    url = _gateway_cypress_url(env_defaults)
    return gateway_cypress_uses_bearer_bypass(odh_dashboard_url=url)


def _ldap_gateway_cypress_login(*, env_defaults: dict[str, str] | None = None) -> bool:
    if _cypress_uses_bearer_bypass(env_defaults=env_defaults):
        return False
    return str(os.environ.get("TEST_USER_AUTH_TYPE", "")).lower().startswith("ldap")


def resolve_oc_token_for_cypress(env_defaults: dict[str, str] | None = None) -> str:
    """Resolve OC bearer token for Cypress (env, oc whoami -t, then kubeconfig user.token)."""
    for source in (env_defaults or {}, os.environ):
        for key in ("OC_TOKEN", "CYPRESS_OC_TOKEN"):
            val = str(source.get(key) or "").strip()
            if val:
                return val
    token_r = oc_run(
        ["whoami", "-t"],
        check=False,
        capture_output=True,
        timeout=30,
    )
    if token_r.returncode == 0 and (token_r.stdout or "").strip():
        return (token_r.stdout or "").strip()
    return _token_from_kubeconfig(os.environ.get("KUBECONFIG", "").strip())


def apply_dashboard_cypress_runtime_env(env_defaults: dict[str, str]) -> None:
    """OC server, token, and Cypress runtime defaults before the run step."""
    # Pre-check Kuadrant/Authorino readiness before Cypress runs (avoid 503 errors)
    if not _kuadrant_gateway_auth_ready():
        print(
            "WARN: Kuadrant gateway or Authorino not Ready; Cypress may hit 503 errors",
            file=sys.stderr,
            flush=True,
        )

    server_r = oc_run(
        ["whoami", "--show-server"],
        check=False,
        capture_output=True,
        timeout=30,
    )
    if server_r.returncode == 0 and (server_r.stdout or "").strip():
        oc_server = (server_r.stdout or "").strip()
        env_defaults["CYPRESS_OC_SERVER"] = oc_server
        env_defaults["OC_SERVER"] = oc_server
        print(f"Resolved OC_SERVER for Cypress: {oc_server}", flush=True)
    if _cypress_uses_bearer_bypass(env_defaults=env_defaults):
        oc_token = resolve_oc_token_for_cypress(env_defaults)
        if oc_token:
            # CYPRESS_OC_TOKEN → Cypress.env('OC_TOKEN'); OC_TOKEN → shell/bash in tests
            env_defaults["CYPRESS_OC_TOKEN"] = oc_token
            env_defaults["OC_TOKEN"] = oc_token
            print("Resolved OC_TOKEN for Cypress (bearer auth bypass)", flush=True)
        else:
            print(
                "WARN: could not resolve OC_TOKEN for Cypress; bearer bypass may fail",
                file=sys.stderr,
                flush=True,
            )
    else:
        for key in ("OC_TOKEN", "CYPRESS_OC_TOKEN"):
            env_defaults.pop(key, None)
            os.environ.pop(key, None)
        print(
            "Gateway Cypress run: skipping OC_TOKEN at orchestrate (vault/OAuth login)",
            flush=True,
        )
    for key, val in _DASHBOARD_CYPRESS_RUNTIME_DEFAULTS:
        env_defaults.setdefault(key, val)


def sync_cypress_orchestrate_env(env_defaults: dict[str, str]) -> None:
    """Mirror orchestrate env_defaults into os.environ before baking Cypress --env."""
    for key in (
        "OC_TOKEN",
        "CYPRESS_OC_TOKEN",
        "OC_SERVER",
        "CYPRESS_OC_SERVER",
        "ODH_DASHBOARD_URL",
        "BASE_URL",
    ):
        val = str(env_defaults.get(key) or "").strip()
        if val:
            os.environ[key] = val
        else:
            os.environ.pop(key, None)


def normalize_cypress_run_config(run_config: str) -> str:
    """Collapse YAML fold whitespace so --config stays one shell token."""
    return "".join(run_config.split())


def _cypress_config_cli_arg(run_config: str) -> str:
    """Format --config for bash -c."""
    normalized = normalize_cypress_run_config(run_config)
    return f'--config "{normalized}"'


def _dsc_component_management_state(component_key: str) -> str:
    r = oc_run(
        [
            "get",
            "datasciencecluster",
            "default-dsc",
            "-o",
            f"jsonpath={{.spec.components.{component_key}.managementState}}",
        ],
        check=False,
        capture_output=True,
        timeout=30,
    )
    if r.returncode != 0:
        return ""
    return (r.stdout or "").strip()


def cypress_extra_env_flags() -> str:
    """Extra --env keys for Cypress when bearer auth or namespace defaults are set."""
    parts: list[str] = []
    ldap_gateway = _ldap_gateway_cypress_login()
    has_token = bool(
        os.environ.get("OC_TOKEN", "").strip() or os.environ.get("CYPRESS_OC_TOKEN", "").strip()
    )
    if ldap_gateway:
        has_token = False
    if not has_token:
        if "CLUSTER_AUTH" in os.environ:
            val = os.environ.get("CLUSTER_AUTH", "")
            parts.append(f'CLUSTER_AUTH="{val.replace(chr(34), chr(92) + chr(34))}"')
        for key in ("TEST_USER_USERNAME", "TEST_USER_PASSWORD", "TEST_USER_AUTH_TYPE"):
            val = os.environ.get(key, "").strip()
            if val:
                parts.append(f'{key}="{val.replace(chr(34), chr(92) + chr(34))}"')
    elif not ldap_gateway:
        parts.append('CLUSTER_AUTH=""')
    for key in ("OC_TOKEN", "CYPRESS_OC_TOKEN", "OC_SERVER", "CYPRESS_OC_SERVER"):
        if ldap_gateway and key in ("OC_TOKEN", "CYPRESS_OC_TOKEN"):
            continue
        val = os.environ.get(key, "").strip()
        if not val:
            continue
        if key in ("OC_SERVER", "CYPRESS_OC_SERVER") and not has_token:
            continue
        parts.append(f'{key}="{val.replace(chr(34), chr(92) + chr(34))}"')
    for key, default in _DASHBOARD_CYPRESS_CONFIG_DEFAULTS.items():
        val = os.environ.get(key, "").strip() or default
        parts.append(f'{key}="{val.replace(chr(34), chr(92) + chr(34))}"')
    return ",".join(parts)


_AUTH_CYPRESS_ENV_KEYS = frozenset(
    {
        "CLUSTER_AUTH",
        "TEST_USER_USERNAME",
        "TEST_USER_PASSWORD",
        "TEST_USER_AUTH_TYPE",
        "OC_TOKEN",
        "CYPRESS_OC_TOKEN",
        "OC_SERVER",
        "CYPRESS_OC_SERVER",
    }
)


def inject_auth_into_cypress_run_command(run_command: str) -> str:
    """Append auth Cypress --env keys after runtime auth sync (orchestrate runs earlier)."""
    extra = cypress_extra_env_flags()
    if not extra:
        return run_command
    auth_parts = [
        part
        for part in extra.split(",")
        if part.split("=", 1)[0].strip() in _AUTH_CYPRESS_ENV_KEYS
    ]
    if not auth_parts:
        return run_command
    suffix = ",".join(auth_parts)

    def _strip_auth_key(env_body: str, key: str) -> str:
        env_body = re.sub(rf'{re.escape(key)}="[^"]*"(,)?', "", env_body)
        return re.sub(r",+", ",", env_body).strip(",").strip()

    def _patch_env(match: re.Match[str]) -> str:
        prefix, env_body, tail = match.group(1), match.group(2), match.group(3)
        keep_keys = {part.split("=", 1)[0].strip() for part in auth_parts}
        for key in _AUTH_CYPRESS_ENV_KEYS:
            if key not in keep_keys:
                env_body = _strip_auth_key(env_body, key)
        # Orchestrate may bake CLUSTER_AUTH="" when OC_TOKEN was set; replace at run time.
        env_body = _strip_auth_key(env_body, "CLUSTER_AUTH")
        env_body = re.sub(r",+", ",", env_body).strip(",").strip()
        missing = [
            part
            for part in auth_parts
            if part.split("=", 1)[0].strip() not in env_body
        ]
        if not missing and env_body == match.group(2).strip():
            return match.group(0)
        insert = ",".join(missing)
        if env_body:
            return f"{prefix}{insert},{env_body}{tail}" if insert else f"{prefix}{env_body}{tail}"
        return f"{prefix}{insert}{tail}"

    return re.sub(
        r'(--env\s+)(.+?)(\s+--config)',
        _patch_env,
        run_command,
    )


def inject_skip_tags_into_cypress_run_command(run_command: str, extra_skip_tags: str) -> str:
    """Merge *extra_skip_tags* into skipTags= in orchestrate-produced Cypress --env."""
    extra = extra_skip_tags.strip()
    if not extra:
        return run_command

    def _patch_env(match: re.Match[str]) -> str:
        prefix, env_body, tail = match.group(1), match.group(2), match.group(3)
        tag_match = re.search(r'skipTags="([^"]*)"', env_body)
        if not tag_match:
            return match.group(0)
        merged = f"{tag_match.group(1)} {extra}".strip()
        new_body = env_body.replace(tag_match.group(0), f'skipTags="{merged}"', 1)
        return f"{prefix}{new_body}{tail}"

    return re.sub(
        r'(--env\s+)(.+?)(\s+--config)',
        _patch_env,
        run_command,
    )


def cypress_run_cli_prefix() -> str:
    """Use Electron when Chrome is absent or CYPRESS_BROWSER=electron is set."""
    forced = os.environ.get("CYPRESS_BROWSER", "").strip().lower()
    if forced == "electron":
        return "npx cypress run --browser electron --project ../packages/cypress "
    if shutil.which("google-chrome") or shutil.which("google-chrome-stable"):
        return "npm run cypress:run -- "
    return "npx cypress run --browser electron --project ../packages/cypress "


def cypress_set_command(
    *,
    grep_tag: str,
    results_subdir: str,
    skip_tags: str,
    test_timeout_seconds: str,
    run_config: str,
) -> str:
    """Single npm cypress:run invocation for one parallel tag set."""
    env_part = f'skipTags="{skip_tags}",CY_TEST_TIMEOUT_SECONDS={test_timeout_seconds}'
    extra = cypress_extra_env_flags()
    if extra:
        env_part = f"{env_part},{extra}"
    config = _cypress_config_cli_arg(run_config)
    return (
        f'CY_RESULTS_DIR="${{ARTIFACTS}}/{results_subdir}" '
        f"{cypress_run_cli_prefix()}"
        f'--env {env_part},grepTags="{grep_tag}" '
        f"{config}"
    )


def resolve_cypress_max_parallel(config_max: int | None = None) -> int | None:
    """Cap concurrent Cypress browsers to reduce gateway/Authorino 503 under load.

    Order: ``CYPRESS_MAX_PARALLEL`` env → catalog ``maxParallel`` → auto ``2`` for
    ``rh-ai.`` / RHCL gateway URLs → unlimited (None).
    """
    raw = os.environ.get("CYPRESS_MAX_PARALLEL", "").strip()
    if raw:
        try:
            value = int(raw)
        except ValueError:
            value = 0
        return value if value >= 1 else None
    if config_max is not None and config_max >= 1:
        return config_max
    url = (
        os.environ.get("ODH_DASHBOARD_URL", "").strip()
        or os.environ.get("BASE_URL", "").strip()
    )
    if "rh-ai." in url:
        return 2
    return None


def cypress_parallel_sets_command(
    *,
    sets: Sequence[CypressParallelSet],
    skip_tags: str,
    test_timeout_seconds: str,
    run_config: str,
    parallel_stagger_sec: int,
    display_base: int,
    max_parallel: int | None = None,
) -> str:
    """Run catalog-defined tag sets in parallel (Jenkins dashboard-e2e-tests).

    When *max_parallel* is set, a bash fifo semaphore limits concurrent browsers
    (gateway/Authorino 503 mitigation under five SmokeSets).
    """
    jobs: list[str] = []
    set_dirs: list[str] = []
    limit = resolve_cypress_max_parallel(max_parallel)
    for i, item in enumerate(sets):
        set_dirs.append(item.results_subdir)
        display = display_base + i
        stagger = i * parallel_stagger_sec
        set_cmd = cypress_set_command(
            grep_tag=item.grep_tag,
            results_subdir=item.results_subdir,
            skip_tags=skip_tags,
            test_timeout_seconds=test_timeout_seconds,
            run_config=run_config,
        )
        acquire = "read -u 3; " if limit else ""
        release = "echo >&3; " if limit else ""
        jobs.append(
            "("
            f"sleep {stagger}; "
            f"{acquire}"
            f'mkdir -p "${{ARTIFACTS}}/{item.results_subdir}"; '
            f"export DISPLAY=:{display}; "
            f"Xvfb :{display} -screen 0 1920x1080x24 -ac -nolisten tcp "
            f'>"${{ARTIFACTS}}/{item.results_subdir}/xvfb.log" 2>&1 & '
            f"xvfb_pid=$!; "
            f"sleep 3; "
            f"set +e; "
            f"{set_cmd} 2>&1 | tee \"${{ARTIFACTS}}/{item.results_subdir}/cypress.log\"; "
            f"ec=${{PIPESTATUS[0]}}; "
            f"kill $xvfb_pid 2>/dev/null || true; "
            f'echo $ec > "${{ARTIFACTS}}/{item.results_subdir}/exit"; '
            f'if [ -n "${{SCRIPTS_REPO_ROOT:-}}" ] && [ -n "${{ARTIFACT_PREFIX:-}}" ]; then '
            f'PYTHONPATH="${{SCRIPTS_REPO_ROOT}}:${{PYTHONPATH:-}}" '
            f'python3 -c "from pathlib import Path; import os; '
            f'from components.dashboard_cypress.runtime import refresh_partial_cypress_junit; '
            f'refresh_partial_cypress_junit(Path(os.environ[\\"ARTIFACTS\\"]), os.environ[\\"ARTIFACT_PREFIX\\"])" '
            f"2>/dev/null || true; "
            f"fi; "
            f"{release}"
            ") &"
        )
    dirs = " ".join(f'"${{ARTIFACTS}}/{d}"' for d in set_dirs)
    aggregate = (
        f"wait; fail=0; for d in {dirs}; do "
        '[ -f "$d/exit" ] && [ "$(cat "$d/exit")" -eq 0 ] || fail=1; '
        "done; exit $fail"
    )
    body = " ".join(jobs) + " " + aggregate
    if not limit:
        return body
    # Named fifo as counting semaphore (N tokens on fd 3).
    tokens = " ".join(["echo >&3;"] * limit)
    return (
        f'sem=$(mktemp -u); mkfifo "$sem"; exec 3<>"$sem"; rm -f "$sem"; '
        f"{tokens} "
        f"{body}"
    )


def cypress_results_subdirs(config: CypressRunnerConfig, phases: tuple[str, ...]) -> tuple[str, ...]:
    seen: set[str] = set()
    out: list[str] = []
    for phase in phases:
        for item in config.gates.get(phase, ()):
            if item.results_subdir not in seen:
                seen.add(item.results_subdir)
                out.append(item.results_subdir)
    return tuple(out)


def resolve_cypress_run_command(
    config: CypressRunnerConfig,
    phases: tuple[str, ...],
) -> str:
    """Build shell command from catalog cypress.gates for selected test phases."""
    parts: list[str] = []
    for phase in phases:
        sets = config.gates.get(phase)
        if not sets:
            continue
        parts.append(
            cypress_parallel_sets_command(
                sets=sets,
                skip_tags=config.skip_tags,
                test_timeout_seconds=config.test_timeout_seconds,
                run_config=config.run_config,
                parallel_stagger_sec=config.parallel_stagger_sec,
                display_base=config.display_base,
                max_parallel=config.max_parallel,
            )
        )
    return " && ".join(parts)


def _parse_cypress_runner_config(raw: dict[str, object]) -> CypressRunnerConfig | None:
    cy = raw.get("cypress")
    if not isinstance(cy, dict):
        return None
    skip_tags = str(cy.get("skip_tags") or "").strip()
    if not skip_tags:
        return None
    gates_raw = cy.get("gates")
    if not isinstance(gates_raw, dict):
        return None
    gates: dict[str, tuple[CypressParallelSet, ...]] = {}
    for gate_key, sets_raw in gates_raw.items():
        if not isinstance(sets_raw, list):
            continue
        sets: list[CypressParallelSet] = []
        for item in sets_raw:
            if not isinstance(item, dict):
                continue
            grep_tag = str(item.get("grep_tag") or "").strip()
            results_subdir = str(item.get("results_subdir") or "").strip()
            if grep_tag and results_subdir:
                sets.append(CypressParallelSet(grep_tag=grep_tag, results_subdir=results_subdir))
        if sets:
            gates[str(gate_key)] = tuple(sets)
    if not gates:
        return None
    max_parallel: int | None = None
    max_raw = cy.get("max_parallel")
    if max_raw is not None and str(max_raw).strip():
        max_parallel = int(max_raw)
    return CypressRunnerConfig(
        skip_tags=skip_tags,
        gates=gates,
        test_timeout_seconds=str(cy.get("test_timeout_seconds") or "480").strip(),
        parallel_stagger_sec=int(cy.get("parallel_stagger_sec") or 15),
        max_parallel=max_parallel,
        display_base=int(cy.get("display_base") or 99),
        run_config=normalize_cypress_run_config(str(cy.get("run_config") or (
            "numTestsKeptInMemory=0,experimentalMemoryManagement=true,"
            "video=false,viewportWidth=1920,viewportHeight=1080"
        ))),
    )


def _junit_report_has_testcases(path: Path) -> bool:
    """True when a junit-report.xml contains at least one testcase."""
    try:
        if path.stat().st_size == 0:
            return False
        doc = ET.parse(path).getroot()
    except (OSError, ET.ParseError):
        return False
    return bool(doc.findall(".//testcase"))


def _find_junit_report(subdir: Path) -> Path | None:
    reports = _find_all_junit_reports(subdir)
    return reports[0] if reports else None


def _find_all_junit_reports(subdir: Path) -> list[Path]:
    found: list[Path] = []
    seen: set[Path] = set()
    for rel_parts in _JUNIT_REPORT_REL_PATHS:
        candidate = subdir.joinpath(*rel_parts)
        if candidate.is_file() and _junit_report_has_testcases(candidate) and candidate not in seen:
            found.append(candidate)
            seen.add(candidate)
    if found:
        return found
    for candidate in sorted(subdir.rglob("*.xml")):
        if candidate.is_file() and _junit_report_has_testcases(candidate) and candidate not in seen:
            found.append(candidate)
            seen.add(candidate)
    return found


def write_merged_junit_reports(reports: Sequence[Path], dest: Path) -> bool:
    """Merge one or more JUnit XML files into *dest*."""
    if not reports:
        return False
    if len(reports) == 1:
        dest.write_bytes(reports[0].read_bytes())
        return True
    root = ET.Element("testsuites")
    for path in reports:
        _append_junit_testsuites(root, ET.parse(path).getroot())
    ET.ElementTree(root).write(dest, encoding="unicode", xml_declaration=True)
    return True


def _find_mochawesome_json_paths(subdir: Path) -> list[Path]:
    """Collect merged mochawesome JSON under a result subdir (ignore per-spec fragments)."""
    found: list[Path] = []
    seen: set[Path] = set()
    for rel_parts in _MOCHAWESOME_REL_PATHS:
        candidate = subdir.joinpath(*rel_parts)
        if candidate.is_file() and candidate not in seen:
            found.append(candidate)
            seen.add(candidate)
    if found:
        return found
    jsons_dir = subdir / "e2e" / ".jsons"
    if jsons_dir.is_dir():
        merged = jsons_dir / "mochawesome.json"
        if merged.is_file():
            return [merged]
        for candidate in sorted(jsons_dir.glob("mochawesome_*.json")):
            if candidate.is_file() and candidate not in seen:
                found.append(candidate)
                seen.add(candidate)
    return found


def _mochawesome_entries_to_tests(suite: dict) -> list[dict]:
    """Collect mochawesome test + hook entries for JUnit conversion."""
    entries: list[dict] = []
    for key in ("tests", "beforeHooks", "afterHooks"):
        for item in suite.get(key) or []:
            if isinstance(item, dict):
                entries.append(item)
    return entries


def _mochawesome_suite_to_junit(
    root: ET.Element, suite: dict, parent_title: str
) -> None:
    """Recursively convert a mochawesome suite dict into <testsuite> elements."""
    tests = _mochawesome_entries_to_tests(suite)
    suites = suite.get("suites") or []
    title = str(suite.get("title") or parent_title or "")
    full_title = f"{parent_title} > {title}" if parent_title and title else (parent_title or title)
    if tests:
        failures = sum(1 for t in tests if isinstance(t, dict) and t.get("state") == "failed")
        skipped = sum(
            1 for t in tests if isinstance(t, dict) and t.get("state") in ("pending", "skipped")
        )
        elapsed = sum((t.get("duration") or 0) for t in tests if isinstance(t, dict))
        suite_el = ET.SubElement(
            root,
            "testsuite",
            {
                "name": full_title,
                "tests": str(len(tests)),
                "failures": str(failures),
                "skipped": str(skipped),
                "time": f"{elapsed / 1000:.3f}",
            },
        )
        for test in tests:
            if not isinstance(test, dict):
                continue
            tc = ET.SubElement(
                suite_el,
                "testcase",
                {
                    "name": str(test.get("title") or ""),
                    "classname": full_title,
                    "time": f"{(test.get('duration') or 0) / 1000:.3f}",
                },
            )
            state = test.get("state", "")
            if state == "failed":
                err = test.get("err") or {}
                msg = str(err.get("message") or err) if err else ""
                ET.SubElement(tc, "failure").text = msg
            elif state in ("pending", "skipped"):
                ET.SubElement(tc, "skipped")
    for child in suites:
        if isinstance(child, dict):
            _mochawesome_suite_to_junit(root, child, full_title)


def _mochawesome_json_to_junit_xml(mochawesome_path: Path, dest: Path) -> bool:
    """Convert a mochawesome JSON report to JUnit XML and write to dest."""
    try:
        data = json.loads(mochawesome_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return False
    root = ET.Element("testsuites")
    for result in data.get("results") or []:
        if isinstance(result, dict):
            _mochawesome_suite_to_junit(root, result, "")
    if not len(root):
        return False
    ET.ElementTree(root).write(dest, encoding="unicode", xml_declaration=True)
    return True


def merge_smokeset_junit_reports(
    artifacts_dir: Path,
    dest: Path,
    *,
    results_subdirs: Sequence[str] | None = None,
) -> bool:
    """Merge junit-report.xml from parallel Cypress result dirs into dest.

    Falls back to converting mochawesome JSON when no junit-report.xml is found.
    """
    reports: list[Path] = []
    mochawesome_paths: list[Path] = []

    def _collect(subdir: Path) -> None:
        if not subdir.is_dir():
            return
        junit = _find_all_junit_reports(subdir)
        if junit:
            reports.extend(junit)
            return
        jsons = _find_mochawesome_json_paths(subdir)
        if jsons:
            mochawesome_paths.extend(jsons)

    if results_subdirs:
        for name in results_subdirs:
            _collect(artifacts_dir / name)
    else:
        for subdir in sorted(p for p in artifacts_dir.iterdir() if p.is_dir()):
            _collect(subdir)

    if mochawesome_paths:
        print(
            f"No junit-report.xml found; converting {len(mochawesome_paths)} "
            "mochawesome JSON report(s) to JUnit XML",
            flush=True,
        )
        if len(mochawesome_paths) == 1:
            return _mochawesome_json_to_junit_xml(mochawesome_paths[0], dest)
        root = ET.Element("testsuites")
        for mo_path in mochawesome_paths:
            try:
                data = json.loads(mo_path.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError):
                continue
            for result in data.get("results") or []:
                if isinstance(result, dict):
                    _mochawesome_suite_to_junit(root, result, "")
        if len(root):
            ET.ElementTree(root).write(dest, encoding="unicode", xml_declaration=True)
            return True

    if reports:
        return write_merged_junit_reports(reports, dest)

    return False


def _append_junit_testsuites(root: ET.Element, doc_root: ET.Element) -> None:
    if doc_root.tag == "testsuite":
        root.append(doc_root)
        return
    for child in doc_root:
        if child.tag == "testsuite":
            root.append(child)


def prepend_cypress_shell_env(
    run_command: str,
    *,
    tools_bin: str,
    kubeconfig: str,
) -> str:
    """Ensure parallel Cypress subshells inherit staged oc/jq, kubeconfig, and auth env."""
    prefix = (
        f'export PATH="{tools_bin}:$PATH"; '
        f'export KUBECONFIG="{kubeconfig}"; '
    )
    for key in (
        "OC_TOKEN",
        "CYPRESS_OC_TOKEN",
        "ODH_DASHBOARD_URL",
        "BASE_URL",
        "CY_TEST_CONFIG",
        "PYTHONPATH",
        "CLUSTER_AUTH",
        "TEST_USER_USERNAME",
        "TEST_USER_PASSWORD",
        "TEST_USER_AUTH_TYPE",
        "SCRIPTS_REPO_ROOT",
        "ARTIFACT_PREFIX",
    ):
        if key == "CLUSTER_AUTH":
            if key not in os.environ:
                continue
            val = os.environ.get(key, "")
        else:
            val = os.environ.get(key, "").strip()
            if not val:
                continue
        escaped = val.replace('"', '\\"')
        prefix += f'export {key}="{escaped}"; '
    for key, val in _DASHBOARD_CYPRESS_CONFIG_DEFAULTS.items():
        if os.environ.get(key, "").strip():
            continue
        prefix += f'export {key}="{val}"; '
    return f"{prefix}{run_command}"


def discover_cypress_results_subdirs(artifacts_dir: Path) -> tuple[str, ...]:
    """Return SmokeSet/SanitySet result dirs under ARTIFACTS (fallback for JUnit merge)."""
    return tuple(
        sorted(
            p.name
            for p in artifacts_dir.iterdir()
            if p.is_dir() and p.name.startswith(("SmokeSet", "SanitySet"))
        )
    )
