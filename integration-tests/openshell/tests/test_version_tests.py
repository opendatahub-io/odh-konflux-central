import copy
import json
from pathlib import Path
import sys
import tempfile
import unittest
import urllib.error
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).parents[1]))
import version_tests as m

REV = "a" * 40
DIGEST = "sha256:" + "b" * 64
TAG = "v0.1.8-rhaiv.0"


def images():
    return {role: {"image": "quay.io/opendatahub/odh-openshell-" + role + "@" + DIGEST,
                   "source_revision": REV, "selection_tag": "odh-stable" if role == "e2e" else TAG,
                   "build": role + "-build", "evidence": {"snapshot": role, "snapshot_uid": role + "-uid"}}
            for role in m.ROLES}


def manifest():
    return {"schema_version": 1, "version": TAG, "source_repository": m.SOURCE, "source_revision": REV, "tiers": ["smoke"], "architecture": "amd64",
            "run_id": "openshell-version-1", "started_at": "2026-11-19T12:00:00+00:00"}


class AgentSandboxInstallation(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.directory = Path(temporary.name)
        self.args = SimpleNamespace(data=self.directory, reports=self.directory)
        m.write(self.directory / "manifest.json", {"agent_sandbox": m.AGENT_SANDBOX})
        self.subscription = {"metadata": {"uid": "subscription-uid"}, "status": {
            "installPlanRef": {"uid": "plan-uid", "name": "install-1"},
            "installedCSV": m.AGENT_SANDBOX["csv"]}}
        self.plan = {"metadata": {"name": "install-1", "uid": "plan-uid", "ownerReferences": [
            {"kind": "Subscription", "uid": "subscription-uid"}]},
            "spec": {"clusterServiceVersionNames": [m.AGENT_SANDBOX["csv"]],
                     "approval": "Manual", "approved": False}}
        self.csv = {"spec": {"version": "0.9.0", "provider": {"name": "Red Hat"},
                             "install": {"spec": {"deployments": [{"name": "controller-manager"}]}},
                             "customresourcedefinitions": {"owned": [
                                 {"name": "sandboxes.agents.x-k8s.io"}]}},
                    "status": {"phase": "Succeeded"}}
        self.image = "registry.redhat.io/agent-sandbox/agent-sandbox-rhel9-operator@" + DIGEST
        self.served = True
        self.calls = []

    def cli(self, *args, value=None):
        self.calls.append((args, value))
        if args[:2] == ("get", "packagemanifests"):
            return {"items": [{"status": {"catalogSource": "redhat-operators",
                "catalogSourceNamespace": "openshift-marketplace", "provider": {"name": "Red Hat"},
                "channels": [{"name": "preview-0.9", "entries": [{"name": m.AGENT_SANDBOX["csv"]}]}]}}]}
        if args[0] in ("apply", "patch"):
            return value or {}
        if args[:2] == ("get", "subscriptions.operators.coreos.com"):
            return self.subscription
        if args[:2] == ("get", "installplans.operators.coreos.com"):
            return self.plan
        if args[:2] == ("get", "clusterserviceversions.operators.coreos.com"):
            return self.csv
        if args[:2] == ("get", "deployments.apps"):
            return {"spec": {"template": {"spec": {"containers": [{"image": self.image}]}}}}
        if args[:2] == ("get", "crd/sandboxes.agents.x-k8s.io"):
            return {"spec": {"versions": [{"name": "v1beta1", "served": self.served}]}}
        raise AssertionError(args)

    def test_subscription_approves_only_pinned_plan_and_waits_for_readiness(self):
        with patch.object(m, "oc", side_effect=self.cli), patch.object(m.subprocess, "run") as process:
            m.install_agent_sandbox(self.args)
        applied = [value for args, value in self.calls if args[0] == "apply"]
        subscription = next(item for item in applied if item["kind"] == "Subscription")
        self.assertEqual(subscription["spec"], {
            "name": "agent-sandbox-operator", "channel": "preview-0.9",
            "source": "redhat-operators", "sourceNamespace": "openshift-marketplace",
            "startingCSV": "agent-sandbox-operator.v0.9.0", "installPlanApproval": "Manual"})
        self.assertEqual(next(item for item in applied if item["kind"] == "OperatorGroup")["spec"], {})
        approval = next(args for args, _ in self.calls if args[0] == "patch")
        patch_data = json.loads(approval[approval.index("-p") + 1])
        self.assertIn({"op": "test", "path": "/metadata/uid", "value": "plan-uid"}, patch_data)
        self.assertIn({"op": "test", "path": "/spec/clusterServiceVersionNames",
                       "value": [m.AGENT_SANDBOX["csv"]]}, patch_data)
        self.assertTrue(any("rollout" in call.args[0] for call in process.call_args_list))
        self.assertTrue(any("crd/sandboxes.agents.x-k8s.io" in call.args[0]
                            for call in process.call_args_list))
        installed = json.loads((self.directory / "agent-sandbox-installation.json").read_text())
        self.assertTrue(installed["ready"])
        self.assertEqual(installed["csv"], m.AGENT_SANDBOX["csv"])
        self.assertEqual(installed["images"]["controller-manager"], [self.image])

    def test_other_versions_extra_dependencies_unrelated_or_automatic_plans_are_rejected(self):
        for change in ("version", "dependency", "uid", "owner", "automatic"):
            plan = copy.deepcopy(self.plan)
            if change == "version":
                plan["spec"]["clusterServiceVersionNames"] = ["agent-sandbox-operator.v0.9.1"]
            elif change == "dependency":
                plan["spec"]["clusterServiceVersionNames"].append("other-operator.v1.0.0")
            elif change == "uid":
                plan["metadata"]["uid"] = "another-plan"
            elif change == "owner":
                plan["metadata"]["ownerReferences"][0]["uid"] = "another-subscription"
            else:
                plan["spec"]["approval"] = "Automatic"
            with self.subTest(change=change), self.assertRaises(ValueError):
                m.check_install_plan(plan, self.subscription)

    def test_rejected_plan_is_never_approved_and_no_readiness_receipt_is_written(self):
        self.plan["spec"]["clusterServiceVersionNames"] = ["agent-sandbox-operator.v0.9.1"]
        with patch.object(m, "oc", side_effect=self.cli), self.assertRaises(ValueError):
            m.install_agent_sandbox(self.args)
        self.assertFalse(any(args[0] == "patch" for args, _ in self.calls))
        self.assertFalse((self.directory / "agent-sandbox-installation.json").exists())

    def test_wrong_csv_controller_or_api_never_reports_ready(self):
        for change in ("version", "provider", "image", "api", "subscription"):
            self.setUp()
            if change == "version":
                self.csv["spec"]["version"] = "0.9.1"
            elif change == "provider":
                self.csv["spec"]["provider"]["name"] = "Upstream"
            elif change == "image":
                self.image = "registry.k8s.io/agent-sandbox/controller:latest"
            elif change == "api":
                self.served = False
            else:
                self.subscription["status"]["installedCSV"] = "agent-sandbox-operator.v0.9.1"
            with self.subTest(change=change), patch.object(m, "oc", side_effect=self.cli), \
                    patch.object(m.subprocess, "run"), self.assertRaises(ValueError):
                m.install_agent_sandbox(self.args)
            self.assertFalse((self.directory / "agent-sandbox-installation.json").exists())

    def test_missing_catalog_version_times_out_without_creating_resources(self):
        with patch.object(m, "oc", return_value={"items": []}) as cli, \
                patch.object(m.time, "monotonic", side_effect=[0, 1, 301]), patch.object(m.time, "sleep"), \
                self.assertRaises(TimeoutError):
            m.install_agent_sandbox(self.args)
        self.assertTrue(all(call.args[0] == "get" for call in cli.call_args_list))

    def test_controller_rollout_failure_does_not_write_readiness_receipt(self):
        with patch.object(m, "oc", side_effect=self.cli), patch.object(m.subprocess, "run",
                side_effect=m.subprocess.CalledProcessError(1, ["oc", "rollout"])), \
                self.assertRaises(m.subprocess.CalledProcessError):
            m.install_agent_sandbox(self.args)
        self.assertFalse((self.directory / "agent-sandbox-installation.json").exists())


class Reporting(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.reports = Path(self.tmp.name)
        (self.reports / "smoke").mkdir()
        self.manifest = manifest()
        self.manifest["images"] = images()
        m.write(self.reports / "agent-sandbox-installation.json", dict(m.AGENT_SANDBOX, ready=True))

    def report(self, cases, exit_code="0"):
        (self.reports / "smoke/e2e-odh-smoke-xunit.xml").write_text(
            '<testsuites tests="999"><testsuite>' + cases + '</testsuite></testsuites>')
        (self.reports / "smoke.exit").write_text(exit_code)

    def test_counts_cases_not_nested_aggregate_attributes(self):
        self.report('<testcase name="a"/><testcase name="b"><skipped/></testcase>')
        s = m.summary(self.manifest, self.reports, "Succeeded")
        self.assertEqual(s["status"], "passed")
        self.assertEqual(s["counts"], dict(total=2, passed=1, skipped=1, failed=0, errors=0))

    def test_failed_tests_and_errors_are_retained_without_messages(self):
        self.report('<testcase><failure>private failure detail</failure></testcase><testcase><error/></testcase>', "1")
        s = m.summary(self.manifest, self.reports, "Failed")
        self.assertEqual(s["status"], "failed")
        self.assertEqual(s["counts"]["errors"], 1)
        self.assertNotIn("private failure detail", json.dumps(s))

    def test_incomplete_all_skipped_and_infrastructure_failure_never_pass(self):
        self.assertEqual(m.summary(self.manifest, self.reports, "Failed")["status"], "error")
        for cases, exit_code, status in [('<testcase><skipped/></testcase>', "0", "Succeeded"),
                                         ('<testcase/>', "1", "Succeeded"),
                                         ('<testcase/>', "0", "Failed")]:
            self.report(cases, exit_code)
            self.assertEqual(m.summary(self.manifest, self.reports, status)["status"], "error")
        self.report('<testcase/>')
        self.manifest["tiers"].append("tier1")
        self.assertEqual(m.summary(self.manifest, self.reports, "Succeeded")["status"], "error")

    def test_malformed_report_cannot_pass(self):
        self.report("<testcase>")
        self.assertEqual(m.summary(self.manifest, self.reports, "Succeeded")["status"], "error")

    def test_missing_or_wrong_agent_sandbox_installation_cannot_pass(self):
        self.report('<testcase/>')
        receipt = self.reports / "agent-sandbox-installation.json"
        receipt.unlink()
        self.assertEqual(m.summary(self.manifest, self.reports, "Succeeded")["status"], "error")
        for incorrect in (dict(m.AGENT_SANDBOX, ready=False),
                          dict(m.AGENT_SANDBOX, ready=True, distribution="upstream"),
                          dict(m.AGENT_SANDBOX, ready=True, version="v0.9.1")):
            m.write(receipt, incorrect)
            self.assertEqual(m.summary(self.manifest, self.reports, "Succeeded")["status"], "error")


class ClusterAccess(unittest.TestCase):
    def test_paginated_build_listing_keeps_every_page(self):
        pages = [{"items": [1], "metadata": {"continue": "cursor"}}, {"items": [2]}]
        queries = []

        def api(path, query):
            queries.append(dict(query))
            return pages.pop(0)

        with patch.object(m, "cluster_request", side_effect=api):
            self.assertEqual(m.cluster_collection("tekton.dev/v1/namespaces/{namespace}/pipelineruns"), [1, 2])
        self.assertNotIn("continue", queries[0])
        self.assertEqual(queries[1]["continue"], "cursor")



if __name__ == "__main__":
    unittest.main()
