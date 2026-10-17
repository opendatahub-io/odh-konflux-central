"""Resolve odh-dashboard git repo/ref from installed operator version."""

from __future__ import annotations

import os
import re
from dataclasses import dataclass

from suite.component_version_gate import normalize_version_for_enablement

_UPSTREAM_DASHBOARD_REPO = "https://github.com/opendatahub-io/odh-dashboard.git"
_RHDS_DASHBOARD_REPO = "https://github.com/red-hat-data-services/odh-dashboard.git"
_RHOAI_RELEASE_BRANCH_RE = re.compile(r"^rhoai-\d+\.\d+(?:-ea\.\d+)?$")
_EA_RELEASE_RE = re.compile(r"^(\d+)\.(\d+)(?:\.\d+)?-ea\.(\d+)$")


@dataclass(frozen=True)
class DashboardGitSource:
    repo: str
    ref: str


def _branch_prefix_for_product(product: str) -> str:
    if (product or "").strip().lower() == "odh":
        return "odh"
    return "rhoai"


def resolve_dashboard_source_ref(
    operator_version: str,
    *,
    catalog_ref: str = "main",
    product: str = "",
) -> str:
    """Map probed RHOAI/ODH version to odh-dashboard release branch (e.g. rhoai-3.4)."""
    override = os.environ.get("DASHBOARD_SOURCE_REF_OVERRIDE", "").strip()
    if override:
        return override

    fallback = (catalog_ref or "main").strip() or "main"
    ver = (operator_version or "").strip()
    if ver in {"", "(unknown)", "n/a"}:
        return fallback

    ea_match = _EA_RELEASE_RE.match(ver)
    if ea_match:
        major, minor, ea = ea_match.group(1), ea_match.group(2), ea_match.group(3)
        prefix = _branch_prefix_for_product(product)
        return f"{prefix}-{major}.{minor}-ea.{ea}"

    compare_ver, is_numeric = normalize_version_for_enablement(ver)
    if not is_numeric or not compare_ver:
        return fallback

    parts = compare_ver.split(".")
    if len(parts) < 2 or not parts[0].isdigit() or not parts[1].isdigit():
        return fallback

    prefix = _branch_prefix_for_product(product)
    return f"{prefix}-{parts[0]}.{parts[1]}"


def resolve_dashboard_git_source(
    operator_version: str,
    *,
    catalog_repo: str,
    catalog_ref: str = "main",
    product: str = "",
) -> DashboardGitSource:
    """Resolve clone URL and branch for dashboard Cypress."""
    repo_override = os.environ.get("DASHBOARD_SOURCE_REPO_OVERRIDE", "").strip()
    repo = repo_override or (catalog_repo or "").strip() or _UPSTREAM_DASHBOARD_REPO
    ref = resolve_dashboard_source_ref(
        operator_version,
        catalog_ref=catalog_ref,
        product=product,
    )
    if not repo_override and _RHOAI_RELEASE_BRANCH_RE.match(ref):
        repo = _RHDS_DASHBOARD_REPO
    return DashboardGitSource(repo=repo, ref=ref)
