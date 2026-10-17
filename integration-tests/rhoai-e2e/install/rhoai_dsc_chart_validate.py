"""DSC policy from rhods-operator samples (Jenkins RhoaiDscComponentsResolver parity)."""

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
OPERATOR_DSC_SAMPLE_V2_PATH = "config/rhoai/samples/datasciencecluster_v2_datasciencecluster.yaml"
OPERATOR_DSC_SAMPLE_V3_PATH = "config/rhoai/samples/datasciencecluster_v3_datasciencecluster.yaml"

OPERATOR_SAMPLE_POLICY_PATHS_V2: dict[str, tuple[str, ...]] = {
    "modelsasservice": ("kserve", "modelsAsService"),
    "batchgateway": ("aigateway", "batchGateway"),
}
OPERATOR_SAMPLE_POLICY_PATHS_V3: dict[str, tuple[str, ...]] = {
    "dashboard": ("dashboard", "standard"),
    "modelsasservice": ("aigateway", "modelsAsAService"),
    "batchgateway": ("aigateway", "batchGateway"),
    "modelregistry": ("aiHub",),
    "feastoperator": ("data", "featureStore"),
}

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

POLICY_KEY_TO_DSC_SPEC: dict[str, str] = {
    "aipipelines": "aipipelines",
    "datasciencepipelines": "aipipelines",
    "trainingoperator": "trainingoperator",
    "trainer": "trainer",
    "modelregistry": "modelregistry",
}


@dataclass(frozen=True)
class OperatorDscSampleContext:
    operator_git_ref: str
    dsc_sample_path: str
    dsc_sample_url: str
    dsc_sample_doc: dict[str, Any]
    sample_discovery: str = ""


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


def _fetch_text_allow_not_found(fetch_text_fn: Callable[[str], str], url: str) -> str | None:
    try:
        text = fetch_text_fn(url).strip()
        return text or None
    except AppError as exc:
        if "404" in str(exc):
            return None
        raise


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
    print(f"Installing PyYAML to {target} (DSC sample validation)...", flush=True)
    pip_install_to_target("pyyaml", target)
    prepend_pythonpath(str(target))
    import yaml  # type: ignore[import-untyped, unused-ignore]  # noqa: F401


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
        proc = subprocess.run(
            [yq_bin, "e", "-o=json", "."],
            input=yaml_text,
            capture_output=True,
            text=True,
            check=False,
            timeout=120,
        )
        if proc.returncode == 0 and proc.stdout.strip():
            return json.loads(proc.stdout)
        detail = (proc.stderr or proc.stdout or "").strip()
        raise AppError(f"yq failed to parse YAML: {detail or proc.returncode}", 2)

    raise AppError(
        "DSC sample validation requires PyYAML or yq in the install-rhoai task image",
        2,
    )


def candidate_sample_paths_for_operator_ref(operator_git_ref: str) -> tuple[str, ...]:
    ref = (operator_git_ref or "").strip()
    if "-ea" in ref:
        return (OPERATOR_DSC_SAMPLE_V2_PATH, OPERATOR_DSC_SAMPLE_V3_PATH)
    match = re.match(r"rhoai-(\d+)\.(\d+)", ref)
    if match:
        major, minor = int(match.group(1)), int(match.group(2))
        if major > 3 or (major == 3 and minor >= 6):
            return (OPERATOR_DSC_SAMPLE_V3_PATH, OPERATOR_DSC_SAMPLE_V2_PATH)
    return (OPERATOR_DSC_SAMPLE_V2_PATH, OPERATOR_DSC_SAMPLE_V3_PATH)


