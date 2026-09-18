"""Unit tests for CLI trigger / Reference metadata helpers."""

from __future__ import annotations

from pathlib import Path

import pytest

from suite.constants import (
    ANNOTATION_BUILD_COMMIT_SHA,
    ANNOTATION_BUILD_REPO,
    ANNOTATION_FBCF_IMAGE,
    ANNOTATION_REFERENCE,
    ANNOTATION_SHA_URL,
    ANNOTATION_TARGET_BRANCH,
    ANNOTATION_TRIGGER_COMMAND,
    ANNOTATION_TRIGGER_TYPE,
    EVENT_TYPE_INCOMING,
    EVENT_TYPE_PUSH,
    PIPELINE_TYPE_TEST,
    LABEL_CLUSTER,
    LABEL_KONFLUX_APPLICATION,
    LABEL_KONFLUX_PIPELINE_TYPE,
    LABEL_PRODUCT,
    LABEL_RUN_OWNER,
    LABEL_TARGET,
    LABEL_TEST_SHA,
    LABEL_TEST_URL_ORG,
    LABEL_TEST_URL_REPOSITORY,
    LABEL_TRIGGER_EVENT_TYPE,
    TRIGGER_TYPE_MANUAL,
)
from runners.report.pipelinerun_metadata import (
    build_cli_trigger_labels,
    build_cli_trigger_metadata,
    build_konflux_activity_metadata,
    build_konflux_test_pipelinerun_type_labels,
    build_manual_snapshot_trigger_labels,
    build_reference_text,
    format_rhoai_e2e_trigger_command,
    quay_manifest_web_url,
    quay_repository_web_url,
    shell_quote_arg,
    short_digest_from_image,
)

from unit_tests._paths import RHOAI_E2E_ROOT

_RHOAI_E2E_DIR = RHOAI_E2E_ROOT
_FBC = (
    "quay.io/rhoai/rhoai-fbc-fragment@sha256:"
    "ab0042e79c995ace875bf5624c6a7e98fe082c833b39bbc0ea9b0c16399496a9"
)
_SCRIPTS_GIT = "https://github.com/manosnoam/odh-konflux-central.git"
_SCRIPTS_REV = "olminstall_smoke"

