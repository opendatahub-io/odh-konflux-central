"""Validate smoke DSC keys against operator-pinned RHOAI-Build-Config chart (Jenkins RhoaiDscComponentsResolver parity)."""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import urllib.error
import urllib.request
from dataclasses import dataclass
from functools import lru_cache
from typing import Any, Callable

from suite.errors import AppError

_FETCH_USER_AGENT = "rhoai-e2e-dsc-chart-validate"
_DEFAULT_CONNECT_TIMEOUT_SEC = 30
_DEFAULT_READ_TIMEOUT_SEC = 120
MANAGEMENT_STATES = frozenset({"Managed", "Unmanaged", "Removed"})

RHOAI_OPERATOR_GITHUB = "red-hat-data-services/rhods-operator"
RHOAI_BUILD_CONFIG_GITHUB = "red-hat-data-services/RHOAI-Build-Config"
MANIFESTS_CONFIG_PATHS = ("manifests-config.yaml", "build/manifests-config.yaml")
CHART_VALUES_PATH = "to-be-processed/helm/rhai-on-openshift-chart/values.yaml"
OPENSHIFT_VALUES_PATCH_PATH = "helm/openshift-values-patch.yaml"

# Jenkins GateJobParams.GATE_DSC_SUMMARY_COMPONENT_KEYS (promotion-gate catalog).
GATE_DSC_SUMMARY_COMPONENT_KEYS: tuple[str, ...] = (
    "dashboard",
    "workbenches",
    "aipipelines",
    "kserve",
    "kueue",
    "ray",
    "trustyai",
    "trainingoperator",
    "trainer",
    "modelregistry",
    "feastoperator",
    "llamastackoperator",
    "ogx",
    "mlflowoperator",
    "modelsasservice",
    "sparkoperator",
    "aigateway",
    "batchgateway",
    "mcplifecycleoperator",
)

CHART_COMPONENT_KEY_ALIASES: dict[str, str] = {
    "aipipelines": "datasciencepipelines",
    "codeflare": "ray",
}

NESTED_CHART_DSC_PATHS: dict[str, tuple[str, str]] = {
    "modelsasservice": ("aigateway", "modelsAsAService"),
    "batchgateway": ("aigateway", "batchGateway"),
}

# Promotion-gate policy key → DSC spec.components key (when they differ).
POLICY_KEY_TO_DSC_SPEC: dict[str, str] = {
    "aipipelines": "aipipelines",
    "datasciencepipelines": "aipipelines",
    "trainingoperator": "trainingoperator",
    "trainer": "trainer",
    "modelregistry": "modelregistry",
}


@dataclass(frozen=True)
class PinnedChartContext:
    operator_git_ref: str
    build_config_display_ref: str
    build_config_fetch_ref: str
    values_yaml_url: str
    values_doc: dict[str, Any]


def chart_validation_enabled() -> bool:
    raw = os.environ.get("DSC_CHART_VALIDATE", "true").strip().lower()
    return raw not in ("0", "false", "no", "off")


def infer_operator_git_ref(operator_version: str) -> str:
    fallback = os.environ.get("RHOAI_OPERATOR_GIT_REF", "").strip() or "main"
    ver = (operator_version or "").strip()
    if not ver:
        return fallback
    normalized = re.sub(r"^[vV]", "", ver)
    match = re.match(r"^(\d+)\.(\d+)", normalized)
    if match:
        return f"rhoai-{match.group(1)}.{match.group(2)}"
    return fallback


def raw_github_content_url(owner_repo: str, git_ref: str, repo_relative_path: str) -> str:
    path = repo_relative_path.strip().lstrip("/")
    ref = git_ref.strip()
    return f"https://raw.githubusercontent.com/{owner_repo}/{ref}/{path}"


def _urlopen_timeout_sec(connect_timeout_sec: int, read_timeout_sec: int) -> float:
    if sys.version_info >= (3, 11):
        return (float(connect_timeout_sec), float(read_timeout_sec))  # type: ignore[return-value]
    return float(connect_timeout_sec) + float(read_timeout_sec)


