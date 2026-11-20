"""Coordinate versioned OpenShell builds and publish small public test summaries."""

import argparse
import base64
import datetime as dt
import hashlib
import json
import os
from pathlib import Path
import re
import ssl
import shutil
import subprocess
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET

SOURCE = "https://github.com/opendatahub-io/openshell"
METADATA = "opendatahub-io/odh-build-metadata"
VERSION = re.compile(r"v[0-9]+\.[0-9]+\.[0-9]+-rhaiv\.[0-9]+")
SHA = re.compile(r"[0-9a-f]{40}")
DIGEST = re.compile(r"sha256:[0-9a-f]{64}")
ROLES = ("gateway", "supervisor", "sandbox", "e2e")
TIERS = {"smoke", "tier1", "tier2", "tier3"}
AGENT_SANDBOX = {
    "distribution": "redhat", "version": "v0.9.0",
    "package": "agent-sandbox-operator", "channel": "preview-0.9",
    "csv": "agent-sandbox-operator.v0.9.0", "namespace": "agent-sandbox-system",
    "catalog_source": "redhat-operators", "catalog_namespace": "openshift-marketplace",
}


def now():
    return dt.datetime.now(dt.timezone.utc).isoformat()


def write(path, value):
    Path(path).write_text(json.dumps(value, indent=2, sort_keys=True) + "\n")


def freeze(path, value):
    """A restarted final task reuses the first completed record verbatim."""
    path = Path(path)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", dir=path.parent, delete=False) as stream:
            temporary = Path(stream.name)
            json.dump(value, stream, indent=2, sort_keys=True)
            stream.write("\n")
            stream.flush()
            os.fchmod(stream.fileno(), 0o644)
            os.fsync(stream.fileno())
        os.link(temporary, path)
    except FileExistsError:
        return json.loads(path.read_text())
    finally:
        if temporary:
            temporary.unlink(missing_ok=True)
    return value


def group_identity(group):
    value = {key: group[key] for key in ("version", "source_revision", "architecture", "tiers", "agent_sandbox")}
    value["images"] = {role: {key: group["images"][role][key] for key in ("image", "source_revision")}
                       for role in ROLES}
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def validate_group(group):
    if (group.get("schema_version") != 1
            or not VERSION.fullmatch(group["version"]) or not SHA.fullmatch(group["source_revision"])
            or group["architecture"] != "amd64" or group["agent_sandbox"] != AGENT_SANDBOX
            or not group["tiers"] or len(group["tiers"]) != len(set(group["tiers"]))
            or set(group["tiers"]) - TIERS or set(group["images"]) != set(ROLES)):
        raise ValueError("invalid immutable qualification group")
    for role, item in group["images"].items():
        expected = "quay.io/opendatahub/odh-openshell-" + role + "@"
        if (not item["image"].startswith(expected) or not DIGEST.fullmatch(item["image"][len(expected):])
                or item["source_revision"] != group["source_revision"]
                or item["selection_tag"] != ("odh-stable" if role == "e2e" else group["version"])
                or not item.get("build") or not item.get("evidence", {}).get("snapshot_uid")):
            raise ValueError("missing successful-build provenance or mismatched image: " + role)
    if group["group_id"] != group_identity(group):
        raise ValueError("group identity does not match its images and scope")
    return group


def request(url, *, data=None, token=None, method=None, context=None, content_type="application/json"):
    headers = {"Accept": "application/json", "User-Agent": "openshell-version-tests"}
    if token:
        headers["Authorization"] = "Bearer " + token
    if data is not None:
        headers["Content-Type"] = content_type
        data = json.dumps(data).encode()
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    with urllib.request.urlopen(req, timeout=30, context=context) as response:
        return json.load(response)


def cluster_request(path, query=None, **kwargs):
    sa = Path("/var/run/secrets/kubernetes.io/serviceaccount")
    host = os.environ["KUBERNETES_SERVICE_HOST"]
    port = os.environ["KUBERNETES_SERVICE_PORT"]
    namespace = sa.joinpath("namespace").read_text().strip()
    context = ssl.create_default_context(cafile=str(sa / "ca.crt"))
    token = sa.joinpath("token").read_text().strip()
    prefix = "" if path.startswith("/") else "/apis/"
    url = f"https://{host}:{port}{prefix}{path.format(namespace=namespace)}"
    if query:
        url += "?" + urllib.parse.urlencode(query)
    return request(url, token=token, context=context, **kwargs)


