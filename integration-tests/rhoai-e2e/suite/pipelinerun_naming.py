"""Build Konflux PipelineRun ``metadata.generateName`` prefixes for rhoai-e2e triggers."""

from __future__ import annotations

import re
from datetime import datetime, timezone
from pathlib import PurePosixPath

from suite.constants import product_installs_operator
from suite.its_trigger_params import CLUSTER_SOURCE_EPHC, is_external_cluster_source

_E2E_PLR_PREFIX = "e2e"
_RHOAI_E2E_RESOURCE_PREFIX = "rhoai-e2e"
_DEFAULT_PLR_GENERATE_PREFIX = f"{_E2E_PLR_PREFIX}-"
_DNS_LABEL_RE = re.compile(r"^[a-z0-9]([-a-z0-9]*[a-z0-9])?$")
_RHOAI_VERSION_TAIL_RE = re.compile(r"^v?(\d+)[.-](\d+)(?:-(.*))?$", re.IGNORECASE)
_PLACEHOLDER_VERSION_RE = re.compile(
    r"(unspecified|latest|\(default\)|\bdefault\b|\bn/a\b)",
    re.IGNORECASE,
)
_GENERATE_PREFIX_MAX_LEN = 63


def _join_prefix_segments(segments: list[str]) -> str:
    return "-".join(segments) + "-"


def _fit_generate_prefix(
    *,
    head: list[str],
    middle: list[str],
    tail: list[str],
    user_seg: str,
    its_profile_seg: str,
    gates_seg: str,
    version_seg: str,
    product_seg: str,
) -> str:
    """Prefer descriptive middle segments; drop version, then product, before minimal fallback."""
    attempts: list[list[str]] = [middle]
    if version_seg and version_seg in middle:
        attempts.append([s for s in middle if s != version_seg])
    if product_seg and product_seg in middle:
        attempts.append([s for s in middle if s not in {product_seg, version_seg}])
    for middle_attempt in attempts:
        prefix = _join_prefix_segments(head + middle_attempt + tail)
        if len(prefix) <= _GENERATE_PREFIX_MAX_LEN:
            return prefix
    if its_profile_seg and gates_seg:
        short = _join_prefix_segments([_E2E_PLR_PREFIX, f"its-{its_profile_seg}", *tail])
        if len(short) <= _GENERATE_PREFIX_MAX_LEN:
            return short
    if user_seg and gates_seg:
        short = _join_prefix_segments([_E2E_PLR_PREFIX, f"cli-{user_seg}", *tail])
        if len(short) <= _GENERATE_PREFIX_MAX_LEN:
            return short
    prefix = _join_prefix_segments(head + middle + tail)
    if len(prefix) > _GENERATE_PREFIX_MAX_LEN:
        prefix = prefix[:_GENERATE_PREFIX_MAX_LEN]
        if not prefix.endswith("-"):
            prefix = prefix.rstrip("-.") + "-"
    return prefix


def _sanitize_segment(raw: str, *, max_len: int = 24) -> str:
    """DNS-1123 subdomain label for one name segment."""
    text = (raw or "").strip().lower()
    if not text:
        return ""
    if "@" in text:
        text = text.split("@", 1)[0]
    text = text.replace(".", "-").replace("_", "-")
    text = re.sub(r"[^a-z0-9-]+", "-", text)
    text = re.sub(r"-+", "-", text).strip("-")
    if not text:
        return ""
    text = text[:max_len].strip("-")
    if not text or not _DNS_LABEL_RE.match(text):
        return ""
    return text


def _version_name_segment(version_compact: str) -> str:
    """Allow ``3.5`` / ``3.5ea2`` dots in compact version tokens."""
    text = (version_compact or "").strip().lower()
    if not text:
        return ""
    text = re.sub(r"[^a-z0-9.-]+", "", text)[:16].strip(".-")
    if not text or not re.match(r"^[a-z0-9]", text):
        return ""
    return text


def version_placeholder(version: str) -> bool:
    text = (version or "").strip()
    if not text:
        return True
    return bool(_PLACEHOLDER_VERSION_RE.search(text))


