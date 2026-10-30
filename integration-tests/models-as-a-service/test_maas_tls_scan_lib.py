#!/usr/bin/env python3
"""Unit tests for MaaS TLS scan helpers."""
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from maas_tls_scan_lib import (
    ServiceTarget,
    build_targets_meta,
    discover_service_targets,
    filter_targets_by_allowlist,
    parse_service_targets,
    targets_cli_argument,
    validate_scan_artifacts,
)


class TestServiceTargetParse(unittest.TestCase):
    def test_parse_entry(self) -> None:
        t = ServiceTarget.parse_entry("maas-api:odh-ai-gateway-infra/maas-api:8443")
        self.assertEqual(t.component, "maas-api")
        self.assertEqual(t.namespace, "odh-ai-gateway-infra")
        self.assertEqual(t.service, "maas-api")
        self.assertEqual(t.port, 8443)
        self.assertEqual(
            t.host_port(),
            "maas-api.odh-ai-gateway-infra.svc.cluster.local:8443",
        )

    def test_parse_invalid(self) -> None:
        with self.assertRaises(ValueError):
            ServiceTarget.parse_entry("bad")

    def test_defaults(self) -> None:
        targets = parse_service_targets(None)
        self.assertEqual(len(targets), 2)
        self.assertEqual(targets[0].component, "maas-api")


class TestTargetsCLI(unittest.TestCase):
    def test_cli_argument(self) -> None:
        targets = parse_service_targets(
            "maas-api:odh-ai-gateway-infra/maas-api:8443,"
            "maas-controller:opendatahub/maas-controller-metrics:8443"
        )
        arg = targets_cli_argument(targets)
        self.assertIn("maas-api.odh-ai-gateway-infra.svc.cluster.local:8443", arg)
        self.assertIn("maas-controller-metrics.opendatahub.svc.cluster.local:8443", arg)


class TestDiscoverServices(unittest.TestCase):
    def test_filters_missing(self) -> None:
        targets = list(parse_service_targets(None))

        def exists(ns: str, name: str) -> bool:
            return ns == "odh-ai-gateway-infra" and name == "maas-api"

        kept, warnings = discover_service_targets(targets, exists)
        self.assertEqual(len(kept), 1)
        self.assertEqual(kept[0].component, "maas-api")
        self.assertTrue(any("maas-controller-metrics" in w for w in warnings))


class TestAllowlistFilter(unittest.TestCase):
    def test_port_filter(self) -> None:
        targets = parse_service_targets(
            "maas-api:ns/svc:8443,x:ns/metrics:9090"
        )
        filtered = filter_targets_by_allowlist(targets, {8443})
        self.assertEqual(len(filtered), 1)
        self.assertEqual(filtered[0].port, 8443)


class TestValidateArtifacts(unittest.TestCase):
    def _write_results(self, directory: Path, payload: dict) -> None:
        (directory / "results.json").write_text(
            json.dumps(payload), encoding="utf-8"
        )

    def test_passes_with_host_map(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            d = Path(tmp)
            host = "maas-api.odh-ai-gateway-infra.svc.cluster.local"
            self._write_results(
                d,
                {
                    "ip_results": [
                        {
                            "ip": host,
                            "port_results": [
                                {
                                    "port": 8443,
                                    "tls_versions": ["TLSv1.3"],
                                    "tls13_supported": True,
                                    "mlkem_supported": True,
                                }
                            ],
                        },
                        {
                            "ip": "maas-controller-metrics.opendatahub.svc.cluster.local",
                            "openshift_component": {"component": "maas-controller"},
                            "port_results": [
                                {
                                    "port": 8443,
                                    "tls13_supported": True,
                                    "mlkem_supported": True,
                                    "tls_versions": ["TLSv1.3"],
                                }
                            ],
                        },
                    ]
                },
            )
            meta = build_targets_meta(parse_service_targets(None))
            (d / "scan-targets.meta.json").write_text(
                json.dumps(meta), encoding="utf-8"
            )
            ok, errors, hits = validate_scan_artifacts(
                d,
                allow_ports={8443},
                required_components=["maas-api", "maas-controller"],
                require_pqc=True,
                host_map=meta["host_to_component"],
            )
            self.assertTrue(ok, errors)
            self.assertEqual(len(hits["maas-api"]), 1)

    def test_fails_without_pqc(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            d = Path(tmp)
            self._write_results(
                d,
                {
                    "ip_results": [
                        {
                            "ip": "10.0.0.1",
                            "openshift_component": {"component": "maas-api"},
                            "port_results": [
                                {
                                    "port": 8443,
                                    "tls13_supported": True,
                                    "mlkem_supported": False,
                                    "tls_versions": ["TLSv1.3"],
                                }
                            ],
                        }
                    ]
                },
            )
            ok, errors, _ = validate_scan_artifacts(
                d,
                allow_ports={8443},
                required_components=["maas-api", "maas-controller"],
                require_pqc=True,
                host_map={},
            )
            self.assertFalse(ok)
            self.assertTrue(any("maas-controller" in e for e in errors))

    def test_fails_vacuous_pass_message(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            d = Path(tmp)
            self._write_results(d, {"ip_results": []})
            (d / "output.log").write_text(
                "PQC COMPLIANCE CHECK: PASSED\nFatal error: Can't connect\n",
                encoding="utf-8",
            )
            ok, errors, _ = validate_scan_artifacts(
                d,
                allow_ports={8443},
                required_components=["maas-api"],
                require_pqc=True,
                host_map={},
            )
            self.assertFalse(ok)
            self.assertTrue(any("PQC COMPLIANCE CHECK" in e for e in errors))


if __name__ == "__main__":
    unittest.main()
