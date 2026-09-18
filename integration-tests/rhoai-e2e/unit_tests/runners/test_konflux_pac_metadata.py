"""Unit tests for upstream PR / PAC metadata helpers."""

from __future__ import annotations

from suite.constants import (
    ANNOTATION_BUILD_COMMIT_SHA,
    ANNOTATION_BUILD_REPO,
    ANNOTATION_SHA_URL,
    ANNOTATION_TARGET_BRANCH,
    EVENT_TYPE_PULL_REQUEST,
    LABEL_PAC_PULL_REQUEST,
    LABEL_TEST_PULL_REQUEST,
    LABEL_TEST_SHA,
    LABEL_TEST_URL_ORG,
    LABEL_TEST_URL_REPOSITORY,
    LABEL_TRIGGER_EVENT_TYPE,
)
from runners.report.konflux_pac_metadata import (
    UpstreamPullRequest,
    build_pull_request_pac_metadata,
    extract_pac_metadata_from_resource,
    resolve_branch_head_sha,
    snapshot_has_pull_request_pac,
)
from runners.report.pipelinerun_metadata import build_konflux_activity_metadata

def test_build_pull_request_pac_metadata() -> None:
    pr = UpstreamPullRequest(
        number="42",
        head_sha="abc123def456",
        base_branch="main",
        pr_org="opendatahub-io",
        pr_repo="odh-konflux-central",
    )
    ann, labels = build_pull_request_pac_metadata(pr=pr)
    assert labels[LABEL_TRIGGER_EVENT_TYPE] == EVENT_TYPE_PULL_REQUEST
    assert labels[LABEL_TEST_URL_ORG] == "opendatahub-io"
    assert labels[LABEL_TEST_URL_REPOSITORY] == "odh-konflux-central"
    assert labels[LABEL_TEST_PULL_REQUEST] == "42"
    assert labels[LABEL_PAC_PULL_REQUEST] == "42"
    assert labels[LABEL_TEST_SHA] == "abc123def456"
    assert ann[ANNOTATION_BUILD_COMMIT_SHA] == "abc123def456"
    assert ann[ANNOTATION_BUILD_REPO].endswith("?rev=abc123def456")
    assert ann[ANNOTATION_SHA_URL].endswith("/commit/abc123def456")
    assert ann[ANNOTATION_TARGET_BRANCH] == "main"

def test_extract_pac_metadata_from_build_snapshot() -> None:
    meta = {
        "labels": {
            "pac.test.appstudio.openshift.io/event-type": "pull_request",
            "pac.test.appstudio.openshift.io/url-org": "opendatahub-io",
            "pac.test.appstudio.openshift.io/url-repository": "odh-konflux-central",
            "pac.test.appstudio.openshift.io/pull-request": "99",
            "pac.test.appstudio.openshift.io/sha": "deadbeef",
            "unrelated": "skip",
        },
        "annotations": {
            "build.appstudio.openshift.io/repo": "https://github.com/opendatahub-io/odh-konflux-central.git?rev=deadbeef",
            "build.appstudio.redhat.com/commit_sha": "deadbeef",
            "pipelinesascode.tekton.dev/sha-url": "https://github.com/opendatahub-io/odh-konflux-central/commit/deadbeef",
            "other": "skip",
        },
    }
    labels, ann = extract_pac_metadata_from_resource(meta)
    assert snapshot_has_pull_request_pac(labels)
    assert labels["pac.test.appstudio.openshift.io/event-type"] == "pull_request"
    assert ann["build.appstudio.redhat.com/commit_sha"] == "deadbeef"
    assert "other" not in ann

def test_build_konflux_activity_metadata_copies_fbc_snapshot_pac() -> None:
    fbc_meta = {
        "labels": {
            "pac.test.appstudio.openshift.io/event-type": "pull_request",
            "pac.test.appstudio.openshift.io/url-org": "opendatahub-io",
            "pac.test.appstudio.openshift.io/url-repository": "odh-konflux-central",
            "pac.test.appstudio.openshift.io/pull-request": "77",
            "pac.test.appstudio.openshift.io/sha": "cafebabe",
        },
        "annotations": {
            "build.appstudio.openshift.io/repo": "https://github.com/opendatahub-io/odh-konflux-central.git?rev=cafebabe",
            "build.appstudio.redhat.com/commit_sha": "cafebabe",
        },
    }
    ann, labels = build_konflux_activity_metadata(
        fbcf_image="quay.io/rhoai/rhoai-fbc-fragment@sha256:ab0042e",
        scripts_git_url="https://github.com/manosnoam/odh-konflux-central.git",
        scripts_git_revision="olminstall_smoke",
        fbc_snapshot_meta=fbc_meta,
    )
    assert labels[LABEL_TRIGGER_EVENT_TYPE] == EVENT_TYPE_PULL_REQUEST
    assert labels[LABEL_TEST_PULL_REQUEST] == "77"
    assert ann[ANNOTATION_BUILD_COMMIT_SHA] == "cafebabe"

def test_resolve_branch_head_sha_git_ls_remote() -> None:
    sha = resolve_branch_head_sha(
        git_url="https://github.com/manosnoam/odh-konflux-central.git",
        branch="olminstall_smoke",
    )
    assert len(sha) == 40
    assert all(c in "0123456789abcdef" for c in sha)