def compact_version_for_name(version: str) -> str:
    """Compact RHOAI version for PipelineRun names (``3.5``, ``rhoai-v3-5-ea-2`` → ``3.5ea2``)."""
    text = (version or "").strip().lower()
    if version_placeholder(text):
        return ""
    if text.startswith("rhoai-v"):
        text = text[len("rhoai-v") :]
    elif text.startswith("rhoai-"):
        text = text[len("rhoai-") :]
    text = text.strip()
    if not text:
        return ""
    rc_match = re.match(r"^(\d+)\.(\d+)(?:\.(\d+))?-rc\.(\d+)$", text)
    if rc_match:
        major, minor, patch, rc = rc_match.groups()
        base = f"{major}.{minor}" if not patch or patch == "0" else f"{major}.{minor}.{patch}"
        return f"{base}rc{rc}"[:16]

    ea_match = re.match(r"^(\d+)\.(\d+)(?:\.(\d+))?-ea\.(\d+)$", text)
    if ea_match:
        major, minor, patch, ea = ea_match.groups()
        base = f"{major}.{minor}" if not patch or patch == "0" else f"{major}.{minor}.{patch}"
        return f"{base}ea{ea}"[:16]

    match = _RHOAI_VERSION_TAIL_RE.match(text)
    if not match:
        compact = re.sub(r"[^a-z0-9]+", "", text)
        return compact[:16] if compact else ""
    major, minor, tail = match.group(1), match.group(2), (match.group(3) or "").strip()
    base = f"{major}.{minor}"
    if not tail:
        return base
    suffix = re.sub(r"[^a-z0-9]+", "", tail)
    return f"{base}{suffix}"[:16] if suffix else base


def gates_segment_for_name(tests_csv: str) -> str:
    """Join gate ids with hyphens; omit ``bvt`` unless it is the only gate."""
    parts = [p.strip().lower() for p in (tests_csv or "").split(",") if p.strip()]
    if not parts:
        return ""
    if parts == ["bvt"]:
        return "bvt"
    return "-".join(p for p in parts if p != "bvt")


def component_segment_for_name(components_csv: str) -> str:
    """Include a component token only when exactly one id is requested (not ``all``)."""
    parts = [p.strip().lower() for p in (components_csv or "").split(",") if p.strip()]
    if len(parts) != 1 or parts[0] == "all":
        return ""
    return _sanitize_segment(parts[0], max_len=24)


def cluster_segment_for_name(
    *,
    cluster_source: str,
    cluster_label: str,
    target_type: str,
) -> str:
    """Cluster/target token for the name; omit when unknown or stub without external cluster."""
    label = _sanitize_segment(cluster_label, max_len=20)
    if label:
        return label
    source = (cluster_source or "").strip()
    if is_external_cluster_source(source):
        # Tenant Secret names (e.g. rhoai-e2e-kubeconfig-*) are not cluster ids.
        if re.match(r"^rhoai-e2e-kubeconfig", source, re.IGNORECASE):
            return ""
        return _sanitize_segment(source, max_len=20)
    if source == CLUSTER_SOURCE_EPHC or target_type in ("ephc", "ehc"):
        return "ephc"
    return ""


def user_segment_for_name(run_owner: str) -> str:
    return _sanitize_segment(run_owner, max_len=12)


def its_profile_segment_for_name(its_profile: str) -> str:
    """Sanitize caller-supplied ITS profile token for ``e2e-its-{profile}-…`` prefixes."""
    return _sanitize_segment(its_profile, max_len=20)


_ITS_PIPELINERUN_TEMPLATE_STEM = f"{_RHOAI_E2E_RESOURCE_PREFIX}-pipelinerun"


def its_profile_from_scenario_name(scenario_name: str) -> str:
    """Use ``IntegrationTestScenario.metadata.name`` (minus ``rhoai-e2e-``) as the ITS profile."""
    name = (scenario_name or "").strip().lower()
    scenario_prefix = f"{_RHOAI_E2E_RESOURCE_PREFIX}-"
    if name.startswith(scenario_prefix):
        name = name[len(scenario_prefix) :]
    return its_profile_segment_for_name(name)


