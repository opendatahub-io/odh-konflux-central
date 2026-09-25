"""OGX platform diagnostics when pytest selects zero product tests (JP4: no hollow SUCCESS)."""

from __future__ import annotations

import time
from pathlib import Path
from xml.sax.saxutils import escape, quoteattr

from install.dsc_install import oc_run


_OGX_CRD_NEEDLES = ("ogx.io", "ogxservers", "ogxdistributions")


def _list_ogx_crds() -> list[str]:
    r = oc_run(["get", "crd", "-o", "name"], check=False, capture_output=True, timeout=60)
    if r.returncode != 0:
        return []
    names: list[str] = []
    for line in (r.stdout or "").splitlines():
        name = line.strip().removeprefix("customresourcedefinition.apiextensions.k8s.io/")
        low = name.lower()
        if any(n in low for n in _OGX_CRD_NEEDLES):
            names.append(name)
    return names


def _crd_present(name: str) -> tuple[bool, str]:
    r = oc_run(["get", "crd", name], check=False, capture_output=True, timeout=30)
    if r.returncode == 0:
        return True, f"CRD {name} registered"
    err = (r.stderr or r.stdout or "").strip() or f"oc get crd {name} exit {r.returncode}"
    return False, err


def _dsc_ogx_or_llamastack_state() -> tuple[bool, str]:
    """llamastackoperator must stay Removed for EA OGX."""
    from install.dsc_install import dsc_component_management_state

    try:
        llama = dsc_component_management_state("llamastackoperator")
    except Exception as exc:
        return False, f"llamastackoperator state unreadable: {exc}"
    if llama == "Managed":
        return False, "llamastackoperator is Managed (conflicts with OGX EA.2)"
    return True, f"llamastackoperator={llama or 'absent/Removed'}"


def run_ogx_platform_checks() -> list[tuple[str, bool, str]]:
    """Return (name, passed, detail) for lightweight OGX platform assertions."""
    results: list[tuple[str, bool, str]] = []
    crds = _list_ogx_crds()
    if crds:
        results.append(("ogx_crds_present", True, f"found {', '.join(crds[:5])}"))
    else:
        # Fall back to known EA.2 names when listing fails or CRDs absent.
        any_ok = False
        details: list[str] = []
        for crd in ("ogxservers.ogx.io", "ogxdistributions.ogx.io"):
            ok, detail = _crd_present(crd)
            details.append(detail)
            any_ok = any_ok or ok
        results.append(
            (
                "ogx_crds_present",
                any_ok,
                "; ".join(details) if details else "no ogx.* CRDs found",
            )
        )
    ok, detail = _dsc_ogx_or_llamastack_state()
    results.append(("llamastackoperator_not_managed", ok, detail))
    return results


def write_ogx_platform_junit(artifacts_dir: Path, *, prefix: str = "ogx-smoke") -> Path:
    """Write platform-check JUnit used when vector_stores smoke is deselected on EPHC."""
    checks = run_ogx_platform_checks()
    failures = sum(1 for _, ok, _ in checks if not ok)
    cases: list[str] = []
    for name, ok, detail in checks:
        name_attr = quoteattr(name)
        if ok:
            cases.append(
                f'  <testcase classname="ogx.platform" name={name_attr} time="0.1"/>\n'
            )
            print(f"✓ OGX platform: {name} — {detail}", flush=True)
        else:
            msg = quoteattr(detail)
            cases.append(
                f'  <testcase classname="ogx.platform" name={name_attr} time="0.1">\n'
                f'    <failure message={msg}>{escape(detail)}</failure>\n'
                f"  </testcase>\n"
            )
            print(f"✗ OGX platform: {name} — {detail}", flush=True)
    xml = (
        '<?xml version="1.0" encoding="utf-8"?>\n'
        f'<testsuite name="ogx" tests="{len(checks)}" failures="{failures}" errors="0" skipped="0" '
        f'time="{0.1 * len(checks):g}" timestamp="{time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}">\n'
        f"{''.join(cases)}"
        "</testsuite>\n"
    )
    out = artifacts_dir / f"{prefix}.xml"
    out.write_text(xml, encoding="utf-8")
    print(f"JUnit (ogx platform): {out} ({len(checks) - failures} passed, {failures} failed)", flush=True)
    return out


def should_write_ogx_platform_smoke() -> bool:
    """Always False: do not write platform CRD JUnit as SUCCESS when pytest selected nothing."""
    return False


def ensure_ogx_junit_after_pytest(artifacts_dir: Path, *, prefix: str = "ogx-smoke") -> None:
    """Log platform diagnostics when pytest selected nothing; do not fake SUCCESS JUnit.

    Jenkins/Konflux parity: empty selection must stay a real failure (unreadable/empty
    JUnit → component red) on EPHC and external clusters. Optional CRD checks are
    printed for operators only.
    """
    junit = artifacts_dir / f"{prefix}.xml"
    if junit.is_file():
        text = junit.read_text(encoding="utf-8", errors="replace")
        has_product_tests = (
            "<testcase" in text
            and 'classname="ogx.platform"' not in text
            and 'name="timeout"' not in text
        )
        if has_product_tests:
            return
    print(
        "NOTE: OGX pytest left no product testcases (filter deselected all or empty suite); "
        "not writing platform CRD JUnit as SUCCESS (JP4)",
        flush=True,
    )
    try:
        for name, passed, detail in run_ogx_platform_checks():
            mark = "✓" if passed else "✗"
            print(f"{mark} OGX platform diagnostic: {name} - {detail}", flush=True)
    except Exception as exc:
        print(f"WARN: OGX platform diagnostics failed: {exc}", flush=True)
    if should_write_ogx_platform_smoke():
        write_ogx_platform_junit(artifacts_dir, prefix=prefix)