def cluster_collection(path, query=None):
    query = dict(query or {}, limit="100")
    items = []
    while True:
        page = cluster_request(path, query)
        items.extend(page["items"])
        cursor = page.get("metadata", {}).get("continue")
        if not cursor:
            return items
        query["continue"] = cursor


def prepare(args):
    group_id = os.environ["GROUP_ID"]
    if not re.fullmatch(r"[0-9a-f]{64}", group_id):
        raise ValueError("invalid group ID")
    canonical = cluster_request("/api/v1/namespaces/{namespace}/configmaps/openshell-group-" + group_id[:40])
    if not canonical.get("immutable"):
        raise ValueError("qualification requires a retained immutable group")
    context = validate_group(json.loads(canonical["data"]["record.json"]))
    if context["group_id"] != group_id:
        raise ValueError("retained group differs from the requested group ID")
    context.update(schema_version=1, source_repository=SOURCE, started_at=now(),
                   config_revision=(args.data / "config-revision").read_text().strip(),
                   run_id=os.environ["RUN_ID"], run_uid=os.environ["PIPELINE_UID"],
                   agent_sandbox_version=AGENT_SANDBOX["version"])
    write(args.data / "manifest.json", context)
    for role in ROLES:
        Path(os.environ[role.upper() + "_RESULT"]).write_text(context["images"][role]["image"])
    Path(os.environ["TIERS_RESULT"]).write_text(",".join(context["tiers"]))


def oc(*args, value=None):
    result = subprocess.run(["oc", "--request-timeout=30s", *args], check=True,
                            input=json.dumps(value) if value is not None else None,
                            text=True, capture_output=True, timeout=45)
    return json.loads(result.stdout) if result.stdout.strip() else None


def wait_for(description, probe, timeout=900):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        result = probe()
        if result:
            return result
        time.sleep(5)
    raise TimeoutError("timed out waiting for " + description)


def check_install_plan(plan, subscription):
    """Approve only this Subscription's exact version, never a channel upgrade."""
    reference = subscription["status"]["installPlanRef"]
    if (plan["metadata"]["uid"] != reference["uid"]
            or plan["spec"].get("clusterServiceVersionNames") != [AGENT_SANDBOX["csv"]]
            or plan["spec"].get("approval") != "Manual"
            or not any(owner.get("kind") == "Subscription"
                       and owner.get("uid") == subscription["metadata"]["uid"]
                       for owner in plan["metadata"].get("ownerReferences", []))):
        raise ValueError("refusing an unrelated InstallPlan or an Agent Sandbox version other than v0.9.0")