def fetch_text(url: str, *, connect_timeout_sec: int = _DEFAULT_CONNECT_TIMEOUT_SEC) -> str:
    read_timeout = int(
        os.environ.get("DSC_CHART_VALIDATE_READ_TIMEOUT_SEC", str(_DEFAULT_READ_TIMEOUT_SEC))
    )
    req = urllib.request.Request(url, headers={"User-Agent": _FETCH_USER_AGENT})
    try:
        with urllib.request.urlopen(
            req, timeout=_urlopen_timeout_sec(connect_timeout_sec, read_timeout)
        ) as resp:
            return resp.read().decode("utf-8")
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")[:500]
        raise AppError(f"HTTP GET {url} failed: {exc.code}. {body}", 2) from exc
    except urllib.error.URLError as exc:
        raise AppError(f"HTTP GET {url} failed: {exc.reason}", 2) from exc


def fetch_manifests_config(
    operator_git_ref: str,
    fetch_text_fn: Callable[[str], str] = fetch_text,
) -> tuple[str, str]:
    failures: list[str] = []
    for path in MANIFESTS_CONFIG_PATHS:
        url = raw_github_content_url(RHOAI_OPERATOR_GITHUB, operator_git_ref, path)
        try:
            yaml_text = fetch_text_fn(url).strip()
            if yaml_text:
                return yaml_text, url
        except AppError as exc:
            if "404" in str(exc):
                failures.append(url)
                continue
            raise
    raise AppError(
        f"Operator manifests config not found for ref {operator_git_ref} "
        f"(tried {', '.join(MANIFESTS_CONFIG_PATHS)}): {'; '.join(failures)}",
        2,
    )


def _ensure_chart_validate_yaml_loader() -> None:
    try:
        import yaml  # type: ignore[import-untyped, unused-ignore]  # noqa: F401
        return
    except ImportError:
        pass
    if shutil.which("yq"):
        return
    from helpers.pip_bootstrap import pip_install_to_target, prepend_pythonpath
    from steps.tests_payload import resolve_tests_payload_root, tests_payload_tools_python_dir

    artifacts = os.environ.get("ARTIFACTS_DIR", "").strip() or "/workspace/tests-shared"
    target = tests_payload_tools_python_dir(resolve_tests_payload_root(artifacts))
    print(f"Installing PyYAML to {target} (DSC chart validation)...", flush=True)
    pip_install_to_target("pyyaml", target)
    prepend_pythonpath(str(target))
    import yaml  # type: ignore[import-untyped, unused-ignore]  # noqa: F401


def parse_build_config_pin(manifests_yaml: str) -> tuple[str, str]:
    doc = _load_yaml_document_from_text(manifests_yaml)
    ref = str((doc.get("buildConfig") or {}).get("rhoai", {}).get("ref") or "").strip()
    if not ref:
        return "", ""
    if "@" in ref:
        branch_part, commit_part = ref.split("@", 1)
        commit = commit_part.strip()
        return ref, commit or branch_part.strip()
    return ref, ref


def _load_yaml_document_from_text(yaml_text: str) -> dict[str, Any]:
    if not yaml_text.strip():
        return {}
    loaded = _load_yaml_document_from_string(yaml_text)
    return loaded if isinstance(loaded, dict) else {}


def _load_yaml_document_from_string(yaml_text: str) -> Any:
    text = yaml_text.strip()
    if not text:
        return {}
    if text.startswith("{"):
        return json.loads(text)
    try:
        import yaml  # type: ignore[import-untyped]

        loaded = yaml.safe_load(yaml_text)
        return loaded if loaded is not None else {}
    except ImportError:
        pass
    except Exception as exc:
        raise AppError(f"Invalid YAML document: {exc}", 2) from exc

    yq_bin = shutil.which("yq")
    if yq_bin:
        try:
            proc = subprocess.run(
                [yq_bin, "e", "-o=json", "."],
                input=yaml_text,
                capture_output=True,
                text=True,
                check=False,
                timeout=120,
            )
        except subprocess.TimeoutExpired as exc:
            raise AppError(f"yq timed out parsing YAML (>{exc.timeout}s)", 2) from exc
        if proc.returncode == 0 and proc.stdout.strip():
            try:
                return json.loads(proc.stdout)
            except json.JSONDecodeError as exc:
                raise AppError(f"Invalid JSON from yq: {exc}", 2) from exc
        detail = (proc.stderr or proc.stdout or "").strip()
        raise AppError(f"yq failed to parse YAML: {detail or proc.returncode}", 2)

    raise AppError(
        "DSC chart validation requires PyYAML or yq in the install-rhoai task image",
        2,
    )