def discover_operator_sample(
    operator_git_ref: str,
    explicit_sample_path: str,
    fetch_text_fn: Callable[[str], str],
) -> OperatorDscSampleContext:
    candidates = (
        (explicit_sample_path.strip(),)
        if explicit_sample_path.strip()
        else candidate_sample_paths_for_operator_ref(operator_git_ref)
    )
    probed: list[str] = []
    for path in candidates:
        url = raw_github_content_url(RHOAI_OPERATOR_GITHUB, operator_git_ref, path)
        probed.append(path)
        yaml_text = _fetch_text_allow_not_found(fetch_text_fn, url)
        if not yaml_text:
            continue
        doc = _load_yaml_document_from_text(yaml_text)
        if (doc.get("spec") or {}).get("components"):
            discovery = f"explicit={path}" if explicit_sample_path.strip() else f"probed={'>'.join(probed)}"
            return OperatorDscSampleContext(
                operator_git_ref=operator_git_ref,
                dsc_sample_path=path,
                dsc_sample_url=url,
                dsc_sample_doc=doc,
                sample_discovery=discovery,
            )
    fallback_path = candidates[0]
    fallback_url = raw_github_content_url(RHOAI_OPERATOR_GITHUB, operator_git_ref, fallback_path)
    fallback_yaml = fetch_text_fn(fallback_url)
    fallback_doc = _load_yaml_document_from_text(fallback_yaml)
    return OperatorDscSampleContext(
        operator_git_ref=operator_git_ref,
        dsc_sample_path=fallback_path,
        dsc_sample_url=fallback_url,
        dsc_sample_doc=fallback_doc,
        sample_discovery=f"fallback={fallback_path}; probed={'>'.join(probed)}",
    )


def read_management_state_from_components(components: dict[str, Any], path: tuple[str, ...]) -> str | None:
    node: Any = components
    for segment in path:
        if not isinstance(node, dict) or segment not in node:
            return None
        node = node[segment]
    if isinstance(node, dict):
        state = str(node.get("managementState") or "").strip()
        return state or None
    return None


def build_policy_from_operator_sample(dsc_sample_doc: dict[str, Any]) -> dict[str, str]:
    api_version = str(dsc_sample_doc.get("apiVersion") or "")
    v3 = "/v3" in api_version
    path_overrides = OPERATOR_SAMPLE_POLICY_PATHS_V3 if v3 else OPERATOR_SAMPLE_POLICY_PATHS_V2
    components = dsc_sample_doc.get("spec", {}).get("components")
    if not isinstance(components, dict):
        components = {}
    policy: dict[str, str] = {}
    for dsc_key in GATE_DSC_SUMMARY_COMPONENT_KEYS:
        path = path_overrides.get(dsc_key, (dsc_key,))
        state = read_management_state_from_components(components, path)
        policy[dsc_key] = state if state in MANAGEMENT_STATES else "Removed"
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
        if key in OPERATOR_SAMPLE_POLICY_PATHS_V2 or key in OPERATOR_SAMPLE_POLICY_PATHS_V3:
            continue
        removed.add(POLICY_KEY_TO_DSC_SPEC.get(key, key))
    return frozenset(removed)


def install_removed_keys_from_promotion_policy(policy_string: str) -> frozenset[str]:
    return install_removed_dsc_keys_from_policy_map(parse_component_names_policy(policy_string))


def resolve_operator_dsc_sample(
    operator_version: str,
    *,
    operator_git_ref: str = "",
    explicit_sample_path: str = "",
    fetch_text_fn: Callable[[str], str] = fetch_text,
) -> OperatorDscSampleContext:
    _ensure_chart_validate_yaml_loader()
    op_ref = (operator_git_ref or infer_operator_git_ref(operator_version)).strip()
    explicit = (
        explicit_sample_path.strip()
        or os.environ.get("RHOAI_OPERATOR_DSC_SAMPLE_PATH", "").strip()
    )
    ctx = discover_operator_sample(op_ref, explicit, fetch_text_fn)
    if not (ctx.dsc_sample_doc.get("spec") or {}).get("components"):
        raise AppError(
            f"Operator DSC sample at {ctx.dsc_sample_url} has no spec.components "
            f"(discovery={ctx.sample_discovery})",
            2,
        )
    return ctx


