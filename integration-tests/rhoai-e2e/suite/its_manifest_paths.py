"""Repo paths for in-tree IntegrationTestScenario manifests (ITS + colocated PipelineRun shell)."""

from __future__ import annotations

_RHOAI_E2E_PREFIX = "integration-tests/rhoai-e2e/tekton/its/its-"
# Tekton PipelineRun metadata.name = generateName prefix + ~5 random chars (DNS label max 63).
_TEKTON_PLR_NAME_MAX_LEN = 63
_TEKTON_GENERATE_SUFFIX_LEN = 5
_MAX_GENERATE_PREFIX_LEN = _TEKTON_PLR_NAME_MAX_LEN - _TEKTON_GENERATE_SUFFIX_LEN


def integration_test_scenario_manifest_repo_path(its_name: str) -> str:
    """Git path to colocated ``its-<metadata.name>.yaml`` (PipelineRun doc 1, ITS doc 2)."""
    name = (its_name or "").strip()
    return f"{_RHOAI_E2E_PREFIX}{name}.yaml"


def integration_test_scenario_pipelinerun_wrapper_path(its_name: str) -> str:
    """``resolverRef.pathInRepo`` (same file; Integration Service decodes document 1)."""
    return integration_test_scenario_manifest_repo_path(its_name)


def integration_test_scenario_generate_prefix(its_name: str) -> str:
    """``generateName`` / ``PIPELINERUN_GENERATE_PREFIX``: ``e2e-its-<metadata.name>-`` (trimmed to fit DNS)."""
    name = (its_name or "").strip()
    body = f"e2e-its-{name}"
    while len(body) + 1 > _MAX_GENERATE_PREFIX_LEN and "-" in body:
        body = body.rsplit("-", 1)[0]
    return f"{body}-"
