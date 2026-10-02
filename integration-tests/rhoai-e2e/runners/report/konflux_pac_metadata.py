"""Resolve upstream GitHub PRs and PAC metadata for Konflux Activity columns."""

from __future__ import annotations

import json
import re
import shutil
import urllib.parse
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from suite.constants import (
    ANNOTATION_BUILD_COMMIT_SHA,
    ANNOTATION_BUILD_REPO,
    ANNOTATION_SHA_URL,
    ANNOTATION_TARGET_BRANCH,
    DEFAULT_UPSTREAM_KONFLUX_GIT,
    LABEL_PAC_PULL_REQUEST,
    LABEL_TEST_PULL_REQUEST,
    LABEL_TEST_SHA,
    LABEL_TEST_URL_ORG,
    LABEL_TEST_URL_REPOSITORY,
    LABEL_TRIGGER_EVENT_TYPE,
)
from k8s.github_url import normalize_https_git_url, parse_github_org_repo
from k8s.oc_util import run_cmd

_PAC_PREFIX = "pac.test.appstudio.openshift.io/"
_PAC_BUILD_KEYS = (
    ANNOTATION_BUILD_REPO,
    ANNOTATION_BUILD_COMMIT_SHA,
    ANNOTATION_TARGET_BRANCH,
    ANNOTATION_SHA_URL,
    "pac.test.appstudio.openshift.io/repo-url",
    "pac.test.appstudio.openshift.io/sha-url",
    "pac.test.appstudio.openshift.io/sha-title",
)

_GIT_SHA_RE = re.compile(r"^[0-9a-f]{40}$", re.IGNORECASE)


@dataclass(frozen=True)
class UpstreamPullRequest:
    number: str
    head_sha: str
    base_branch: str
    pr_org: str
    pr_repo: str


def _gh_json(args: list[str]) -> list[dict[str, Any]] | dict[str, Any] | None:
    if not shutil.which("gh"):
        return None
    proc = run_cmd(["gh", *args], capture=True, check=False, timeout=30)
    if proc.returncode != 0 or not (proc.stdout or "").strip():
        return None
    try:
        return json.loads(proc.stdout)
    except json.JSONDecodeError:
        return None


def find_upstream_pull_request(
    *,
    head_git_url: str,
    branch: str,
    upstream_git_url: str = DEFAULT_UPSTREAM_KONFLUX_GIT,
) -> UpstreamPullRequest | None:
    """Find an open PR on upstream whose head is ``fork:branch`` or ``branch`` on upstream."""
    branch = (branch or "").strip()
    if not branch:
        return None
    up_org, up_repo = parse_github_org_repo(normalize_https_git_url(upstream_git_url))
    if not up_org or not up_repo:
        return None
    head_org, head_repo = parse_github_org_repo(normalize_https_git_url(head_git_url))
    if not head_org:
        return None
    if head_org.lower() == up_org.lower() and head_repo.lower() == up_repo.lower():
        head_ref = branch
    else:
        head_ref = f"{head_org}:{branch}"

    for state in ("open", "merged"):
        data = _gh_json(
            [
                "pr",
                "list",
                "--repo",
                f"{up_org}/{up_repo}",
                "--head",
                head_ref,
                "--state",
                state,
                "--json",
                "number,headRefOid,baseRefName",
                "--limit",
                "1",
            ]
        )
        if not isinstance(data, list) or not data:
            continue
        row = data[0]
        num = str(row.get("number") or "").strip()
        sha = (row.get("headRefOid") or "").strip()
        if num and sha:
            return UpstreamPullRequest(
                number=num,
                head_sha=sha,
                base_branch=(row.get("baseRefName") or "").strip(),
                pr_org=up_org,
                pr_repo=up_repo,
            )
    return None


def _git_ls_remote_branch_sha(*, git_url: str, branch: str) -> str:
    if not shutil.which("git"):
        return ""
    base = normalize_https_git_url(git_url)
    if not base:
        return ""
    remote = base if base.endswith(".git") else f"{base}.git"
    proc = run_cmd(
        ["git", "ls-remote", remote, f"refs/heads/{branch}"],
        capture=True,
        check=False,
        timeout=60,
    )
    if proc.returncode != 0:
        return ""
    for line in (proc.stdout or "").splitlines():
        parts = line.split()
        if len(parts) >= 2 and parts[1] == f"refs/heads/{branch}":
            sha = parts[0].strip()
            if _GIT_SHA_RE.match(sha):
                return sha
    return ""


