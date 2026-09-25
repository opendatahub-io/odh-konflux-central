"""ITS / PipelineRun trigger params: CLUSTER_SOURCE and informational version labels."""

from __future__ import annotations

import os
import re

from suite.constants import is_test_only_product

# EPHC: Ephemeral Hosted Cluster in OpenShift CI (Prow).
CLUSTER_SOURCE_EPHC = "EPHC"
DEFAULT_SUFFIX = " (default)"
NOT_APPLICABLE = "n/a"

_SECRET_NAME_RE = re.compile(r"^[a-z0-9]([-a-z0-9]*[a-z0-9])?$")
_KNOWN_POOLED_EXTERNAL_SUFFIX_RE = re.compile(r"^(?:rh-nightly-pm|ods-qe-psi-\d+)$")
_RHOAI_APP_VERSION_RE = re.compile(r"^rhoai-v(\d+)-(\d+)")
_RHOAI_APP_CATALOG_LINE_RE = re.compile(
    r"^rhoai-v(\d+)-(\d+)(?:-ea-(\d+))?(?:-rc-(\d+))?$",
    re.IGNORECASE,
)
_STABLE_CHANNEL_VERSION_RE = re.compile(r"^stable-(\d+\.\d+)$")
_RHOAI_FBC_OCP_RE = re.compile(r"ocp-(\d)(\d{2})\b", re.IGNORECASE)
_OCP_MINOR_PREFIX_RE = re.compile(r"^(\d+\.\d+)")
_PLACEHOLDER_OCP_RE = re.compile(
    r"(unspecified|latest|\(default\)|\bdefault\b|\bn/a\b)",
    re.IGNORECASE,
)


def ocp_install_prefix(value: str) -> str:
    """Return OCP minor for ``OCP_VERSION_PREFIX``, or empty for ITS UI placeholders."""
    text = (value or "").strip()
    if not text or _PLACEHOLDER_OCP_RE.search(text):
        return ""
    match = _OCP_MINOR_PREFIX_RE.match(text)
    return match.group(1) if match else ""


def is_ephemeral_hosted_cluster_source(value: str) -> bool:
    """True only for explicit EPHC sentinel (empty/unset is not EPHC)."""
    return (value or "").strip() == CLUSTER_SOURCE_EPHC


def is_external_cluster_source(value: str) -> bool:
    """True when CLUSTER_SOURCE is a tenant Secret name (not EPHC or unset)."""
    text = (value or "").strip()
    return bool(text) and text != CLUSTER_SOURCE_EPHC


def _external_kubeconfig_secret_suffix(value: str) -> str:
    secret = (value or "").strip()
    for prefix in ("rhoai-e2e-kubeconfig-", "kubeconfig-"):
        if secret.startswith(prefix):
            return secret[len(prefix) :].strip("-")
    return secret


def is_known_shared_external_cluster_secret(value: str) -> bool:
    """True for ITS-defined shared QE cluster secrets (exact suffix, not spoofable prefixes)."""
    if not is_external_cluster_source(value):
        return False
    suffix = _external_kubeconfig_secret_suffix(value)
    return bool(suffix) and _KNOWN_POOLED_EXTERNAL_SUFFIX_RE.fullmatch(suffix) is not None


def is_pooled_external_cluster_source(value: str) -> bool:
    """True when pipeline marks a shared external cluster as pooled (not personal).

    Classification is pipeline-controlled via CLUSTER_POOL_MARKER env var, not derived
    from external Secret name. This prevents personal/user-provided clusters from
    receiving pooled-only gate skips. Requires both marker AND cluster-prep flag.
    """
    if not is_external_cluster_source(value):
        return False
    # Environment flag marks whether cluster prep runs in dep operators (shared infrastructure).
    if (os.environ.get("RUN_COMPONENT_CLUSTER_PREP_IN_DEP_OPERATORS") or "").strip().lower() not in (
        "1",
        "true",
        "yes",
    ):
        return False
    # Pipeline-controlled marker explicitly designates a pooled cluster (not spoofable by secret name).
    return (os.environ.get("CLUSTER_POOL_MARKER") or "").strip().lower() in (
        "1",
        "true",
        "yes",
    )


def external_kubeconfig_secret_name(value: str) -> str:
    """Return tenant Secret name for external clusters; empty for EPHC or unset."""
    text = (value or "").strip()
    return text if is_external_cluster_source(text) else ""


def resolve_cluster_source_for_trigger(*, product: str, external_secret: str) -> str:
    """Value for ITS/pipeline CLUSTER_SOURCE from rhoai_e2e.py trigger inputs."""
    secret = (external_secret or "").strip()
    if secret:
        return secret
    if (product or "").strip().lower() in ("rhoai", "odh"):
        return CLUSTER_SOURCE_EPHC
    return ""


def validate_cluster_source(value: str) -> None:
    """Reject invalid CLUSTER_SOURCE before oc apply."""
    text = (value or "").strip()
    if not text or is_ephemeral_hosted_cluster_source(text):
        return
    if not _SECRET_NAME_RE.fullmatch(text):
        raise ValueError(
            f"CLUSTER_SOURCE must be {CLUSTER_SOURCE_EPHC!r} or a valid Kubernetes Secret name; got {text!r}"
        )


def rhoai_version_label_from_app(resolved_app: str) -> str:
    """Return Konflux application name for UI when it matches ``rhoai-v*``."""
    text = (resolved_app or "").strip()
    if _RHOAI_APP_VERSION_RE.match(text):
        return text
    return ""