def _deep_merge_maps(base: dict[str, Any], overlay: dict[str, Any]) -> dict[str, Any]:
    if not overlay:
        return dict(base or {})
    result: dict[str, Any] = dict(base or {})
    for key, value in overlay.items():
        if isinstance(value, dict) and isinstance(result.get(key), dict):
            result[key] = _deep_merge_maps(result[key], value)
        elif value is not None:
            result[key] = value
    return result


def _chart_profile(values_doc: dict[str, Any]) -> str:
    return str(values_doc.get("profile") or "default").strip() or "default"


def _chart_profiles_path(profile: str) -> str:
    chart_dir = CHART_VALUES_PATH.replace("/values.yaml", "")
    return f"{chart_dir}/profiles/{profile}.yaml"


def _fetch_optional_yaml(
    fetch_text_fn: Callable[[str], str],
    build_config_fetch_ref: str,
    repo_relative_path: str,
) -> dict[str, Any]:
    try:
        url = raw_github_content_url(RHOAI_BUILD_CONFIG_GITHUB, build_config_fetch_ref, repo_relative_path)
        yaml_text = fetch_text_fn(url).strip()
        return _load_yaml_document_from_text(yaml_text) if yaml_text else {}
    except AppError:
        return {}


def enrich_chart_values(
    values_doc: dict[str, Any],
    build_config_fetch_ref: str,
    fetch_text_fn: Callable[[str], str],
) -> dict[str, Any]:
    merged = _deep_merge_maps(
        values_doc or {},
        _fetch_optional_yaml(fetch_text_fn, build_config_fetch_ref, OPENSHIFT_VALUES_PATCH_PATH),
    )
    profile = _chart_profile(merged)
    profile_doc = _fetch_optional_yaml(fetch_text_fn, build_config_fetch_ref, _chart_profiles_path(profile))
    profile_components = profile_doc.get("components") if isinstance(profile_doc.get("components"), dict) else {}
    merged["_profileComponents"] = profile_components
    return merged


def _resolve_explicit_state(chart_state: Any, profile_state: Any) -> str | None:
    chart_explicit = str(chart_state or "").strip()
    if chart_explicit in MANAGEMENT_STATES:
        return chart_explicit
    profile_explicit = str(profile_state or "").strip()
    if profile_explicit in MANAGEMENT_STATES:
        return profile_explicit
    return None


def release_policy_state_for_key(
    dsc_key: str,
    chart_components: dict[str, Any],
    profile_components: dict[str, Any],
) -> str:
    if dsc_key in NESTED_CHART_DSC_PATHS:
        parent, nested = NESTED_CHART_DSC_PATHS[dsc_key]
        chart_dsc = (chart_components.get(parent) or {}).get("dsc") if isinstance(chart_components.get(parent), dict) else {}
        profile_dsc = (
            (profile_components.get(parent) or {}).get("dsc")
            if isinstance(profile_components.get(parent), dict)
            else {}
        )
        chart_dsc = chart_dsc if isinstance(chart_dsc, dict) else {}
        profile_dsc = profile_dsc if isinstance(profile_dsc, dict) else {}
        if nested not in chart_dsc and nested not in profile_dsc:
            return "Removed"
        explicit = _resolve_explicit_state(
            (chart_dsc.get(nested) or {}).get("managementState")
            if isinstance(chart_dsc.get(nested), dict)
            else chart_dsc.get(nested),
            (profile_dsc.get(nested) or {}).get("managementState")
            if isinstance(profile_dsc.get(nested), dict)
            else profile_dsc.get(nested),
        )
        return explicit or "Managed"

    chart_entry = chart_components.get(dsc_key) if isinstance(chart_components.get(dsc_key), dict) else {}
    profile_entry = profile_components.get(dsc_key) if isinstance(profile_components.get(dsc_key), dict) else {}
    chart_entry = chart_entry or {}
    profile_entry = profile_entry or {}
    if "dsc" not in chart_entry and "dsc" not in profile_entry:
        alias = CHART_COMPONENT_KEY_ALIASES.get(dsc_key, dsc_key)
        if alias != dsc_key:
            return release_policy_state_for_key(alias, chart_components, profile_components)
        return "Removed"
    chart_dsc = chart_entry.get("dsc") if isinstance(chart_entry.get("dsc"), dict) else {}
    profile_dsc = profile_entry.get("dsc") if isinstance(profile_entry.get("dsc"), dict) else {}
    explicit = _resolve_explicit_state(
        chart_dsc.get("managementState"),
        profile_dsc.get("managementState"),
    )
    return explicit or "Managed"


