"""Tests for ITS resolver pathInRepo → CLI pipelineRef mapping."""

from __future__ import annotations

import unittest

from suite.its_git_pipeline_path import (
    DEFAULT_RHOAI_E2E_GIT_PIPELINE_PATH,
    resolve_git_pipeline_path_from_its_resolver,
)


class ItsGitPipelinePathTests(unittest.TestCase):
    def test_default_when_empty(self) -> None:
        self.assertEqual(
            resolve_git_pipeline_path_from_its_resolver(""),
            DEFAULT_RHOAI_E2E_GIT_PIPELINE_PATH,
        )

    def test_pipeline_path_unchanged(self) -> None:
        path = "integration-tests/rhoai-e2e/tekton/pipelines/rhoai-e2e-pipeline.yaml"
        self.assertEqual(resolve_git_pipeline_path_from_its_resolver(path), path)

    def test_main_olminstall_ephc_wrapper(self) -> None:
        self.assertEqual(
            resolve_git_pipeline_path_from_its_resolver(
                "integration-tests/olminstall/tekton/pipelines/olminstall-pipelinerun-ephc.yaml"
            ),
            "integration-tests/olminstall/tekton/pipelines/olminstall-pipeline.yaml",
        )

    def test_rhoai_e2e_ephc_wrapper(self) -> None:
        self.assertEqual(
            resolve_git_pipeline_path_from_its_resolver(
                "integration-tests/rhoai-e2e/tekton/pipelines/rhoai-e2e-pipelinerun-ephc.yaml"
            ),
            "integration-tests/rhoai-e2e/tekton/pipelines/rhoai-e2e-pipeline.yaml",
        )

    def test_olminstall_rh_nightly_wrapper(self) -> None:
        self.assertEqual(
            resolve_git_pipeline_path_from_its_resolver(
                "integration-tests/olminstall/tekton/pipelines/olminstall-pipelinerun-rh-nightly.yaml"
            ),
            "integration-tests/olminstall/tekton/pipelines/olminstall-pipeline.yaml",
        )


if __name__ == "__main__":
    unittest.main()