def install_agent_sandbox(args):
    manifest = json.loads((args.data / "manifest.json").read_text())
    if manifest.get("agent_sandbox") != AGENT_SANDBOX:
        raise ValueError("unexpected Red Hat Agent Sandbox installation configuration")
    config = AGENT_SANDBOX
    namespace, csv_name = config["namespace"], config["csv"]

    def get(resource, name=None, ns=namespace):
        names = [name] if name else []
        return oc("get", resource, *names, "-n", ns, "--ignore-not-found", "-o", "json")

    def catalog_ready():
        packages = oc("get", "packagemanifests", "-n", config["catalog_namespace"],
                      "--selector=catalog=" + config["catalog_source"],
                      "--field-selector=metadata.name=" + config["package"], "-o", "json")
        for package in packages["items"]:
            status = package["status"]
            if (status.get("catalogSource") != config["catalog_source"]
                    or status.get("catalogSourceNamespace") != config["catalog_namespace"]
                    or status.get("provider", {}).get("name") != "Red Hat"):
                continue
            for channel in status.get("channels", []):
                entries = [entry["name"] for entry in channel.get("entries", [])]
                if channel["name"] == config["channel"] and csv_name in entries:
                    return True
        return False

    wait_for("Red Hat catalog package preview-0.9 / v0.9.0", catalog_ready, timeout=300)
    objects = [
        {"apiVersion": "v1", "kind": "Namespace", "metadata": {"name": namespace}},
        {"apiVersion": "operators.coreos.com/v1", "kind": "OperatorGroup",
         "metadata": {"name": "agent-sandbox", "namespace": namespace}, "spec": {}},
        {"apiVersion": "operators.coreos.com/v1alpha1", "kind": "Subscription",
         "metadata": {"name": config["package"], "namespace": namespace},
         "spec": {"name": config["package"], "channel": config["channel"],
                  "source": config["catalog_source"], "sourceNamespace": config["catalog_namespace"],
                  "startingCSV": csv_name, "installPlanApproval": "Manual"}},
    ]
    for item in objects:
        oc("apply", "-f", "-", "-o", "json", value=item)

    def plan_ready():
        subscription = get("subscriptions.operators.coreos.com", config["package"])
        if not subscription or not subscription.get("status", {}).get("installPlanRef"):
            return None
        ref = subscription["status"]["installPlanRef"]
        plan = get("installplans.operators.coreos.com", ref["name"])
        if not plan:
            return None
        check_install_plan(plan, subscription)
        return plan

    plan = wait_for("the exact Agent Sandbox InstallPlan", plan_ready)
    if not plan["spec"].get("approved"):
        # Test the identity and version in the same API write as approval.
        patch = [{"op": "test", "path": "/metadata/uid", "value": plan["metadata"]["uid"]},
                 {"op": "test", "path": "/spec/clusterServiceVersionNames", "value": [csv_name]},
                 {"op": "test", "path": "/spec/approval", "value": "Manual"},
                 {"op": "add", "path": "/spec/approved", "value": True}]
        oc("patch", "installplans.operators.coreos.com", plan["metadata"]["name"], "-n", namespace,
           "--type=json", "-p", json.dumps(patch), "-o", "json")

    def csv_ready():
        csv = get("clusterserviceversions.operators.coreos.com", csv_name)
        if not csv:
            return None
        if csv["spec"].get("version") != "0.9.0" or csv["spec"].get("provider", {}).get("name") != "Red Hat":
            raise ValueError("installed CSV is not Red Hat Agent Sandbox v0.9.0")
        if csv.get("status", {}).get("phase") == "Failed":
            raise RuntimeError("Agent Sandbox CSV installation failed")
        return csv if csv.get("status", {}).get("phase") == "Succeeded" else None

    csv = wait_for("Agent Sandbox v0.9.0 CSV Succeeded", csv_ready)
    deployments = csv["spec"]["install"]["spec"]["deployments"]
    if not deployments:
        raise ValueError("Agent Sandbox CSV has no controller deployments")
    images = {}
    for item in deployments:
        name = item["name"]
        subprocess.run(["oc", "-n", namespace, "rollout", "status", "deployment/" + name,
                        "--timeout=300s"], check=True, timeout=330)
        deployment = get("deployments.apps", name)
        images[name] = [c["image"] for c in deployment["spec"]["template"]["spec"]["containers"]]
    if not any(image.startswith("registry.redhat.io/agent-sandbox/")
               for refs in images.values() for image in refs):
        raise ValueError("Agent Sandbox controller is not using a Red Hat registry image")

    for owned in csv["spec"].get("customresourcedefinitions", {}).get("owned", []):
        subprocess.run(["oc", "wait", "crd/" + owned["name"], "--for=condition=Established",
                        "--timeout=120s"], check=True, timeout=150)
    sandbox_crd = oc("get", "crd/sandboxes.agents.x-k8s.io", "-o", "json")
    if not any(v["name"] == "v1beta1" and v.get("served") for v in sandbox_crd["spec"]["versions"]):
        raise ValueError("Agent Sandbox does not serve the required v1beta1 API")
    subscription = get("subscriptions.operators.coreos.com", config["package"])
    if subscription.get("status", {}).get("installedCSV") != csv_name:
        raise ValueError("Subscription did not install the requested Agent Sandbox CSV")
    write(args.reports / "agent-sandbox-installation.json", dict(config, ready=True, images=images))


