"""Unit tests for emit_parse_artifacts Tekton budget trimming."""

from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from _bootstrap import ensure_rhoai_e2e_path

ensure_rhoai_e2e_path()

from steps.emit_parse_artifacts import main
from steps.tekton_util import (
    _TEKTON_STEP_TERMINATION_BUDGET_BYTES,
    _TEKTON_TASK_RESULTS_BUDGET_BYTES,
    tekton_results_termination_payload_size,
    tekton_step_termination_payload_size,
)
from suite.component_catalog import default_components_smoke_config_path, load_components_smoke_catalog


class EmitParseArtifactsTest(unittest.TestCase):
    def test_emit_trims_long_trigger_cmd_and_keeps_components_csv(self) -> None:
        catalog = load_components_smoke_catalog(default_components_smoke_config_path())
        components_csv = ",".join(catalog.component_ids)
        trigger_cmd = (
            "python3 integration-tests/rhoai-e2e/rhoai_e2e.py "
            "--external-kubeconfig /home/nmanos/.kube/ods-qe-psi-07 "
            "--cleanup true --product rhoai --rhoai-version 3.5 "
            "--image quay.io/rhoai/rhoai-fbc-fragment@sha256:a708c3f7a0ed3d901a5b75032cfb359334cc57066311603cf06e842e4848f9f4 "
            "--tests bvt,smoke --konflux-repo https://github.com/manosnoam/odh-konflux-central.git "
            "--konflux-branch fix/ephc-lease-resilience --konflux-namespace rhoai-tenant --konflux-app testops-playpen"
        )
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            run_config = tmp_path / "run-config"
            results_dir = tmp_path / "results"
            run_config.mkdir()
            results_dir.mkdir()
            (run_config / "COMPONENTS_CSV").write_text(components_csv, encoding="utf-8")
            (run_config / "SETUP_DEPENDENCIES_ARGS").write_text("-M", encoding="utf-8")
            (run_config / "SMOKE_AWS_SECRET").write_text("shiftleft-envfile-model-serving", encoding="utf-8")

            seed = {
                "TRIGGER": "CLI direct (manual trigger)",
                "KONFLUX_EVENT": "Incoming — CLI direct PipelineRun",
                "SNAPSHOT": "n/a",
                "FBC": "rhoai-fbc-fragment-ocp-421 @ sha256:a708c3f7a0ed…",
                "CLUSTER": "ods-qe-psi-07 (rhoai-e2e-kubeconfig-ods-qe-psi-07-nmanos)",
                "RUN": "product=rhoai, tests=bvt,smoke",
                "TRIGGER_CMD": trigger_cmd,
                "RUN_SMOKE": "true",
                "RUN_BVT": "true",
                "RUN_TIER1": "false",
                "RUN_OPENDATAHUB_TESTS": "true",
                "RUN_MINIMAL_DEPS": "true",
                "RUN_INSTALL_DEP_OPERATORS": "true",
                "RUN_COMPONENT_TESTS": "true",
                "RUN_COMPONENT_CLUSTER_PREP_IN_DEP_OPERATORS": "false",
                "RUN_BVT_PLACEHOLDER_ONLY": "false",
                "RUN_DISTRIBUTED_WORKLOADS_TESTS": "true",
            }
            for name, value in seed.items():
                (results_dir / name).write_text(value, encoding="utf-8")

            env = {
                "RUN_CONFIG_DIR": str(run_config),
                "TEKTON_RESULTS_DIR": str(results_dir),
                "COMPONENTS_CSV_PATH": str(results_dir / "COMPONENTS_CSV"),
                "SETUP_DEPENDENCIES_ARGS_PATH": str(results_dir / "SETUP_DEPENDENCIES_ARGS"),
                "SMOKE_AWS_SECRET_PATH": str(results_dir / "SMOKE_AWS_SECRET"),
            }
            with patch.dict(os.environ, env, clear=True):
                self.assertEqual(main(), 0)

            fitted = {
                path.name: path.read_text(encoding="utf-8")
                for path in results_dir.iterdir()
                if path.is_file()
            }
            self.assertEqual(fitted.get("COMPONENTS_CSV"), components_csv)
            self.assertLess(
                tekton_step_termination_payload_size(fitted),
                _TEKTON_STEP_TERMINATION_BUDGET_BYTES,
            )
            self.assertLess(
                tekton_results_termination_payload_size(fitted),
                _TEKTON_TASK_RESULTS_BUDGET_BYTES,
            )


if __name__ == "__main__":
    raise SystemExit(unittest.main())
