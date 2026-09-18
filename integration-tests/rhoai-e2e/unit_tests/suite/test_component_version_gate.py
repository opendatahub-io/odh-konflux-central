#!/usr/bin/env python3
"""Unit tests for component version gates (Jenkins minRhoai/maxRhoai parity)."""

from __future__ import annotations

import os
import unittest
from unittest.mock import patch

from suite.component_catalog_models import SmokeComponent  # noqa: E402
from suite.component_version_gate import (  # noqa: E402
    component_enabled_for_version,
    normalize_version_for_enablement,
    resolve_operator_version_for_gates,
)

def _comp(*, cid: str, min_rhoai: str | None = None, max_rhoai: str | None = None) -> SmokeComponent:
    return SmokeComponent(
        id=cid,
        description="",
        pytest_marker="smoke",
        phase_markers={"smoke": "smoke", "tier1": "tier1"},
        pytest_extra_args="-svv",
        tests_subdir="tests/foo/",
        requires_minimal_deps=False,
        setup_dependencies_args="",
        artifact_prefix=f"{cid}-smoke",
        min_rhoai=min_rhoai,
        max_rhoai=max_rhoai,
    )

class NormalizeVersionTest(unittest.TestCase):
    def test_ea_strips_suffix(self) -> None:
        self.assertEqual(normalize_version_for_enablement("3.5.0-ea.2"), ("3.5.0", True))

    def test_stable_numeric(self) -> None:
        self.assertEqual(normalize_version_for_enablement("3.4.1"), ("3.4.1", True))

class ComponentVersionGateTest(unittest.TestCase):
    def test_ogx_requires_35(self) -> None:
        comp = _comp(cid="ogx", min_rhoai="3.5")
        self.assertFalse(component_enabled_for_version(comp, "3.4.0").enabled)
        self.assertTrue(component_enabled_for_version(comp, "3.5.0-ea.2").enabled)

    def test_llama_stack_max_34(self) -> None:
        comp = _comp(cid="llama_stack", max_rhoai="3.4")
        self.assertFalse(component_enabled_for_version(comp, "3.5.0-ea.2").enabled)
        self.assertTrue(component_enabled_for_version(comp, "3.4.1").enabled)

    def test_maas_min_33(self) -> None:
        comp = _comp(cid="maas_billing", min_rhoai="3.3")
        self.assertFalse(component_enabled_for_version(comp, "3.2.9").enabled)
        self.assertTrue(component_enabled_for_version(comp, "3.3.0").enabled)

    def test_unknown_version_does_not_gate(self) -> None:
        comp = _comp(cid="ogx", min_rhoai="3.5")
        self.assertTrue(component_enabled_for_version(comp, "").enabled)

    def test_no_bounds_always_enabled(self) -> None:
        comp = _comp(cid="workbenches")
        self.assertTrue(component_enabled_for_version(comp, "2.0.0").enabled)

class ResolveOperatorVersionTest(unittest.TestCase):
    def test_reads_operator_version_from_plan_json(self) -> None:
        import json
        import tempfile
        from pathlib import Path

        with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as tf:
            json.dump({"operator_version": "3.5.0-ea.2", "components": []}, tf)
            plan_path = tf.name
        try:
            with patch.dict(
                os.environ,
                {
                    "COMPONENT_TEST_PLAN_JSON": plan_path,
                    "OPERATOR_VERSION": "",
                    "OLMINSTALL_TESTS_VERSION_OVERRIDE": "",
                },
                clear=False,
            ):
                self.assertEqual(resolve_operator_version_for_gates(), "3.5.0-ea.2")
        finally:
            Path(plan_path).unlink(missing_ok=True)