def _git_rev_parse_sha(*, repo_root: Path, branch: str) -> str:
    if not shutil.which("git") or not branch:
        return ""
    root = repo_root.resolve()
    if not (root / ".git").exists():
        return ""
    proc_branch = run_cmd(
        ["git", "rev-parse", "--abbrev-ref", "HEAD"],
        cwd=str(root),
        capture=True,
        check=False,
        timeout=15,
    )
    on_branch = (proc_branch.stdout or "").strip() if proc_branch.returncode == 0 else ""
    refs = [branch]
    if on_branch == branch:
        refs.append("HEAD")
    for ref in refs:
        proc = run_cmd(
            ["git", "rev-parse", ref],
            cwd=str(root),
            capture=True,
            check=False,
            timeout=15,
        )
        if proc.returncode != 0:
            continue
        sha = (proc.stdout or "").strip()
        if _GIT_SHA_RE.match(sha):
            return sha
    return ""


def resolve_branch_head_sha(
    *,
    git_url: str,
    branch: str,
    local_repo: Path | None = None,
) -> str:
    """Best-effort commit SHA for a branch (gh API, git ls-remote, or local checkout)."""
    branch = (branch or "").strip()
    if not branch:
        return ""
    org, repo = parse_github_org_repo(normalize_https_git_url(git_url))
    if org and repo and shutil.which("gh"):
        proc = run_cmd(
            ["gh", "api", f"repos/{org}/{repo}/commits/{urllib.parse.quote(branch, safe='')}", "--jq", ".sha"],
            capture=True,
            check=False,
            timeout=30,
        )
        if proc.returncode == 0:
            sha = (proc.stdout or "").strip().strip('"')
            if _GIT_SHA_RE.match(sha):
                return sha

    sha = _git_ls_remote_branch_sha(git_url=git_url, branch=branch)
    if sha:
        return sha

    if local_repo is not None:
        return _git_rev_parse_sha(repo_root=local_repo, branch=branch)
    return ""


def extract_pac_metadata_from_resource(meta: dict[str, Any] | None) -> tuple[dict[str, str], dict[str, str]]:
    """Copy PAC / build labels and annotations from a Snapshot (or PipelineRun) metadata."""
    if not meta:
        return {}, {}
    labels_in = meta.get("labels") or {}
    ann_in = meta.get("annotations") or {}
    labels: dict[str, str] = {}
    annotations: dict[str, str] = {}
    for key, val in labels_in.items():
        text = (val or "").strip()
        if not text:
            continue
        if key.startswith(_PAC_PREFIX) or key == LABEL_PAC_PULL_REQUEST:
            labels[key] = text
    for key, val in ann_in.items():
        text = (val or "").strip()
        if not text:
            continue
        if key.startswith(_PAC_PREFIX) or key.startswith("build.appstudio.") or key in _PAC_BUILD_KEYS:
            annotations[key] = text
    return labels, annotations


def snapshot_has_pull_request_pac(labels: dict[str, str]) -> bool:
    event = (labels.get(LABEL_TRIGGER_EVENT_TYPE) or "").strip()
    if event == "pull_request":
        return True
    return bool((labels.get(LABEL_TEST_PULL_REQUEST) or labels.get(LABEL_PAC_PULL_REQUEST) or "").strip())


def build_pull_request_pac_metadata(
    *,
    pr: UpstreamPullRequest,
    repo_git_url: str | None = None,
) -> tuple[dict[str, str], dict[str, str]]:
    """Konflux Activity PR row metadata (upstream repo + PR number + head SHA)."""
    git_base = normalize_https_git_url(repo_git_url or f"https://github.com/{pr.pr_org}/{pr.pr_repo}.git")
    sha = pr.head_sha
    labels = {
        LABEL_TRIGGER_EVENT_TYPE: "pull_request",
        LABEL_TEST_URL_ORG: pr.pr_org,
        LABEL_TEST_URL_REPOSITORY: pr.pr_repo,
        LABEL_TEST_SHA: sha,
        LABEL_TEST_PULL_REQUEST: pr.number,
        LABEL_PAC_PULL_REQUEST: pr.number,
    }
    annotations = {
        ANNOTATION_BUILD_REPO: f"{git_base}?rev={sha}",
        ANNOTATION_BUILD_COMMIT_SHA: sha,
        ANNOTATION_SHA_URL: f"{git_base}/commit/{sha}",
    }
    if pr.base_branch:
        annotations[ANNOTATION_TARGET_BRANCH] = pr.base_branch
    return annotations, labels