def build_release_eligible_policy_from_chart(
    values_doc: dict[str, Any],
    profile_components: dict[str, Any] | None = None,
) -> dict[str, str]:
    chart_components = values_doc.get("components")
    if not isinstance(chart_components, dict):
        chart_components = {}
    profile = profile_components if profile_components is not None else values_doc.get("_profileComponents")
    if not isinstance(profile, dict):
        profile = {}
    policy: dict[str, str] = {}
    for dsc_key in GATE_DSC_SUMMARY_COMPONENT_KEYS:
        policy[dsc_key] = release_policy_state_for_key(dsc_key, chart_components, profile)
    return policy


def apply_component_policy_overrides(
    base_policy: dict[str, str],
    override_string: str,
) -> dict[str, str]:
    if not (override_string or "").strip():
        return dict(base_policy)
    result = dict(base_policy)
    for key, state in parse_component_names_policy(override_string).items():
        result[key] = state
    return result


def component_names_to_string(policy: dict[str, str]) -> str:
    return ",".join(f"{key}:{state}" for key, state in sorted(policy.items()))


def resolve_pinned_chart_context(
    operator_version: str,
    *,
    operator_git_ref: str = "",
    fetch_text_fn: Callable[[str], str] = fetch_text,
    enrich: bool = True,
) -> PinnedChartContext:
    _ensure_chart_validate_yaml_loader()
    op_ref = (operator_git_ref or infer_operator_git_ref(operator_version)).strip()
    manifests_yaml, _manifests_url = fetch_manifests_config(op_ref, fetch_text_fn)
    display_ref, fetch_ref = parse_build_config_pin(manifests_yaml)
    if not fetch_ref:
        fetch_ref = os.environ.get("RHOAI_BUILD_CONFIG_GIT_REF", "").strip() or op_ref
    values_url = raw_github_content_url(RHOAI_BUILD_CONFIG_GITHUB, fetch_ref, CHART_VALUES_PATH)
    values_yaml = fetch_text_fn(values_url)
    values_doc = _load_yaml_document_from_text(values_yaml)
    if not values_doc:
        raise AppError(f"Pinned chart values empty or unreadable: {values_url}", 2)
    if enrich:
        values_doc = enrich_chart_values(values_doc, fetch_ref, fetch_text_fn)
    return PinnedChartContext(
        operator_git_ref=op_ref,
        build_config_display_ref=display_ref,
        build_config_fetch_ref=fetch_ref,
        values_yaml_url=values_url,
        values_doc=values_doc,
    )


def resolve_chart_dsc_policy_for_version(
    operator_version: str,
    *,
    components_override: str = "",
    operator_git_ref: str = "",
    fetch_text_fn: Callable[[str], str] | None = None,
) -> dict[str, str]:
    if fetch_text_fn is not None:
        ctx = resolve_pinned_chart_context(
            operator_version,
            operator_git_ref=operator_git_ref,
            fetch_text_fn=fetch_text_fn,
        )
    else:
        ctx = _cached_pinned_chart(operator_version.strip(), (operator_git_ref or "").strip())
    profile_components = ctx.values_doc.get("_profileComponents")
    if not isinstance(profile_components, dict):
        profile_components = {}
    policy = build_release_eligible_policy_from_chart(ctx.values_doc, profile_components)
    override = (components_override or os.environ.get("PROMOTION_GATE_COMPONENTS_OVERRIDE", "")).strip()
    return apply_component_policy_overrides(policy, override)


def _chart_has_component(chart_components: dict[str, Any], dsc_key: str) -> bool:
    key = dsc_key.strip().lower()
    if key in NESTED_CHART_DSC_PATHS:
        parent, nested = NESTED_CHART_DSC_PATHS[key]
        parent_doc = chart_components.get(parent)
        if not isinstance(parent_doc, dict):
            return False
        dsc = parent_doc.get("dsc")
        return isinstance(dsc, dict) and nested in dsc
    if key in chart_components:
        return True
    alias = CHART_COMPONENT_KEY_ALIASES.get(key, key)
    return alias in chart_components