def summary(manifest, reports, pipeline_status):
    result = dict(manifest, finished_at=now(), pipeline_status=pipeline_status)
    counts = dict(passed=0, failed=0, errors=0, skipped=0, total=0)
    complete = True
    installation_ready = False
    try:
        installed = json.loads((reports / "agent-sandbox-installation.json").read_text())
        installation_ready = (installed.get("ready") is True
                              and all(installed.get(k) == v for k, v in AGENT_SANDBOX.items()))
        if installation_ready:
            result["agent_sandbox_installation"] = dict(AGENT_SANDBOX, ready=True)
    except (OSError, ValueError, AttributeError):
        pass
    for tier in manifest["tiers"]:
        report = reports / tier / ("e2e-odh-" + tier + "-xunit.xml")
        exit_file = reports / (tier + ".exit")
        try:
            cases = list(ET.parse(report).getroot().iter("testcase"))
            if not cases:
                raise ValueError("empty JUnit report")
            for case in cases:
                key = ("errors" if case.find("error") is not None else
                       "failed" if case.find("failure") is not None else
                       "skipped" if case.find("skipped") is not None else "passed")
                counts[key] += 1
                counts["total"] += 1
            if exit_file.read_text().strip() != "0":
                complete = False
        except (OSError, ET.ParseError, ValueError):
            complete = False
    if counts["failed"] or counts["errors"]:
        status = "failed"
    elif (complete and installation_ready and pipeline_status == "Succeeded" and manifest.get("images")
          and counts["passed"] > 0):
        status = "passed"
    else:
        status = "error"
    result.update(status=status, counts=counts, reports_complete=complete)
    # No raw logs, test failure messages, kubeconfig, or environment are public.
    return result


def test_output(record):
    counts = record["counts"]
    return {"result": {"passed": "SUCCESS", "failed": "FAILURE", "error": "ERROR"}[record["status"]],
            "timestamp": record["finished_at"], "successes": counts["passed"],
            "failures": counts["failed"] + counts["errors"], "warnings": 0,
            "note": ",".join(record["tiers"]) + " / " + record["architecture"] + ": "
                    + str(counts["passed"]) + "/" + str(counts["total"]) + " passed; " + record["status"]}


def finalize(args):
    path = args.data / "test-result.json"
    if path.exists():
        return json.loads(path.read_text())
    manifest = json.loads((args.data / "manifest.json").read_text())
    uid = os.environ["PIPELINE_UID"]
    receipt = None
    deadline = time.monotonic() + int(os.environ.get("CLEANUP_WAIT_SECONDS", "300"))
    while time.monotonic() < deadline:
        try:
            cm = cluster_request("/api/v1/namespaces/{namespace}/configmaps/openshell-cleanup-" + uid)
            receipt = json.loads(cm["data"]["receipt.json"])
            if not cm.get("immutable") or receipt.get("owner_uid") != uid:
                raise ValueError("cleanup receipt belongs to another run")
            break
        except urllib.error.HTTPError as error:
            if error.code != 404:
                raise
        time.sleep(5)
    record = summary(manifest, args.reports, os.environ["PIPELINE_STATUS"])
    record["cluster_cleanup_requested"] = bool(receipt and receipt.get("requested") is True)
    if not record["cluster_cleanup_requested"]:
        record["status"] = "error"
    return freeze(path, record)


def stage_artifacts(args):
    args.artifacts.mkdir(parents=True, exist_ok=True)
    total = 0
    for path in args.reports.rglob("*"):
        if not path.is_file() or path.is_symlink():
            continue
        relative = path.relative_to(args.reports)
        if (path.suffix not in (".xml", ".html", ".json", ".log", ".txt", ".exit")
                or any(word in path.name.lower() for word in ("kubeconfig", "credential", "token", "secret"))):
            continue
        total += path.stat().st_size
        if total > 100 * 1024 * 1024:
            raise ValueError("report artifact size exceeds 100 MiB")
        destination = args.artifacts / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(path, destination)
    shutil.copyfile(args.data / "test-result.json", args.artifacts / "test-result.json")


def seal(args):
    path = args.data / "summary.json"
    if path.exists():
        record = json.loads(path.read_text())
    else:
        record = json.loads((args.data / "test-result.json").read_text())
        uploaded = Path(os.environ["ARTIFACT_EXIT_CODE_FILE"]).read_text().strip() == "0"
        reference = os.environ["ARTIFACT_REFERENCE"]
        if not re.fullmatch(r"quay.io/opendatahub/odh-ci-artifacts:[a-z0-9][a-z0-9.-]{0,127}", reference):
            raise ValueError("invalid artifact reference")
        record["artifacts"] = {"status": "published" if uploaded else "error", "oci_reference": reference,
                               "retention": "30d"}
        if not uploaded:
            record["status"] = "error"
        record = freeze(path, record)
    write(os.environ["TEST_OUTPUT_RESULT"], test_output(record))
    return record


def publish_frozen(args, token=None):
    record = json.loads((args.data / "summary.json").read_text())
    if token is None:
        token = Path("/publisher/token").read_text().strip()
    for attempt in range(3):
        try:
            publish_record(record, token)
            return
        except urllib.error.HTTPError as error:
            if error.code < 500 or attempt == 2:
                raise
        except (urllib.error.URLError, TimeoutError):
            if attempt == 2:
                raise
        time.sleep(2)