def resolve_operator_dsc_policy_for_version(
    operator_version: str,
    *,
    components_override: str = "",
    operator_git_ref: str = "",
    explicit_sample_path: str = "",
    fetch_text_fn: Callable[[str], str] | None = None,
) -> dict[str, str]:
    if fetch_text_fn is not None:
        ctx = resolve_operator_dsc_sample(
            operator_version,
            operator_git_ref=operator_git_ref,
            explicit_sample_path=explicit_sample_path,
            fetch_text_fn=fetch_text_fn,
        )
    else:
        ctx = _cached_operator_sample(
            operator_version.strip(),
            (operator_git_ref or "").strip(),
            (explicit_sample_path or "").strip(),
        )
    policy = build_policy_from_operator_sample(ctx.dsc_sample_doc)
    override = (components_override or os.environ.get("PROMOTION_GATE_COMPONENTS_OVERRIDE", "")).strip()
    return apply_component_policy_overrides(policy, override)


def resolve_chart_dsc_policy_for_version(
    operator_version: str,
    **kwargs: Any,
) -> dict[str, str]:
    """Backward-compatible alias (policy is operator-sample based, not Build-Config chart)."""
    return resolve_operator_dsc_policy_for_version(operator_version, **kwargs)


@lru_cache(maxsize=8)
def _cached_operator_sample(
    operator_version: str,
    operator_git_ref: str,
    explicit_sample_path: str,
) -> OperatorDscSampleContext:
    return resolve_operator_dsc_sample(
        operator_version,
        operator_git_ref=operator_git_ref,
        explicit_sample_path=explicit_sample_path,
    )


def _dsc_spec_key_allowed_by_sample_policy(
    dsc_spec_key: str,
    policy: dict[str, str],
    dsc_sample_doc: dict[str, Any],
) -> bool:
    key = dsc_spec_key.strip().lower()
    if key in policy:
        return policy[key] in ("Managed", "Unmanaged")
    components = dsc_sample_doc.get("spec", {}).get("components")
    if not isinstance(components, dict):
        return False
    state = read_management_state_from_components(components, (key,))
    if state in ("Managed", "Unmanaged"):
        return True
    return False


def validate_dsc_keys_supported_by_sample_policy(
    dsc_keys: set[str],
    policy: dict[str, str],
    dsc_sample_doc: dict[str, Any],
) -> None:
    errors: list[str] = []
    api_version = dsc_sample_doc.get("apiVersion", "(unknown)")
    for key in sorted(dsc_keys):
        if not _dsc_spec_key_allowed_by_sample_policy(key, policy, dsc_sample_doc):
            errors.append(
                f"smoke DSC key '{key}' is Removed or missing in operator sample policy ({api_version})"
            )
    if errors:
        raise AppError(
            f"Operator DSC sample validation failed ({len(errors)}): {'; '.join(errors)}",
            2,
        )


def validate_smoke_managed_keys_for_operator_version(
    managed_keys: set[str],
    operator_version: str,
    *,
    operator_git_ref: str = "",
    components_override: str = "",
    explicit_sample_path: str = "",
    fetch_text_fn: Callable[[str], str] | None = None,
) -> list[str]:
    if not managed_keys or not (operator_version or "").strip():
        return []
    policy = resolve_operator_dsc_policy_for_version(
        operator_version,
        components_override=components_override,
        operator_git_ref=operator_git_ref,
        explicit_sample_path=explicit_sample_path,
        fetch_text_fn=fetch_text_fn,
    )
    if fetch_text_fn is not None:
        ctx = resolve_operator_dsc_sample(
            operator_version,
            operator_git_ref=operator_git_ref,
            explicit_sample_path=explicit_sample_path,
            fetch_text_fn=fetch_text_fn,
        )
    else:
        ctx = _cached_operator_sample(
            operator_version.strip(),
            (operator_git_ref or "").strip(),
            (explicit_sample_path or "").strip(),
        )
    validate_dsc_keys_supported_by_sample_policy(managed_keys, policy, ctx.dsc_sample_doc)
    return [
        f"operatorGitRef={ctx.operator_git_ref}",
        f"dscSamplePath={ctx.dsc_sample_path}",
        f"dscSampleUrl={ctx.dsc_sample_url}",
        f"dscSampleDiscovery={ctx.sample_discovery or '(preloaded)'}",
        f"dscApiVersion={ctx.dsc_sample_doc.get('apiVersion', '(unknown)')}",
        f"validatedSmokeDscKeys={len(managed_keys)}",
    ]