def rhoai_version_from_app(resolved_app: str) -> str:
    """Extract ``x.y`` from a Konflux application name like ``rhoai-v3-5-ea-1``."""
    match = _RHOAI_APP_VERSION_RE.match((resolved_app or "").strip())
    if match:
        return f"{match.group(1)}.{match.group(2)}"
    return ""


def rhoai_catalog_line_from_konflux_app(resolved_app: str) -> str:
    """Map Konflux app names like ``rhoai-v3-5-ea-2`` to catalog lines (``3.5-ea.2``)."""
    match = _RHOAI_APP_CATALOG_LINE_RE.match((resolved_app or "").strip().lower())
    if not match:
        return rhoai_version_from_app(resolved_app)
    major, minor, ea, rc = match.group(1), match.group(2), match.group(3), match.group(4)
    base = f"{major}.{minor}"
    if ea:
        return f"{base}-ea.{ea}"
    if rc:
        return f"{base}-rc.{rc}"
    return base


def rhoai_version_from_channel(update_channel: str) -> str:
    match = _STABLE_CHANNEL_VERSION_RE.match((update_channel or "").strip())
    return match.group(1) if match else ""


def ocp_version_from_rhoai_fbc_name(rhoai_fbc_name: str) -> str:
    """Infer catalog OCP minor from names like ``rhoai-fbc-fragment-ocp-421`` → ``4.21``."""
    match = _RHOAI_FBC_OCP_RE.search((rhoai_fbc_name or "").strip())
    if match:
        return f"{match.group(1)}.{match.group(2)}"
    return ""


def with_default_suffix(value: str, *, explicit: bool) -> str:
    text = (value or "").strip()
    if not text:
        return ""
    return text if explicit else f"{text}{DEFAULT_SUFFIX}"


def resolve_rhoai_version_display(
    *,
    product: str,
    cli_version: str,
    resolved_app: str,
    update_channel: str,
    explicit_cli: bool,
) -> str:
    prod = (product or "").strip().lower()
    if prod != "rhoai":
        return NOT_APPLICABLE if is_test_only_product(prod) else ""
    app_label = rhoai_version_label_from_app(resolved_app)
    if app_label:
        return app_label if explicit_cli else with_default_suffix(app_label, explicit=False)
    if explicit_cli and (cli_version or "").strip():
        return (cli_version or "").strip()
    inferred = (
        (cli_version or "").strip()
        or rhoai_version_from_app(resolved_app)
        or rhoai_version_from_channel(update_channel)
    )
    if not inferred:
        return f"unspecified{DEFAULT_SUFFIX}"
    return with_default_suffix(inferred, explicit=False)


def resolve_ocp_version_display(
    *,
    product: str,
    cluster_source: str,
    cli_ocp: str,
    explicit_cli: bool,
    rhoai_fbc_name: str = "",
) -> str:
    prod = (product or "").strip().lower()
    source = (cluster_source or "").strip()
    catalog_ocp = ocp_version_from_rhoai_fbc_name(rhoai_fbc_name)

    if explicit_cli and (cli_ocp or "").strip():
        return (cli_ocp or "").strip()

    if is_external_cluster_source(source):
        if catalog_ocp:
            return with_default_suffix(catalog_ocp, explicit=False)
        return NOT_APPLICABLE

    if is_test_only_product(prod) and not is_ephemeral_hosted_cluster_source(source):
        if catalog_ocp:
            return with_default_suffix(catalog_ocp, explicit=False)
        return NOT_APPLICABLE

    if prod not in ("rhoai", "odh") and not is_ephemeral_hosted_cluster_source(source):
        return NOT_APPLICABLE

    if is_ephemeral_hosted_cluster_source(source) or prod in ("rhoai", "odh"):
        return f"latest{DEFAULT_SUFFIX}"
    return NOT_APPLICABLE


def resolve_rhoai_fbc_image(*, fbc_image: str, explicit_cli: bool) -> str:
    """Informational RHOAI FBC catalog pullspec for Konflux PipelineRun UI."""
    text = (fbc_image or "").strip()
    if not text:
        return f"unspecified{DEFAULT_SUFFIX}"
    return with_default_suffix(text, explicit=explicit_cli)


def resolve_version_display_params(
    *,
    product: str,
    cli_version: str,
    resolved_app: str,
    update_channel: str,
    cluster_source: str,
    cli_ocp: str,
    ocp_explicit: bool,
    rhoai_fbc_name: str = "",
    fbc_image: str = "",
    fbc_image_explicit: bool = False,
) -> dict[str, str]:
    """Informational RHOAI/OCP/FBC params for Konflux PipelineRun UI."""
    return {
        "RHOAI_VERSION": resolve_rhoai_version_display(
            product=product,
            cli_version=cli_version,
            resolved_app=resolved_app,
            update_channel=update_channel,
            explicit_cli=bool((cli_version or "").strip()),
        ),
        "OCP_VERSION": resolve_ocp_version_display(
            product=product,
            cluster_source=cluster_source,
            cli_ocp=cli_ocp,
            explicit_cli=ocp_explicit,
            rhoai_fbc_name=rhoai_fbc_name,
        ),
        "RHOAI_FBC_IMAGE": resolve_rhoai_fbc_image(
            fbc_image=fbc_image,
            explicit_cli=fbc_image_explicit,
        ),
    }