@pytest.fixture
def no_github_network(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep branch-push tests offline and independent of open PRs."""
    import runners.report.pipelinerun_metadata as pr_meta

    monkeypatch.setattr(pr_meta, "find_upstream_pull_request", lambda **_: None)
    monkeypatch.setattr(pr_meta, "resolve_branch_head_sha", lambda **_: "")

def test_shell_quote_arg_plain() -> None:
    assert shell_quote_arg("rhoai") == "rhoai"

def test_shell_quote_arg_spaces() -> None:
    assert shell_quote_arg("has space") == "'has space'"

def test_format_trigger_command_relative_prog() -> None:
    cmd = format_rhoai_e2e_trigger_command(
        _RHOAI_E2E_DIR,
        ["--product", "rhoai", "--tests", "bvt"],
    )
    assert cmd.startswith("python3 integration-tests/rhoai-e2e/rhoai_e2e.py")
    assert "--product rhoai" in cmd
    assert "--tests bvt" in cmd

def test_short_digest_from_image() -> None:
    assert short_digest_from_image(_FBC) == "ab0042e79c99"

def test_quay_web_urls_from_pullspec() -> None:
    assert quay_repository_web_url(_FBC) == "https://quay.io/repository/rhoai/rhoai-fbc-fragment"
    assert quay_manifest_web_url(_FBC).endswith(
        "ab0042e79c995ace875bf5624c6a7e98fe082c833b39bbc0ea9b0c16399496a9"
    )

def test_build_konflux_test_pipelinerun_type_labels() -> None:
    labels = build_konflux_test_pipelinerun_type_labels()
    assert labels == {LABEL_KONFLUX_PIPELINE_TYPE: PIPELINE_TYPE_TEST}

def test_build_reference_with_ocp() -> None:
    assert build_reference_text(fbcf_image=_FBC, ocp_version="4.21") == f"{_FBC} · OCP 4.21"

def test_build_konflux_activity_metadata_branch_push_without_gh(no_github_network: None) -> None:
    ann, labels = build_konflux_activity_metadata(
        fbcf_image=_FBC,
        scripts_git_url=_SCRIPTS_GIT,
        scripts_git_revision=_SCRIPTS_REV,
    )
    assert labels[LABEL_TRIGGER_EVENT_TYPE] == EVENT_TYPE_PUSH
    assert labels[LABEL_TEST_URL_ORG] == "manosnoam"
    assert labels[LABEL_TEST_URL_REPOSITORY] == "odh-konflux-central"
    assert LABEL_TEST_SHA not in labels
    assert ann[ANNOTATION_BUILD_REPO].startswith("https://github.com/manosnoam/odh-konflux-central")
    assert "rev=olminstall_smoke" in ann[ANNOTATION_BUILD_REPO]
    assert ANNOTATION_BUILD_COMMIT_SHA not in ann
    assert ann[ANNOTATION_TARGET_BRANCH] == _SCRIPTS_REV
    assert ann[ANNOTATION_SHA_URL].endswith("/tree/olminstall_smoke")
    assert "quay.io/" not in ann[ANNOTATION_BUILD_REPO]

def test_build_konflux_activity_metadata_incoming_when_no_git_rev() -> None:
    ann, labels = build_konflux_activity_metadata(
        fbcf_image=_FBC,
        scripts_git_url="",
        scripts_git_revision="",
    )
    assert labels[LABEL_TRIGGER_EVENT_TYPE] == EVENT_TYPE_INCOMING
    assert labels[LABEL_TEST_SHA] == "ab0042e79c99"
    assert ann[ANNOTATION_SHA_URL].startswith("https://quay.io/repository/rhoai/rhoai-fbc-fragment/manifest/sha256:")

def test_build_cli_trigger_metadata_populates_konflux_and_rhoai_e2e_fields(no_github_network: None) -> None:
    meta = build_cli_trigger_metadata(
        script_dir=_RHOAI_E2E_DIR,
        trigger_argv=["--product", "", "--tests", "bvt"],
        product="",
        tests="bvt",
        fbcf_image=_FBC,
        ocp_version="4.20",
        scripts_git_url=_SCRIPTS_GIT,
        scripts_git_revision=_SCRIPTS_REV,
    )
    assert meta[ANNOTATION_TRIGGER_TYPE] == TRIGGER_TYPE_MANUAL
    assert "rhoai_e2e.py" in meta[ANNOTATION_TRIGGER_COMMAND]
    assert meta[ANNOTATION_FBCF_IMAGE] == _FBC
    assert _FBC in meta[ANNOTATION_REFERENCE]
    assert meta[ANNOTATION_SHA_URL].endswith("/tree/olminstall_smoke")
    assert ANNOTATION_BUILD_COMMIT_SHA not in meta
    assert meta[ANNOTATION_BUILD_REPO].startswith("https://")

def test_build_cli_trigger_labels(no_github_network: None) -> None:
    labels = build_cli_trigger_labels(
        fbcf_image=_FBC,
        scripts_git_url=_SCRIPTS_GIT,
        scripts_git_revision=_SCRIPTS_REV,
    )
    assert labels[LABEL_TRIGGER_EVENT_TYPE] == EVENT_TYPE_PUSH

def test_build_manual_snapshot_trigger_labels(no_github_network: None) -> None:
    labels = build_manual_snapshot_trigger_labels(
        application="testops-playpen",
        run_owner="nmanos",
        product="",
        target_type="external",
        cluster="nmanos-konflux1",
        fbcf_image=_FBC,
        scripts_git_url=_SCRIPTS_GIT,
        scripts_git_revision=_SCRIPTS_REV,
    )
    assert labels[LABEL_TRIGGER_EVENT_TYPE] == EVENT_TYPE_PUSH
    assert labels[LABEL_KONFLUX_APPLICATION] == "testops-playpen"
    assert labels[LABEL_RUN_OWNER] == "nmanos"
    assert labels.get(LABEL_PRODUCT, "") == ""
    assert labels[LABEL_CLUSTER] == "nmanos-konflux1"
    assert labels[LABEL_TARGET] == "external"
    assert labels[LABEL_TEST_URL_ORG] == "manosnoam"
    assert labels[LABEL_TEST_URL_REPOSITORY] == "odh-konflux-central"


@pytest.mark.parametrize(
    "rel_path",
    [
        "tekton/pipelines/rhoai-e2e-pipelinerun.yaml",
        "tekton/pipelines/rhoai-e2e-pipelinerun-rh-nightly.yaml",
    ],
)
def test_its_pipelinerun_template_has_push_event_type(rel_path: str) -> None:
    """Integration Service runs use this template; Konflux Activity Trigger reads PAC event-type."""
    import yaml

    path = _RHOAI_E2E_DIR / rel_path
    doc = yaml.safe_load(path.read_text(encoding="utf-8"))
    labels = (doc.get("metadata") or {}).get("labels") or {}
    assert labels.get(LABEL_TRIGGER_EVENT_TYPE) == EVENT_TYPE_PUSH