def _dsc_spec_key_allowed_by_policy(dsc_spec_key: str, policy: dict[str, str], values_doc: dict[str, Any]) -> bool:
    key = dsc_spec_key.strip().lower()
    chart_components = values_doc.get("components")
    if isinstance(chart_components, dict) and _chart_has_component(chart_components, key):
        if key in policy and policy[key] == "Removed":
            return False
        return True
    if key in policy:
        return policy[key] in ("Managed", "Unmanaged")
    alias = CHART_COMPONENT_KEY_ALIASES.get(key, key)
    if alias != key and alias in policy:
        return policy[alias] in ("Managed", "Unmanaged")
    for policy_key, spec_key in POLICY_KEY_TO_DSC_SPEC.items():
        if spec_key == key and policy_key in policy:
            return policy[policy_key] in ("Managed", "Unmanaged")
    return False


def validate_dsc_keys_supported_by_chart(
    dsc_keys: set[str],
    values_doc: dict[str, Any],
    *,
    components_override: str = "",
) -> None:
    profile_components = values_doc.get("_profileComponents")
    if not isinstance(profile_components, dict):
        profile_components = {}
    policy = build_release_eligible_policy_from_chart(values_doc, profile_components)
    policy = apply_component_policy_overrides(policy, components_override)
    errors: list[str] = []
    for key in sorted(dsc_keys):
        if not _dsc_spec_key_allowed_by_policy(key, policy, values_doc):
            errors.append(
                f"smoke DSC key '{key}' is Removed or missing in pinned chart policy "
                f"(profile={_chart_profile(values_doc)})"
            )
    if errors:
        raise AppError(
            f"Pinned chart validation failed ({len(errors)}): {'; '.join(errors)}",
            2,
        )


@lru_cache(maxsize=8)
def _cached_pinned_chart(operator_version: str, operator_git_ref: str) -> PinnedChartContext:
    return resolve_pinned_chart_context(operator_version, operator_git_ref=operator_git_ref)


def validate_smoke_managed_keys_for_operator_version(
    managed_keys: set[str],
    operator_version: str,
    *,
    operator_git_ref: str = "",
    components_override: str = "",
    fetch_text_fn: Callable[[str], str] | None = None,
) -> list[str]:
    if not managed_keys or not (operator_version or "").strip():
        return []
    if fetch_text_fn is not None:
        ctx = resolve_pinned_chart_context(
            operator_version,
            operator_git_ref=operator_git_ref,
            fetch_text_fn=fetch_text_fn,
        )
    else:
        ctx = _cached_pinned_chart(operator_version.strip(), (operator_git_ref or "").strip())
    validate_dsc_keys_supported_by_chart(
        managed_keys,
        ctx.values_doc,
        components_override=components_override,
    )
    return [
        f"operatorGitRef={ctx.operator_git_ref}",
        f"buildConfigRef={ctx.build_config_display_ref or '(none)'}",
        f"buildConfigFetchRef={ctx.build_config_fetch_ref}",
        f"valuesYamlUrl={ctx.values_yaml_url}",
        f"chartProfile={_chart_profile(ctx.values_doc)}",
        f"validatedSmokeDscKeys={len(managed_keys)}",
    ]


def parse_component_names_policy(policy_string: str) -> dict[str, str]:
    out: dict[str, str] = {}
    for entry in (policy_string or "").split(","):
        part = entry.strip()
        if not part:
            continue
        key, _, mode = part.partition(":")
        name = key.strip().lower()
        if not name:
            continue
        out[name] = mode.strip() or "Managed"
    return out


def install_removed_dsc_keys_from_policy_map(policy: dict[str, str]) -> frozenset[str]:
    removed: set[str] = set()
    for key, state in policy.items():
        if state != "Removed":
            continue
        if key in NESTED_CHART_DSC_PATHS:
            continue
        removed.add(POLICY_KEY_TO_DSC_SPEC.get(key, key))
    return frozenset(removed)


def install_removed_keys_from_promotion_policy(
    policy_string: str,
    *,
    dsc_cr_key: bool = True,
) -> frozenset[str]:
    if not dsc_cr_key:
        return install_removed_dsc_keys_from_policy_map(parse_component_names_policy(policy_string))
    return install_removed_dsc_keys_from_policy_map(parse_component_names_policy(policy_string))
