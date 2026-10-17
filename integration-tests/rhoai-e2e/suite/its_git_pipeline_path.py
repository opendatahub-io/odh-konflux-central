"""Map IntegrationTestScenario ``resolverRef.pathInRepo`` to Tekton git ``pipelineRef`` paths."""

from __future__ import annotations

import re

# Default when the ITS omits pathInRepo (current rhoai-e2e tree).
DEFAULT_RHOAI_E2E_GIT_PIPELINE_PATH = (
    "integration-tests/rhoai-e2e/tekton/pipelines/rhoai-e2e-pipeline.yaml"
)

# ITS wrappers: ``*-pipelinerun[-suffix].yaml`` → sibling ``*-pipeline.yaml`` (main olminstall + rhoai-e2e).
_PIPELINERUN_WRAPPER_RE = re.compile(
    r"^(?P<dir>.*/)(?P<stem>[\w-]+)-pipelinerun(?:-[a-z0-9][a-z0-9-]*)?\.yaml$",
    re.IGNORECASE,
)


def resolve_git_pipeline_path_from_its_resolver(
    path_in_repo: str,
    *,
    default: str = DEFAULT_RHOAI_E2E_GIT_PIPELINE_PATH,
) -> str:
    """
    Return the git path Tekton should load for ``pipelineRef`` on CLI direct triggers.

    Integration Service ITS objects often set ``resourceKind: pipelinerun`` and point at a
    thin wrapper YAML; Konflux still resolves the monolithic ``*-pipeline.yaml`` inside that
    wrapper. ``--run-its`` must use the same pipeline path for the chosen ``revision``.
    """
    text = (path_in_repo or "").strip().lstrip("/")
    if not text:
        return default
    if text.endswith("-pipeline.yaml"):
        return text
    match = _PIPELINERUN_WRAPPER_RE.match(text)
    if match:
        return f"{match.group('dir')}{match.group('stem')}-pipeline.yaml"
    return text