def its_profile_from_pipelinerun_template(path: str) -> str:
    """
    Derive ITS profile from ``resolverRef.pathInRepo`` basename.

    ``…/rhoai-e2e-pipelinerun-ocp422.yaml`` → ``ocp422``; default wrapper → ``""``.
    """
    stem = PurePosixPath((path or "").strip().replace("\\", "/")).stem.lower()
    template_prefix = f"{_ITS_PIPELINERUN_TEMPLATE_STEM}-"
    if stem.startswith(template_prefix):
        return its_profile_segment_for_name(stem[len(template_prefix) :])
    if stem == _ITS_PIPELINERUN_TEMPLATE_STEM:
        return ""
    return its_profile_segment_for_name(stem)


def is_rhoai_e2e_pipelinerun_name(name: str) -> bool:
    """True for rhoai-e2e Konflux test PipelineRuns (``e2e-cli-*``, ``e2e-its-*``, …)."""
    text = (name or "").strip()
    if not text:
        return False
    return text.startswith(f"{_E2E_PLR_PREFIX}-")


def default_pipelinerun_generate_prefix() -> str:
    """Fallback ``generateName`` prefix when the runner has not set one yet."""
    return _DEFAULT_PLR_GENERATE_PREFIX


def build_rhoai_e2e_generate_prefix(
    *,
    product: str,
    version: str = "",
    cluster_source: str = "",
    cluster_label: str = "",
    target_type: str = "",
    tests_csv: str = "",
    components_csv: str = "",
    run_owner: str = "",
    its_profile: str = "",
) -> str:
    """
    Return a ``generateName`` prefix ending with ``-``.

    CLI pattern: ``e2e-cli-{user}-{cluster?}-{product?}-{version?}-{gates}-{component?}-``
    ITS pattern (``its_profile`` set): ``e2e-its-{profile}-{product?}-{version?}-{gates}-{component?}-``

    ``existing`` product is omitted; unknown version/cluster segments are dropped.
    A component token is added only when ``components_csv`` is a single id (not ``all``).
    For Integration Service runs, prefer ``build_rhoai_e2e_its_generate_prefix`` or
    ``build_its_generate_prefix_for_snapshot``.
    """
    segments: list[str] = [_E2E_PLR_PREFIX]
    its_profile_seg = its_profile_segment_for_name(its_profile)
    user_seg = ""
    if its_profile_seg:
        segments.append(f"its-{its_profile_seg}")
    else:
        user_seg = user_segment_for_name(run_owner)
        if user_seg:
            segments.append(f"cli-{user_seg}")

    cluster_seg = cluster_segment_for_name(
        cluster_source=cluster_source,
        cluster_label=cluster_label,
        target_type=target_type,
    )
    if cluster_seg and not its_profile_seg:
        segments.append(cluster_seg)
    head = segments[:]

    product_seg = ""
    version_seg = ""
    prod = (product or "").strip().lower()
    middle: list[str] = []

    if product_installs_operator(prod):
        product_seg = _sanitize_segment(prod, max_len=8)
        if product_seg:
            middle.append(product_seg)
        version_seg = _version_name_segment(compact_version_for_name(version))
        if version_seg:
            middle.append(version_seg)

    gates_seg = gates_segment_for_name(tests_csv)
    sanitized_gates_seg = _sanitize_segment(gates_seg, max_len=24)
    component_seg = component_segment_for_name(components_csv)
    tail: list[str] = []
    if sanitized_gates_seg:
        tail.append(sanitized_gates_seg)
    if component_seg:
        tail.append(component_seg)

    return _fit_generate_prefix(
        head=head,
        middle=middle,
        tail=tail,
        user_seg=user_seg,
        its_profile_seg=its_profile_seg,
        gates_seg=sanitized_gates_seg,
        version_seg=version_seg,
        product_seg=product_seg,
    )


