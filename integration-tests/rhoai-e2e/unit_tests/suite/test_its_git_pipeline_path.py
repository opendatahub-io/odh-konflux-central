"""Tests for ITS resolver pathInRepo → CLI pipelineRef mapping."""

from __future__ import annotations

import unittest
from pathlib import Path

from suite.its_git_pipeline_path import (
    DEFAULT_RHOAI_E2E_GIT_PIPELINE_PATH,
    resolve_git_pipeline_path_from_its_resolver,
)

_ROOT = Path(__file__).resolve().parents[2]
_REPO_ROOT = _ROOT.resolve().parent.parent


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

    def test_rhoai_e2e_its_manifest_reads_shell_pipeline_ref(self) -> None:
        path = (
            "integration-tests/rhoai-e2e/tekton/its/"
            "its-e2e-ephc-ocp422-aip-ray-mlf-ogx-ais-tr-sprk-cf-llam-dsw-wbi.yaml"
        )
        self.assertEqual(
            resolve_git_pipeline_path_from_its_resolver(
                path,
                local_repo_root=_REPO_ROOT,
            ),
            "integration-tests/rhoai-e2e/tekton/pipelines/rhoai-e2e-pipeline.yaml",
        )

    def test_its_manifest_named_like_pipeline_yaml_uses_shell_not_self(self) -> None:
        rel = "integration-tests/rhoai-e2e/tekton/its/its-e2e-custom-pipeline.yaml"
        manifest = _REPO_ROOT / rel
        manifest.parent.mkdir(parents=True, exist_ok=True)
        manifest.write_text(
            """---
apiVersion: tekton.dev/v1
kind: PipelineRun
spec:
  pipelineRef:
    params:
      - name: pathInRepo
        value: integration-tests/rhoai-e2e/tekton/pipelines/rhoai-e2e-pipeline.yaml
---
apiVersion: appstudio.redhat.com/v1beta2
kind: IntegrationTestScenario
metadata:
  name: e2e-custom-pipeline
spec:
  application: testops-playpen
""",
            encoding="utf-8",
        )
        try:
            resolved = resolve_git_pipeline_path_from_its_resolver(
                rel,
                local_repo_root=_REPO_ROOT,
            )
        finally:
            manifest.unlink(missing_ok=True)
        self.assertEqual(
            resolved,
            "integration-tests/rhoai-e2e/tekton/pipelines/rhoai-e2e-pipeline.yaml",
        )
        self.assertNotEqual(resolved, rel)

    def test_olminstall_rh_nightly_wrapper(self) -> None:
        self.assertEqual(
            resolve_git_pipeline_path_from_its_resolver(
                "integration-tests/olminstall/tekton/pipelines/olminstall-pipelinerun-rh-nightly.yaml"
            ),
            "integration-tests/olminstall/tekton/pipelines/olminstall-pipeline.yaml",
        )


if __name__ == "__main__":
    unittest.main()
