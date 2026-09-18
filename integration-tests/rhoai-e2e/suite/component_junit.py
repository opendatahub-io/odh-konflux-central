#!/usr/bin/env python3
"""Parse component smoke JUnit XML for pass-rate gating between catalog entries."""

from __future__ import annotations

import xml.etree.ElementTree as ET
from pathlib import Path


def _safe_junit_attr(value: str | None) -> int:
    try:
        return int(value or 0)
    except ValueError:
        return 0


def _counts_from_testcases(root: ET.Element) -> dict[str, int] | None:
    """Count failures from ``<testcase>`` elements when present."""
    testcases = root.findall(".//testcase")
    if not testcases:
        return None
    failures = errors = skipped = 0
    for testcase in testcases:
        if testcase.find("failure") is not None:
            failures += 1
        elif testcase.find("error") is not None:
            errors += 1
        elif testcase.find("skipped") is not None:
            skipped += 1
    total = len(testcases)
    passed = total - failures - errors - skipped
    return {
        "total": total,
        "passed": max(0, passed),
        "failures": failures,
        "errors": errors,
        "skipped": skipped,
    }


def _counts_from_suite_attributes(root: ET.Element) -> dict[str, int] | None:
    """Sum ``tests`` / ``failures`` attributes from all ``<testsuite>`` nodes."""
    if root.tag == "testsuite":
        suites = [root]
    elif root.tag == "testsuites":
        suites = root.findall("./testsuite")
    else:
        return None
    if not suites:
        return None
    tests = failures = errors = skipped = 0
    for suite in suites:
        tests += _safe_junit_attr(suite.get("tests"))
        failures += _safe_junit_attr(suite.get("failures"))
        errors += _safe_junit_attr(suite.get("errors"))
        skipped += _safe_junit_attr(suite.get("skipped"))
    passed = tests - failures - errors - skipped
    return {
        "total": tests,
        "passed": max(0, passed),
        "failures": failures,
        "errors": errors,
        "skipped": skipped,
    }


def junit_counts(xml_path: Path) -> dict[str, int] | None:
    """Return per-file test counts, or None if the file is missing or empty."""
    if not xml_path.is_file():
        return None
    try:
        root = ET.parse(xml_path).getroot()
    except ET.ParseError:
        return None
    if root.tag not in ("testsuites", "testsuite"):
        return None
    counts = _counts_from_testcases(root)
    if counts is not None:
        suite_counts = _counts_from_suite_attributes(root)
        if suite_counts and suite_counts["total"] > counts["total"]:
            passed = suite_counts["total"] - suite_counts["failures"] - suite_counts["errors"] - suite_counts["skipped"]
            counts = {
                "total": suite_counts["total"],
                "passed": max(0, passed),
                "failures": suite_counts["failures"],
                "errors": suite_counts["errors"],
                "skipped": suite_counts["skipped"],
            }
    else:
        counts = _counts_from_suite_attributes(root)
    if counts is None:
        return None
    return counts


def junit_pass_rate(xml_path: Path) -> float | None:
    """Return fraction of tests passed (0.0–1.0), or None if the file is missing or empty."""
    counts = junit_counts(xml_path)
    if counts is None or counts["total"] <= 0:
        return None
    return counts["passed"] / counts["total"]


def is_intermediate_cypress_junit(xml_path: Path, artifacts_root: Path) -> bool:
    """True for per-SmokeSet/SanitySet Cypress fragments under *artifacts_root*."""
    try:
        rel = xml_path.relative_to(artifacts_root)
    except ValueError:
        return False
    return any(part.startswith(("SmokeSet", "SanitySet")) for part in rel.parts)