def public_summary(record):
    """Reuse Early Gate's counts while retaining the tested image identity."""
    result = {key: record[key] for key in (
        "schema_version", "version", "status", "source_repository", "source_revision",
        "config_revision", "started_at", "finished_at", "architecture", "tiers",
        "agent_sandbox", "agent_sandbox_installation", "pipeline_status",
        "reports_complete", "cluster_cleanup_requested", "artifacts", "group_id",
        "job_url", "example", "example_note") if key in record}
    result["correlation_id"] = record["run_id"]
    counts = record["counts"]
    result["test_summary"] = {"Failed": counts["failed"] + counts["errors"],
                              "Passed": counts["passed"], "Skipped": counts["skipped"],
                              "Total": counts["total"]}
    result["images"] = {role: {key: item[key] for key in (
        "image", "source_revision", "selection_tag", "build") if key in item}
        for role, item in record.get("images", {}).items()}
    return result


def publication_order(record):
    started = dt.datetime.fromisoformat(record["started_at"].replace("Z", "+00:00"))
    if started.tzinfo is None:
        raise ValueError("summary start time must include a timezone")
    return started, record["correlation_id"]


def publish_record(record, token, branch="early-gate", api=request):
    """Update one version summary; Git history retains earlier published runs."""
    import yaml

    if (not VERSION.fullmatch(record["version"])
            or not re.fullmatch(r"[a-z0-9][a-z0-9.-]{0,252}", record["run_id"])):
        raise ValueError("invalid public result path")
    public = public_summary(record)
    order = publication_order(public)
    base = "https://api.github.com/repos/" + METADATA
    path = "openshell/" + record["version"] + "/early-gate-test-summary.yaml"
    for attempt in range(5):
        ref = api(base + "/git/ref/heads/" + branch, token=token)
        head = ref["object"]["sha"]
        try:
            content = api(base + "/contents/" + path + "?ref=" + head, token=token)
            existing = yaml.safe_load(base64.b64decode(content["content"]))
        except urllib.error.HTTPError as exc:
            if exc.code != 404:
                raise
            exc.close()
            existing = None
        if existing is not None:
            if (not isinstance(existing, dict) or existing.get("schema_version") != 1
                    or existing.get("version") != record["version"]):
                raise ValueError("unrecognized existing version summary")
            if existing.get("correlation_id") == public["correlation_id"]:
                if existing != public:
                    raise ValueError("refusing to replace an existing attempt")
                return
            if publication_order(existing) >= order:
                return
        commit = api(base + "/git/commits/" + head, token=token)
        tree = api(base + "/git/trees", token=token, data={"base_tree": commit["tree"]["sha"], "tree": [
            {"path": path, "mode": "100644", "type": "blob",
             "content": yaml.safe_dump(public, sort_keys=False, default_flow_style=False)}]})
        new = api(base + "/git/commits", token=token, data={
            "message": "test(openshell): record " + record["version"] + " " + record["status"],
            "tree": tree["sha"], "parents": [head]})
        try:
            api(base + "/git/refs/heads/" + branch, token=token, method="PATCH",
                data={"sha": new["sha"], "force": False})
            return
        except urllib.error.HTTPError as exc:
            if exc.code not in (409, 422) or attempt == 4:
                raise
            exc.close()
    raise RuntimeError("could not publish result")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("action", choices=("prepare", "install-agent-sandbox", "finalize", "stage-artifacts", "seal", "publish", "enforce"))
    parser.add_argument("--data", type=Path, default=Path("/data"))
    parser.add_argument("--reports", type=Path, default=Path("/reports"))
    parser.add_argument("--artifacts", type=Path, default=Path("/artifacts"))
    args = parser.parse_args()
    if args.action == "prepare":
        prepare(args)
    elif args.action == "install-agent-sandbox":
        install_agent_sandbox(args)
    elif args.action == "finalize":
        finalize(args)
    elif args.action == "stage-artifacts":
        stage_artifacts(args)
    elif args.action == "seal":
        seal(args)
    elif args.action == "publish":
        publish_frozen(args)
    elif json.loads((args.data / "summary.json").read_text())["status"] != "passed":
        raise SystemExit(1)


if __name__ == "__main__":
    main()