def build_rhoai_e2e_its_generate_prefix(
    *,
    its_profile: str,
    product: str = "rhoai",
    version: str = "",
    tests_csv: str = "",
    components_csv: str = "",
) -> str:
    """
    Integration Service ``generateName`` prefix (no ``cli-{user}`` or per-run cluster token).

    ``its_profile`` must be supplied by the caller (ITS manifest / pipelinerun wrapper path).
    Example: ``e2e-its-ocp422-rhoai-3.5ea2-smoke-``.
    """
    return build_rhoai_e2e_generate_prefix(
        its_profile=its_profile,
        product=product,
        version=version,
        tests_csv=tests_csv,
        components_csv=components_csv,
        run_owner="",
    )


def build_its_generate_prefix_for_snapshot(
    *,
    its_profile: str,
    snapshot_labels: dict[str, str] | None = None,
    snapshot_annotations: dict[str, str] | None = None,
    snapshot_json: str = "",
    fbc_component_name: str = "",
    konflux_application: str = "",
    product: str = "rhoai",
    tests_csv: str = "",
    components_csv: str = "",
    rhoai_version_param: str = "",
) -> str:
    """
    ITS prefix with catalog version resolved from Konflux Snapshot metadata (webhook / IS hook).

    Call at PipelineRun create time; the name cannot be changed after the PLR exists.
    """
    from suite.snapshot_catalog_line import resolve_catalog_version_for_naming

    version = resolve_catalog_version_for_naming(
        snapshot_json=snapshot_json,
        fbc_component_name=fbc_component_name,
        resolved_app=konflux_application,
        rhoai_version_param=rhoai_version_param,
        fbc_snapshot_meta={
            "labels": dict(snapshot_labels or {}),
            "annotations": dict(snapshot_annotations or {}),
        },
    )
    return build_rhoai_e2e_its_generate_prefix(
        its_profile=its_profile,
        product=product,
        version=version,
        tests_csv=tests_csv,
        components_csv=components_csv,
    )


_SEMVER_VERSION_RE = re.compile(r"^\d+\.\d+(?:\.\d+)?$")


def diagnostic_version_segment(operator_version: str) -> str:
    """Compact installed CSV / trigger version for diagnostic artifact names."""
    ver = (operator_version or "").strip()
    if not ver or ver in ("(unknown)", "(see pipeline run logs)"):
        return ""
    if _SEMVER_VERSION_RE.fullmatch(ver):
        return _version_name_segment(ver)
    compact = compact_version_for_name(ver)
    if compact:
        return _version_name_segment(compact)
    cleaned = re.sub(r"[^a-z0-9.-]+", "", ver.lower()).strip(".-")
    return _version_name_segment(cleaned)


def diagnostic_timestamp_segment(since_time: str) -> str:
    """RFC3339-ish UTC stamp for diagnostic log filenames (``2026-06-24T112510Z``)."""
    ts = (since_time or "").strip()
    try:
        if not ts:
            raise ValueError("empty since_time")
        if ts.endswith("Z"):
            dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
        else:
            dt = datetime.fromisoformat(ts)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H%M%SZ")
    except (ValueError, TypeError):
        return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H%M%SZ")


def build_diagnostic_artifact_log_name(
    *,
    since_time: str,
    installed_product: str,
    operator_version: str = "",
    cluster_label: str = "",
    pipeline_product: str = "",
) -> str:
    """
    Return ``{product}-{version?}-{cluster?}-diagnostic-{datetime}.log``.

    Uses cluster-installed product/version (not test-only PRODUCT intent).
    """
    product_seg = _sanitize_segment(installed_product, max_len=8)
    if not product_seg or product_seg == "unknown":
        raw = (pipeline_product or "").strip().lower()
        if product_installs_operator(raw):
            product_seg = _sanitize_segment(raw, max_len=8) or "unknown"
        else:
            product_seg = "unknown"

    segments: list[str] = [product_seg]
    ver_seg = diagnostic_version_segment(operator_version)
    if ver_seg:
        segments.append(ver_seg)
    cluster_seg = _sanitize_segment(cluster_label, max_len=20)
    if cluster_seg:
        segments.append(cluster_seg)
    stamp = diagnostic_timestamp_segment(since_time)
    return f"{'-'.join(segments)}-diagnostic-{stamp}.log"
