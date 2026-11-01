"""Map IntegrationTestScenario ``resolverRef.pathInRepo`` to Tekton git ``pipelineRef`` paths."""

from __future__ import annotations

import re
import urllib.error
import urllib.request
from pathlib import Path

from suite.errors import AppError
from suite.its_registry import (
    load_colocated_pipelinerun_shell,
    load_colocated_pipelinerun_shell_from_text,
    pipelinerun_shell_git_pipeline_path,
)

# Default when the ITS omits pathInRepo (current rhoai-e2e tree).
DEFAULT_RHOAI_E2E_GIT_PIPELINE_PATH = (
    "integration-tests/rhoai-e2e/tekton/pipelines/rhoai-e2e-pipeline.yaml"
)

# ITS wrappers: ``*-pipelinerun[-suffix].yaml`` → sibling ``*-pipeline.yaml`` (main olminstall + rhoai-e2e).
_PIPELINERUN_WRAPPER_RE = re.compile(
    r"^(?P<dir>.*/)(?P<stem>[\w-]+)-pipelinerun(?:-[a-z0-9][a-z0-9-]*)?\.yaml$",
    re.IGNORECASE,
)
_RHOAI_E2E_ITS_MANIFEST_RE = re.compile(
    r"^integration-tests/rhoai-e2e/tekton/its/its-[a-z0-9][a-z0-9-]*\.yaml$",
    re.IGNORECASE,
)
_GITHUB_OWNER_REPO_RE = re.compile(r"github\.com[:/]+(?P<owner>[^/]+)/(?P<repo>[^/]+)", re.IGNORECASE)
_FETCH_USER_AGENT = "rhoai-e2e-its-git-pipeline-path"
_FETCH_TIMEOUT_SEC = 60


def _github_owner_repo(git_url: str) -> str:
    text = (git_url or "").strip().rstrip("/")
    if text.endswith(".git"):
        text = text[:-4]
    match = _GITHUB_OWNER_REPO_RE.search(text)
    if not match:
        return ""
    return f"{match.group('owner')}/{match.group('repo')}"


def _fetch_repo_file_text(*, git_url: str, git_revision: str, path_in_repo: str) -> str:
    owner_repo = _github_owner_repo(git_url)
    rev = (git_revision or "main").strip()
    path = path_in_repo.strip().lstrip("/")
    if not owner_repo or not path:
        return ""
    url = f"https://raw.githubusercontent.com/{owner_repo}/{rev}/{path}"
    req = urllib.request.Request(url, headers={"User-Agent": _FETCH_USER_AGENT})
    try:
        with urllib.request.urlopen(req, timeout=_FETCH_TIMEOUT_SEC) as resp:
            return resp.read().decode("utf-8")
    except (urllib.error.HTTPError, urllib.error.URLError, TimeoutError):
        return ""


def _local_manifest_path(path_in_repo: str, local_repo_root: Path | None) -> Path | None:
    path = path_in_repo.strip().lstrip("/")
    if local_repo_root is None or not path:
        return None
    root = local_repo_root.resolve()
    local_path = (root / path).resolve()
    try:
        local_path.relative_to(root)
    except ValueError:
        return None
    return local_path if local_path.is_file() else None


def _pipeline_path_from_colocated_its_manifest(
    path_in_repo: str,
    *,
    git_url: str,
    git_revision: str,
    local_repo_root: Path | None,
    default: str,
) -> str:
    local_path = _local_manifest_path(path_in_repo, local_repo_root)
    if local_path is not None:
        shell = load_colocated_pipelinerun_shell(local_path)
        resolved = pipelinerun_shell_git_pipeline_path(shell)
        return resolved or default
    if not (git_url or "").strip():
        return default
    raw = _fetch_repo_file_text(
        git_url=git_url,
        git_revision=git_revision,
        path_in_repo=path_in_repo,
    )
    if not raw:
        return default
    try:
        shell = load_colocated_pipelinerun_shell_from_text(raw)
    except AppError:
        return default
    resolved = pipelinerun_shell_git_pipeline_path(shell)
    return resolved or default


def resolve_git_pipeline_path_from_its_resolver(
    path_in_repo: str,
    *,
    git_url: str = "",
    git_revision: str = "",
    local_repo_root: Path | None = None,
    default: str = DEFAULT_RHOAI_E2E_GIT_PIPELINE_PATH,
) -> str:
    """
    Return the git path Tekton should load for ``pipelineRef`` on CLI direct triggers.

    Integration Service ITS objects often set ``resourceKind: pipelinerun`` and point at a
    colocated manifest whose **first document** is a PipelineRun shell. ``--run-its`` loads
    that shell at ``git_url`` / ``git_revision`` and uses its ``pipelineRef.params.pathInRepo``.
    """
    text = (path_in_repo or "").strip().lstrip("/")
    if not text:
        return default
    if _RHOAI_E2E_ITS_MANIFEST_RE.match(text):
        return _pipeline_path_from_colocated_its_manifest(
            text,
            git_url=git_url,
            git_revision=git_revision,
            local_repo_root=local_repo_root,
            default=default,
        )
    if text.endswith("-pipeline.yaml"):
        return text
    match = _PIPELINERUN_WRAPPER_RE.match(text)
    if match:
        return f"{match.group('dir')}{match.group('stem')}-pipeline.yaml"
    return text
