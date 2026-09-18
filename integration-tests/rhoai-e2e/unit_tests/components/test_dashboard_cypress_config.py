"""Tests for dashboard Cypress cluster config resolution."""

from __future__ import annotations

import json
import os
import tempfile
import unittest
import xml.etree.ElementTree as ET
from pathlib import Path
from unittest.mock import patch

from suite.component_catalog_models import CypressParallelSet, CypressRunnerConfig
from components.dashboard_cypress.config import (
    cypress_parallel_sets_command,
    merge_smokeset_junit_reports,
    normalize_cypress_run_config,
    resolve_cypress_run_command,
    resolve_odh_dashboard_base_url,
    resolve_odh_dashboard_project_name,
    write_dashboard_cypress_test_config,
    _token_from_kubeconfig,
)

def _sample_cypress_config() -> CypressRunnerConfig:
    return CypressRunnerConfig(
        skip_tags="@Bug @Maintain @Featureflagged",
        gates={
            "smoke": (
                CypressParallelSet("@SmokeSet1", "SmokeSet1"),
                CypressParallelSet("@SmokeSet2", "SmokeSet2"),
            ),
            "tier1": (
                CypressParallelSet("@SanitySet1", "SanitySet1"),
            ),
        },
    )

class DashboardCypressConfigTest(unittest.TestCase):
    def test_resolve_dashboard_url_from_consolelink(self) -> None:
        consolelinks = {
            "items": [
                {
                    "metadata": {
                        "annotations": {
                            "platform.opendatahub.io/instance.name": "default-dashboard",
                        }
                    },
                    "spec": {"href": "https://rh-ai.apps.example.com/"},
                }
            ]
        }
        with patch(
            "components.dashboard_cypress.config.oc_run",
            return_value=type("R", (), {"returncode": 0, "stdout": json.dumps(consolelinks)})(),
        ):
            self.assertEqual(
                resolve_odh_dashboard_base_url(),
                "https://rh-ai.apps.example.com",
            )

    def test_resolve_dashboard_url_falls_back_to_route(self) -> None:
        with patch(
            "components.dashboard_cypress.config.oc_run",
            side_effect=[
                type("R", (), {"returncode": 0, "stdout": '{"items": []}'})(),
                type("R", (), {"returncode": 0, "stdout": "rhods-dashboard.example.com"})(),
            ],
        ):
            self.assertEqual(
                resolve_odh_dashboard_base_url(),
                "https://rhods-dashboard.example.com",
            )

    def test_write_minimal_test_config(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "cfg.yml"
            write_dashboard_cypress_test_config(path, dashboard_url="https://dash.example")
            text = path.read_text(encoding="utf-8")
            self.assertIn("ODH_DASHBOARD_URL: https://dash.example", text)
            self.assertIn("OPERATOR_NAMESPACE: redhat-ods-operator", text)

    def test_cypress_parallel_command_uses_catalog_tags(self) -> None:
        cmd = cypress_parallel_sets_command(
            sets=(
                CypressParallelSet("@SmokeSet1", "SmokeSet1"),
                CypressParallelSet("@SmokeSet2", "SmokeSet2"),
            ),
            skip_tags="@Bug @Maintain",
            test_timeout_seconds="480",
            run_config="video=false",
            parallel_stagger_sec=15,
            display_base=99,
        )
        self.assertIn("@SmokeSet1", cmd)
        self.assertIn("@SmokeSet2", cmd)
        self.assertIn("DISPLAY=:99", cmd)
        self.assertIn("DISPLAY=:100", cmd)
        self.assertIn("sleep 15", cmd)
        self.assertIn(") &", cmd)
        self.assertIn("wait;", cmd)
        self.assertNotIn(" && CY_RESULTS_DIR", cmd)
        self.assertIn('--config "video=false"', cmd)
        self.assertNotIn("mkfifo", cmd)

    def test_cypress_parallel_command_caps_concurrency(self) -> None:
        with patch.dict(os.environ, {"CYPRESS_MAX_PARALLEL": "2"}, clear=False):
            cmd = cypress_parallel_sets_command(
                sets=(
                    CypressParallelSet("@SmokeSet1", "SmokeSet1"),
                    CypressParallelSet("@SmokeSet2", "SmokeSet2"),
                    CypressParallelSet("@SmokeSet3", "SmokeSet3"),
                ),
                skip_tags="@Bug",
                test_timeout_seconds="480",
                run_config="video=false",
                parallel_stagger_sec=15,
                display_base=99,
                max_parallel=2,
            )
        self.assertIn("mkfifo", cmd)
        self.assertIn("read -u 3", cmd)
        self.assertIn("echo >&3", cmd)
        self.assertEqual(cmd.count("read -u 3"), 3)
        self.assertIn("refresh_partial_cypress_junit", cmd)

    def test_normalize_cypress_run_config_collapses_yaml_fold_whitespace(self) -> None:
        folded = (
            "numTestsKeptInMemory=0,experimentalMemoryManagement=true,\n"
            "video=false,viewportWidth=1920,viewportHeight=1080"
        )
        self.assertEqual(
            normalize_cypress_run_config(folded),
            "numTestsKeptInMemory=0,experimentalMemoryManagement=true,"
            "video=false,viewportWidth=1920,viewportHeight=1080",
        )

    def test_cypress_run_config_survives_env_roundtrip_through_bash(self) -> None:
        import os
        import subprocess
        import tempfile
        from unittest.mock import patch

        from components.dashboard_cypress.config import cypress_set_command
        from suite.component_runner_env import load_component_runner_env

        with patch.dict(os.environ, {"CYPRESS_BROWSER": "electron"}, clear=False):
            cmd = cypress_set_command(
                grep_tag="@SmokeSet1",
                results_subdir="SmokeSet1",
                skip_tags="@Bug @Maintain @Featureflagged",
                test_timeout_seconds="480",
                run_config="video=false,viewportWidth=1920",
            )
        with tempfile.NamedTemporaryFile("w", encoding="utf-8", delete=False) as tmp:
            tmp.write(f"RUN_COMMAND={json.dumps(cmd)}\n")
            env_path = Path(tmp.name)
        try:
            loaded = load_component_runner_env(env_path)["RUN_COMMAND"]
        finally:
            env_path.unlink(missing_ok=True)
        stub = loaded.replace(
            "npx cypress run --browser electron --project ../packages/cypress",
            'python3 -c "import sys; i=sys.argv.index(\'--config\'); print(sys.argv[i+1])" stub',
            1,
        )
        proc = subprocess.run(
            ["bash", "-c", stub],
            capture_output=True,
            text=True,
            check=True,
            env={**os.environ, "ARTIFACTS": "/tmp/artifacts"},
        )
        self.assertEqual(
            proc.stdout.strip(),
            "video=false,viewportWidth=1920",
        )

    def test_resolve_odh_dashboard_project_name_from_csv_list(self) -> None:
        csv_json = json.dumps(
            {
                "items": [
                    {
                        "spec": {"displayName": "Red Hat OpenShift AI"},
                        "status": {"phase": "Succeeded"},
                    }
                ]
            }
        )
        with patch(
            "components.dashboard_cypress.config.oc_run",
            return_value=type("R", (), {"returncode": 0, "stdout": csv_json})(),
        ):
            self.assertEqual(
                resolve_odh_dashboard_project_name(),
                "Red Hat OpenShift AI",
            )

    def test_resolve_cypress_run_command_from_catalog_gates(self) -> None:
        cmd = resolve_cypress_run_command(_sample_cypress_config(), ("smoke", "tier1"))
        self.assertIn("@SmokeSet1", cmd)
        self.assertIn("@SanitySet1", cmd)
        self.assertIn(" && ", cmd)

    def test_merge_smokeset_junit_reports(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            report_a = root / "SmokeSet1" / "e2e"
            report_b = root / "SmokeSet2" / "e2e"
            report_a.mkdir(parents=True)
            report_b.mkdir(parents=True)
            suite_a = ET.Element("testsuite", {"name": "a", "tests": "1"})
            ET.SubElement(suite_a, "testcase", {"name": "t", "classname": "a"})
            ET.ElementTree(suite_a).write(
                report_a / "junit-report.xml",
                encoding="unicode",
                xml_declaration=True,
            )
            suite_b = ET.Element("testsuite", {"name": "b", "tests": "1"})
            ET.SubElement(suite_b, "testcase", {"name": "t", "classname": "b"})
            ET.ElementTree(suite_b).write(
                report_b / "junit-report.xml",
                encoding="unicode",
                xml_declaration=True,
            )
            dest = root / "merged.xml"
            self.assertTrue(
                merge_smokeset_junit_reports(
                    root,
                    dest,
                    results_subdirs=("SmokeSet1", "SmokeSet2"),
                )
            )
            merged = ET.parse(dest).getroot()
            self.assertEqual(merged.tag, "testsuites")
            self.assertEqual(len(list(merged)), 2)

    def test_merge_finds_hash_named_junit_under_smokeset(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            subdir = root / "SmokeSet5" / "e2e"
            subdir.mkdir(parents=True)
            suite = ET.Element("testsuite", {"name": "Mocha Tests", "tests": "1", "failures": "1"})
            tc = ET.SubElement(suite, "testcase", {"name": "hook", "classname": "suite"})
            ET.SubElement(tc, "failure").text = "timed out"
            ET.ElementTree(suite).write(
                subdir / "junit-2bfc11940691ae8f7ef0795a1ab88e63.xml",
                encoding="unicode",
                xml_declaration=True,
            )
            dest = root / "out.xml"
            self.assertTrue(
                merge_smokeset_junit_reports(root, dest, results_subdirs=("SmokeSet5",))
            )
            self.assertEqual(len(ET.parse(dest).getroot().findall(".//testcase")), 1)

    def test_find_all_junit_reports_prefers_canonical_report(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            subdir = root / "SmokeSet1" / "e2e"
            subdir.mkdir(parents=True)
            suite = ET.Element("testsuite", {"name": "full", "tests": "3", "failures": "1"})
            ET.SubElement(suite, "testcase", {"name": "a", "classname": "full"})
            ET.SubElement(suite, "testcase", {"name": "b", "classname": "full"})
            tc = ET.SubElement(suite, "testcase", {"name": "c", "classname": "full"})
            ET.SubElement(tc, "failure").text = "fail"
            ET.ElementTree(suite).write(
                subdir / "junit-report.xml",
                encoding="unicode",
                xml_declaration=True,
            )
            fragment = ET.Element("testsuite", {"name": "Mocha Tests", "tests": "1", "failures": "1"})
            tc = ET.SubElement(fragment, "testcase", {"name": "hook", "classname": "suite"})
            ET.SubElement(tc, "failure").text = "timed out"
            ET.ElementTree(fragment).write(
                subdir / "junit-2bfc11940691ae8f7ef0795a1ab88e63.xml",
                encoding="unicode",
                xml_declaration=True,
            )
            from components.dashboard_cypress.config import _find_all_junit_reports

            reports = _find_all_junit_reports(subdir)
            self.assertEqual(len(reports), 1)
            self.assertEqual(reports[0].name, "junit-report.xml")

    def test_merge_prefers_junit_over_mochawesome_when_both_present(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            subdir = root / "SmokeSet1" / "e2e"
            jsons = subdir / ".jsons"
            jsons.mkdir(parents=True)
            suite = ET.Element("testsuite", {"name": "full", "tests": "3", "failures": "1"})
            ET.SubElement(suite, "testcase", {"name": "a", "classname": "full"})
            ET.SubElement(suite, "testcase", {"name": "b", "classname": "full"})
            tc = ET.SubElement(suite, "testcase", {"name": "c", "classname": "full"})
            ET.SubElement(tc, "failure").text = "fail"
            ET.ElementTree(suite).write(
                subdir / "junit-report.xml",
                encoding="unicode",
                xml_declaration=True,
            )
            (jsons / "mochawesome.json").write_text(
                json.dumps(
                    {
                        "results": [
                            {
                                "title": "",
                                "tests": [{"title": "only-one", "state": "passed", "duration": 1}],
                                "suites": [],
                            }
                        ]
                    }
                ),
                encoding="utf-8",
            )
            dest = root / "out.xml"
            self.assertTrue(
                merge_smokeset_junit_reports(root, dest, results_subdirs=("SmokeSet1",))
            )
            self.assertEqual(len(ET.parse(dest).getroot().findall(".//testcase")), 3)

    def test_merge_prefers_mochawesome_when_junit_empty(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            subdir = root / "SmokeSet5" / "e2e"
            jsons = subdir / ".jsons"
            jsons.mkdir(parents=True)
            (subdir / "junit-report.xml").write_text("", encoding="utf-8")
            sample = {
                "results": [
                    {
                        "title": "",
                        "suites": [
                            {
                                "title": "MaaS subscriptions",
                                "beforeHooks": [
                                    {
                                        "title": '"before each" hook',
                                        "state": "failed",
                                        "duration": 180000,
                                        "err": {"message": "cy.exec timed out"},
                                    }
                                ],
                                "afterHooks": [
                                    {
                                        "title": '"after all" hook',
                                        "state": "failed",
                                        "duration": 300000,
                                        "err": {"message": "cy.exec timed out"},
                                    }
                                ],
                                "tests": [],
                                "suites": [],
                            }
                        ],
                    }
                ]
            }
            (jsons / "mochawesome.json").write_text(json.dumps(sample), encoding="utf-8")
            dest = root / "out.xml"
            self.assertTrue(
                merge_smokeset_junit_reports(root, dest, results_subdirs=("SmokeSet5",))
            )
            root_el = ET.parse(dest).getroot()
            cases = root_el.findall(".//testcase")
            self.assertEqual(len(cases), 2)
            self.assertEqual(cases[0].find("failure").text, "cy.exec timed out")

    def test_inject_auth_into_cypress_run_command(self) -> None:
        from components.dashboard_cypress.config import inject_auth_into_cypress_run_command

        base_cmd = (
            'CY_RESULTS_DIR="${ARTIFACTS}/SmokeSet1" npx cypress run --browser electron '
            '--project ../packages/cypress --env skipTags="@Bug",grepTags="@SmokeSet1" '
            '--config "video=false"'
        )
        with patch.dict(
            os.environ,
            {
                "CLUSTER_AUTH": "htpasswd-cluster-admin",
                "TEST_USER_USERNAME": "htpasswd-cluster-admin-user",
                "TEST_USER_PASSWORD": "secret",
                "TEST_USER_AUTH_TYPE": "htpasswd-cluster-admin",
            },
            clear=False,
        ):
            patched = inject_auth_into_cypress_run_command(base_cmd)
        self.assertIn('CLUSTER_AUTH="htpasswd-cluster-admin"', patched)
        self.assertIn('TEST_USER_USERNAME="htpasswd-cluster-admin-user"', patched)
        self.assertIn('grepTags="@SmokeSet1"', patched)

        stale_cmd = (
            'npx cypress run --env skipTags="@Bug",CLUSTER_AUTH="",grepTags="@SmokeSet1" '
            '--config "video=false"'
        )
        with patch.dict(
            os.environ,
            {"CLUSTER_AUTH": "htpasswd-cluster-admin", "TEST_USER_USERNAME": "user"},
            clear=False,
        ):
            replaced = inject_auth_into_cypress_run_command(stale_cmd)
        self.assertIn('CLUSTER_AUTH="htpasswd-cluster-admin"', replaced)
        self.assertNotIn('CLUSTER_AUTH=""', replaced)

    def test_inject_auth_strips_stale_oc_token_for_ldap_gateway(self) -> None:
        from components.dashboard_cypress.config import inject_auth_into_cypress_run_command

        stale_cmd = (
            'npx cypress run --env skipTags="@Bug",CLUSTER_AUTH="",OC_TOKEN="stale-token",'
            'grepTags="@SmokeSet1" --config "video=false"'
        )
        with patch.dict(
            os.environ,
            {
                "CLUSTER_AUTH": "",
                "TEST_USER_USERNAME": "ldap-admin1",
                "TEST_USER_PASSWORD": "secret",
                "TEST_USER_AUTH_TYPE": "ldap-provider-qe",
            },
            clear=False,
        ):
            patched = inject_auth_into_cypress_run_command(stale_cmd)
        self.assertIn('TEST_USER_USERNAME="ldap-admin1"', patched)
        self.assertNotIn('OC_TOKEN="stale-token"', patched)

    def test_cypress_extra_env_flags_ldap_gateway_omits_oc_token(self) -> None:
        from components.dashboard_cypress.config import cypress_extra_env_flags

        with patch.dict(
            os.environ,
            {
                "ODH_DASHBOARD_URL": "https://rh-ai.apps.example.com",
                "OC_TOKEN": "should-not-appear",
                "TEST_USER_USERNAME": "ldap-admin1",
                "TEST_USER_AUTH_TYPE": "ldap-provider-qe",
                "CLUSTER_AUTH": "",
            },
            clear=False,
        ):
            flags = cypress_extra_env_flags()
        self.assertIn('TEST_USER_USERNAME="ldap-admin1"', flags)
        self.assertNotIn("OC_TOKEN", flags)

    def test_inject_skip_tags_into_cypress_run_command(self) -> None:
        from components.dashboard_cypress.config import inject_skip_tags_into_cypress_run_command

        base_cmd = (
            'npx cypress run --env skipTags="@Bug @Maintain",grepTags="@SmokeSet3" '
            '--config "video=false"'
        )
        patched = inject_skip_tags_into_cypress_run_command(
            base_cmd,
            "@ModelServingCI @ProjectsCI",
        )
        self.assertIn(
            'skipTags="@Bug @Maintain @ModelServingCI @ProjectsCI"',
            patched,
        )

    def test_token_from_kubeconfig(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            kc = Path(tmp) / "config"
            kc.write_text(
                "\n".join(
                    [
                        "current-context: ctx",
                        "contexts:",
                        "  - name: ctx",
                        "    context:",
                        "      cluster: c",
                        "      user: u",
                        "users:",
                        "  - name: u",
                        "    user:",
                        "      token: secret-token-long-enough-for-sanity-check",
                    ]
                )
                + "\n",
                encoding="utf-8",
            )
            self.assertEqual(
                _token_from_kubeconfig(str(kc)),
                "secret-token-long-enough-for-sanity-check",
            )

    def test_load_component_runner_env_parses_exports(self) -> None:
        from suite.component_runner_env import load_component_runner_env

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "component-golang.env"
            run_command = json.dumps("npm run cypress:run")
            path.write_text(
                "\n".join(
                    [
                        "SKIP=false",
                        "WORKING_DIR=packages/cypress",
                        f"RUN_COMMAND={run_command}",
                        "export ODH_DASHBOARD_URL='https://dash.example'",
                        'export CYPRESS_OC_TOKEN="token123"',
                    ]
                )
                + "\n",
                encoding="utf-8",
            )
            self.assertEqual(
                load_component_runner_env(path),
                {
                    "SKIP": "false",
                    "WORKING_DIR": "packages/cypress",
                    "RUN_COMMAND": "npm run cypress:run",
                    "ODH_DASHBOARD_URL": "https://dash.example",
                    "CYPRESS_OC_TOKEN": "token123",
                },
            )

