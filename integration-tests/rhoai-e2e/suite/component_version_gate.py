"""RHOAI/ODH version enablement for component smoke (Jenkins getComponentsEnablementPerVersion parity)."""

from __future__ import annotations

import os
import re
from dataclasses import dataclass

from suite.component_catalog_models import SmokeComponent

_EA_VERSION_RE = re.compile(r"^(\d+\.\d+(?:\.\d+)?)-ea\.\d+$")
_NUMERIC_VERSION_RE = re.compile(r"^\d+\.\d+(?:\.\d+)?$")


@dataclass(frozen=True)
class VersionGateResult:
    enabled: bool
    reason: str = ""


def normalize_version_for_enablement(version: str) -> tuple[str, bool]:
    """Return (compare_version, is_numeric_release) for enablement checks."""
    raw = (version or "").strip()
    if not raw:
        return "", False
    m = _EA_VERSION_RE.match(raw)
    if m:
        return m.group(1), True
    if _NUMERIC_VERSION_RE.match(raw):
        return raw, True
    return raw, False


def _version_segments(version: str) -> tuple[int, ...]:
    out: list[int] = []
    for part in version.split("."):
        m = re.match(r"^(\d+)", part.strip())
        if m:
            out.append(int(m.group(1)))
    return tuple(out)


def _compare_version_strings(left: str, right: str) -> int:
    """Return -1 if left < right, 0 if equal, 1 if left > right."""
    a = _version_segments(left)
    b = _version_segments(right)
    width = max(len(a), len(b))
    a_pad = a + (0,) * (width - len(a))
    b_pad = b + (0,) * (width - len(b))
    if a_pad < b_pad:
        return -1
    if a_pad > b_pad:
        return 1
    return 0


def _effective_version_for_max(test_version: str, max_rhoai: str) -> str:
    test_parts = test_version.split(".")
    max_parts = max_rhoai.split(".")
    if len(max_parts) < len(test_parts):
        return ".".join(test_parts[: len(max_parts)])
    return test_version


def rhoai_version_at_least(operator_version: str, minimum: str) -> bool:
    """True when ``operator_version`` is a numeric/EA release >= ``minimum`` (e.g. 3.5)."""
    compare_ver, is_numeric = normalize_version_for_enablement(operator_version)
    if not is_numeric:
        return False
    return _compare_version_strings(compare_ver, minimum) >= 0


def component_enabled_for_version(
    comp: SmokeComponent,
    operator_version: str,
    *,
    product: str = "",
) -> VersionGateResult:
    """Mirror Jenkins ComponentsTestsLoader.getComponentsEnablementPerVersion for one component."""
    if not comp.enabled:
        return VersionGateResult(False, f"component {comp.id} is disabled in catalog")

    min_rhoai = (comp.min_rhoai or "").strip()
    max_rhoai = (comp.max_rhoai or "").strip()
    if not min_rhoai and not max_rhoai:
        return VersionGateResult(True, "")

    ver = (operator_version or "").strip()
    if not ver:
        return VersionGateResult(True, "")

    compare_ver, is_numeric = normalize_version_for_enablement(ver)
    if not is_numeric:
        return VersionGateResult(True, "")

    enabled = True
    if min_rhoai and _compare_version_strings(compare_ver, min_rhoai) < 0:
        enabled = False
    if enabled and max_rhoai:
        effective = _effective_version_for_max(compare_ver, max_rhoai)
        if _compare_version_strings(effective, max_rhoai) > 0:
            enabled = False

    if enabled:
        return VersionGateResult(True, "")

    bounds: list[str] = []
    if min_rhoai:
        bounds.append(f"minRhoai={min_rhoai}")
    if max_rhoai:
        bounds.append(f"maxRhoai={max_rhoai}")
    product_note = f", product={product}" if product else ""
    return VersionGateResult(
        False,
        (
            f"Component {comp.id} is not enabled for installed RHOAI version {ver!r} "
            f"({', '.join(bounds)}{product_note})"
        ),
    )


def probe_operator_version_from_cluster() -> str:
    """Best-effort CSV version from KUBECONFIG (external or staged tests-shared)."""
    kc = os.environ.get("KUBECONFIG", "").strip()
    op_ns = (os.environ.get("OPERATOR_NAMESPACE", "").strip() or "redhat-ods-operator")
    op_name = (os.environ.get("OPERATOR_NAME", "") or "rhods-operator").strip()
    if not kc:
        return ""
    from install.install_and_verify import pick_succeeded_csv_version

    ver = pick_succeeded_csv_version(op_ns, op_name, timeout=20)
    return (ver or "").strip()


def resolve_operator_version_for_gates() -> str:
    """Version used for component gates and optional tests-image override."""
    override = os.environ.get("OLMINSTALL_TESTS_VERSION_OVERRIDE", "").strip()
    if override:
        print(f"Using OLMINSTALL_TESTS_VERSION_OVERRIDE={override!r} for version gates")
        return override
    from runners.selection import _load_component_test_plan

    plan = _load_component_test_plan()
    if plan is not None:
        plan_ver = str(plan.get("operator_version", "")).strip()
        if plan_ver and plan_ver != "(unknown)":
            return plan_ver
    ver = os.environ.get("OPERATOR_VERSION", "").strip()
    if ver:
        return ver
    probed = probe_operator_version_from_cluster()
    if probed:
        print(f"Probed operator version {probed!r} for version gates")
    return probed


def version_skip_reason_for_component(comp: SmokeComponent) -> str:
    """Return non-empty reason when the component should skip smoke for the current version."""
    product = os.environ.get("PRODUCT", "").strip().lower()
    operator_version = resolve_operator_version_for_gates()
    result = component_enabled_for_version(comp, operator_version, product=product)
    return result.reason if not result.enabled else ""
