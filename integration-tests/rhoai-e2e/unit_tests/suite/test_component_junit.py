"""Tests for JUnit counting used by TEST_OUTPUT and pass-rate gating."""

from __future__ import annotations

import tempfile
import unittest
import xml.etree.ElementTree as ET
from pathlib import Path

from suite.component_junit import junit_counts

class ComponentJunitTest(unittest.TestCase):
    def test_counts_from_testcases_when_attributes_under_report(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "nested.xml"
            outer = ET.Element("testsuite", {"name": "root", "tests": "0", "failures": "0"})
            inner = ET.SubElement(
                outer,
                "testsuite",
                {"name": "inner", "tests": "17", "failures": "9"},
            )
            for idx in range(17):
                tc = ET.SubElement(inner, "testcase", {"name": f"t{idx}"})
                if idx < 9:
                    ET.SubElement(tc, "failure").text = "failed"
            root = ET.Element("testsuites")
            root.append(outer)
            ET.ElementTree(root).write(path, encoding="unicode", xml_declaration=True)

            counts = junit_counts(path)
            self.assertIsNotNone(counts)
            assert counts is not None
            self.assertEqual(counts["total"], 17)
            self.assertEqual(counts["failures"], 9)

    def test_prefers_canonical_junit_report_counts(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "flat.xml"
            suite = ET.Element("testsuite", {"name": "Mocha Tests", "tests": "1", "failures": "1"})
            tc = ET.SubElement(suite, "testcase", {"name": "hook"})
            ET.SubElement(tc, "failure").text = "timed out"
            ET.SubElement(suite, "testcase", {"name": "spec-a"})
            ET.SubElement(suite, "testcase", {"name": "spec-b"})
            ET.ElementTree(suite).write(path, encoding="unicode", xml_declaration=True)

            counts = junit_counts(path)
            self.assertIsNotNone(counts)
            assert counts is not None
            self.assertEqual(counts["total"], 3)
            self.assertEqual(counts["failures"], 1)

    def test_zero_tests_returns_counts_not_none(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "zero.xml"
            suite = ET.Element("testsuite", {"name": "Empty", "tests": "0", "failures": "0"})
            ET.ElementTree(suite).write(path, encoding="unicode", xml_declaration=True)

            counts = junit_counts(path)
            self.assertIsNotNone(counts)
            assert counts is not None
            self.assertEqual(counts["total"], 0)
            self.assertEqual(counts["failures"], 0)

