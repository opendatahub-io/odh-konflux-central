"""Retain successful build evidence and dispatch immutable version-image groups."""

import argparse
import hashlib
import json
import os
import re
import urllib.error
import urllib.parse

import version_tests as v

PREFIX = "openshell.opendatahub.io/"
CONFIGMAPS = "/api/v1/namespaces/{namespace}/configmaps"
RUNS = "tekton.dev/v1/namespaces/{namespace}/pipelineruns"
SNAPSHOTS = "appstudio.redhat.com/v1alpha1/namespaces/{namespace}/snapshots"
FINALIZER = PREFIX + "dispatch-recorded"
COMPONENTS = {"odh-openshell-" + role + "-ci": role for role in v.ROLES}


def identity(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def metadata_value(snapshot, suffix, prefixes):
    meta = snapshot.get("metadata", {})
    values = {meta.get(kind, {}).get(prefix + "/" + suffix)
              for kind in ("labels", "annotations") for prefix in prefixes}
    values.discard(None)
    values.discard("")
    if len(values) > 1:
        raise ValueError("conflicting Snapshot metadata: " + suffix)
    return next(iter(values), "")


def build_record(snapshot):
    """Only controller-created component push Snapshots attest successful builds."""
    pac = ("pac.test.appstudio.openshift.io",)
    event = metadata_value(snapshot, "event-type", pac)
    pr = metadata_value(snapshot, "pull-request", pac)
    branch = metadata_value(snapshot, "branch", pac)
    component = metadata_value(snapshot, "component", ("appstudio.openshift.io", "build.appstudio.redhat.com"))
    if event != "push" or pr or component not in COMPONENTS:
        return None
    role = COMPONENTS[component]
    tag = branch.removeprefix("refs/tags/")
    if role == "e2e":
        if branch not in ("main", "refs/heads/main"):
            return None
        tag = "odh-stable"
    elif not v.VERSION.fullmatch(tag):
        return None
    meta = snapshot["metadata"]
    build = metadata_value(snapshot, "build-pipelinerun", ("appstudio.openshift.io", "integration.konflux-ci.dev"))
    if not build or not meta.get("uid") or not meta.get("creationTimestamp"):
        raise ValueError("missing successful build Snapshot provenance")
    matches = [item for item in snapshot["spec"]["components"] if item["name"] == component]
    if len(matches) != 1:
        raise ValueError("missing or duplicate triggering component")
    entry = matches[0]
    git = entry["source"]["git"]
    if git["url"].removesuffix(".git") != v.SOURCE or not v.SHA.fullmatch(git["revision"]):
        raise ValueError("untrusted build source")
    repository = "quay.io/opendatahub/odh-openshell-" + role
    reference, separator, digest = entry["containerImage"].partition("@")
    if (not separator or reference not in (repository, repository + ":" + tag)
            or not v.DIGEST.fullmatch(digest)):
        raise ValueError("build Snapshot has an invalid image reference")
    return {"role": role, "selection_tag": tag, "image": repository + "@" + digest,
            "source_revision": git["revision"], "build": build,
            "completed_at": meta["creationTimestamp"],
            "evidence": {"snapshot": meta["name"], "snapshot_uid": meta["uid"]}}


def get(path):
    try:
        return v.cluster_request(path)
    except urllib.error.HTTPError as error:
        if error.code != 404:
            raise
        error.close()
        return None


def put_new(path, item):
    try:
        return v.cluster_request(path, data=item, method="POST"), True
    except urllib.error.HTTPError as error:
        if error.code != 409:
            raise
        error.close()
        return get(path + "/" + item["metadata"]["name"]), False


def configmap(name, kind, value):
    return {"apiVersion": "v1", "kind": "ConfigMap", "metadata": {
        "name": name, "labels": {PREFIX + "kind": kind}}, "immutable": True,
        "data": {"record.json": json.dumps(value, sort_keys=True)}}


def collect_records():
    retained = [json.loads(cm["data"]["record.json"]) for cm in v.cluster_collection(
        CONFIGMAPS, {"labelSelector": PREFIX + "kind=build"}) if cm.get("immutable")]
    seen = {record["evidence"]["snapshot_uid"] for record in retained}
    selector = "appstudio.openshift.io/component in (" + ",".join(COMPONENTS) + ")"
    for snapshot in v.cluster_collection(SNAPSHOTS, {"labelSelector": selector}):
        if snapshot["metadata"].get("uid") in seen:
            continue
        record = build_record(snapshot)
        if record is None:
            continue
        saved, _ = put_new(CONFIGMAPS, configmap("openshell-build-" + identity(record)[:40], "build", record))
        if not saved.get("immutable") or json.loads(saved["data"]["record.json"]) != record:
            raise ValueError("refusing changed build evidence")
        retained.append(record)
        seen.add(record["evidence"]["snapshot_uid"])
    return retained


def groups(records, tiers):
    """Keep the versioned runtime set and runner on one source commit."""
    result = []
    versions = sorted({item["selection_tag"] for item in records if item["role"] == "gateway"})
    for version in versions:
        if not v.VERSION.fullmatch(version):
            continue
        selected = {}
        for role in v.ROLES[:3]:
            matches = [r for r in records if r["role"] == role and r["selection_tag"] == version]
            if not matches:
                break
            selected[role] = max(matches, key=lambda r: (r["completed_at"], r["evidence"]["snapshot_uid"]))
        if len(selected) != 3 or len({item["source_revision"] for item in selected.values()}) != 1:
            continue
        revision = selected["gateway"]["source_revision"]
        runners = [r for r in records if r["role"] == "e2e" and r["source_revision"] == revision]
        if not runners:
            continue
        selected["e2e"] = max(runners, key=lambda r: (r["completed_at"], r["evidence"]["snapshot_uid"]))
        group = {"schema_version": 1, "version": version, "source_revision": revision,
                 "architecture": "amd64", "tiers": tiers, "agent_sandbox": v.AGENT_SANDBOX,
                 "images": selected}
        group["group_id"] = v.group_identity(group)
        result.append(group)
    return result


def registry_ready(group, api=v.request):
    for role, image in group["images"].items():
        repository = "opendatahub/odh-openshell-" + role
        base = "https://quay.io/api/v1/repository/" + repository
        digest = image["image"].split("@")[1]
        try:
            manifest = api(base + "/manifest/" + digest)
            document = json.loads(manifest["manifest_data"])
            if "manifests" in document and not any(
                    part.get("platform", {}).get("architecture") == "amd64"
                    and part.get("platform", {}).get("os") == "linux" for part in document["manifests"]):
                raise ValueError("required amd64 platform is absent: " + role)
            if role != "e2e":
                query = urllib.parse.urlencode({"specificTag": group["version"], "onlyActiveTags": "true"})
                tags = api(base + "/tag/?" + query)["tags"]
                if not any(t["name"] == group["version"] and t["manifest_digest"] == digest for t in tags):
                    return False
        except urllib.error.HTTPError as error:
            if error.code == 404:
                error.close()
                return False
            raise
    return True


def worker(group, config_revision, attempt):
    if not v.SHA.fullmatch(config_revision) or not re.fullmatch(r"[1-9][0-9]{0,3}", str(attempt)):
        raise ValueError("pin the central commit and use a positive attempt number")
    name = "openshell-version-" + group["group_id"][:24] + "-" + str(attempt)
    return {"apiVersion": "tekton.dev/v1", "kind": "PipelineRun", "metadata": {
        "name": name, "finalizers": [FINALIZER], "labels": {
            PREFIX + "kind": "qualification", PREFIX + "group": group["group_id"][:40]}},
        "spec": {"timeouts": {"pipeline": "5h", "tasks": "4h30m", "finally": "30m"},
                 "taskRunTemplate": {"serviceAccountName": "konflux-integration-runner",
                     "podTemplate": {"nodeSelector": {"kubernetes.io/arch": "amd64"},
                                     "securityContext": {"fsGroup": 1001}}},
                 "taskRunSpecs": [{"pipelineTaskName": task, "serviceAccountName": "openshell-version-test-workload"}
                                  for task in ("install-agent-sandbox", "run-tests")],
                 "pipelineRef": {"resolver": "git", "params": [
                     {"name": "url", "value": "https://github.com/opendatahub-io/odh-konflux-central.git"},
                     {"name": "revision", "value": config_revision},
                     {"name": "pathInRepo", "value": "integration-tests/openshell/version-tests.yaml"}]},
                 "params": [{"name": "group-id", "value": group["group_id"]},
                            {"name": "config-revision", "value": config_revision}],
                 "workspaces": [{"name": name, "volumeClaimTemplate": {"spec": {
                     "accessModes": ["ReadWriteOnce"], "resources": {"requests": {"storage": size}}}}}
                     for name, size in (("config", "100Mi"), ("reports", "1Gi"))]}}


def release_dispatch(run):
    finalizers = run["metadata"].get("finalizers", [])
    if FINALIZER in finalizers:
        v.cluster_request(RUNS + "/" + run["metadata"]["name"], method="PATCH", content_type="application/merge-patch+json",
                          data={"metadata": {"resourceVersion": run["metadata"]["resourceVersion"],
                                             "finalizers": [f for f in finalizers if f != FINALIZER]}})


def dispatch(group, revision, attempt=1):
    name = "openshell-group-" + group["group_id"][:40]
    canonical, _ = put_new(CONFIGMAPS, configmap(name, "group", group))
    if not canonical.get("immutable"):
        raise ValueError("qualification group must be immutable")
    retained = v.validate_group(json.loads(canonical["data"]["record.json"]))
    if retained["group_id"] != group["group_id"]:
        raise ValueError("qualification group identity collision")
    run = worker(retained, revision, attempt)
    path = RUNS + "/" + run["metadata"]["name"]
    key = PREFIX + "attempt-" + str(attempt)
    recorded_uid = canonical["metadata"].get("annotations", {}).get(key)
    if recorded_uid:
        existing = get(path)
        if existing:
            if existing["metadata"]["uid"] != recorded_uid:
                raise ValueError("qualification run identity changed")
            release_dispatch(existing)
        return False
    saved, created = put_new(RUNS, run)
    if saved["metadata"].get("labels", {}).get(PREFIX + "group") != group["group_id"][:40]:
        raise ValueError("qualification name collision")
    # Immutable data holds the image set; mutable metadata records dispatch recovery.
    v.cluster_request(CONFIGMAPS + "/" + name, method="PATCH", content_type="application/merge-patch+json",
                      data={"metadata": {"resourceVersion": canonical["metadata"]["resourceVersion"],
                                         "annotations": {key: saved["metadata"]["uid"]}}})
    release_dispatch(saved)
    return created


def reconcile(revision, tiers, version=None, attempt=1):
    if not v.SHA.fullmatch(revision) or not tiers or len(tiers) != len(set(tiers)) or set(tiers) - v.TIERS:
        raise ValueError("invalid configuration revision or test tiers")
    launched = 0
    for group in groups(collect_records(), tiers):
        if version and group["version"] != version:
            continue
        canonical = get(CONFIGMAPS + "/openshell-group-" + group["group_id"][:40])
        if canonical and canonical["metadata"].get("annotations", {}).get(PREFIX + "attempt-" + str(attempt)):
            dispatch(group, revision, attempt)
        elif registry_ready(group):
            launched += dispatch(group, revision, attempt)
    return launched


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--version")
    parser.add_argument("--attempt", type=int, default=1)
    args = parser.parse_args()
    print("New qualification runs: " + str(reconcile(os.environ["CONFIG_REVISION"],
          os.environ.get("TEST_TIERS", "smoke").split(","), args.version, args.attempt)))


if __name__ == "__main__":
    main()
