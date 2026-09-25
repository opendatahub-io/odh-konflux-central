"""Apply or remove IntegrationTestScenario objects on the Konflux cluster."""

from __future__ import annotations

import os
import shutil
import sys
import tempfile
from pathlib import Path

from k8s.oc_util import filter_warning_lines, run_cmd
from suite.errors import AppError
from suite.its_registry import (
    integration_test_scenario_application,
    its_manifest_param,
)
from suite.its_trigger_params import CLUSTER_SOURCE_EPHC, is_external_cluster_source, ocp_install_prefix
from suite.pipelinerun_naming import build_rhoai_e2e_generate_prefix


class RunnerItsAdminMixin:
    def enable_integration_test_scenario(self) -> int:
        manifest = Path(self.args.its_manifest_path)
        name = self.args.its_scenario_name
        self._apply_integration_test_scenario(name, manifest_path=manifest)
        return 0

    def run_integration_test_scenario(self) -> int:
        """One-shot debug run: direct PipelineRun with ITS manifest params and dynamic generateName."""
        manifest = Path(self.args.its_manifest_path)
        self.its_file = manifest
        snap_path = getattr(self.args, "run_its_snapshot_path", None)
        self._stage_its_manifest_tmp(manifest, push_context=False)
        if snap_path is not None:
            self.snapshot_file = snap_path
        self._apply_run_its_manifest_defaults(manifest)
        self._print_effective_trigger_context()
        odh_overrides = False
        self._apply_konflux_git_inference_from_clone_or_env()
        items_by_name = {
            item.get("metadata", {}).get("name", ""): item
            for item in self.get_pipelineruns(self.args.namespace)
            if item.get("metadata", {}).get("name")
        }
        owned_running = self.find_owned_live_watch_pr()
        self._prepare_external_cluster_before_trigger(
            owned_running=owned_running,
            items_by_name=items_by_name,
        )
        self.resolve_image(odh_overrides)
        self._apply_run_its_cli_overrides(odh_overrides)
        self._pipelinerun_generate_prefix = self._run_its_generate_prefix(manifest, odh_overrides)
        print(f"  pipelinerun_prefix={self._pipelinerun_generate_prefix!r}")
        self.create_direct_pipelinerun(odh_overrides)
        return self._run_post_trigger_watch()

    def disable_integration_test_scenario(self) -> int:
        name = self.args.its_scenario_name
        manifest = Path(self.args.its_manifest_path)
        self._remove_integration_test_scenario(name, manifest_path=manifest)
        return 0

    def _apply_run_its_manifest_defaults(self, manifest: Path) -> None:
        if not getattr(self.args, "product_explicit", False):
            product = its_manifest_param(manifest, "PRODUCT")
            if product:
                self.args.product = product
        if not getattr(self.args, "tests_explicit", False):
            tests = its_manifest_param(manifest, "TEST_GATES")
            if tests:
                self.args.tests = tests
        fbc_name = its_manifest_param(manifest, "RHOAI_FBC_NAME")
        if fbc_name:
            self.resolved_rhoai_fbc_name = fbc_name
        cluster_source = its_manifest_param(manifest, "CLUSTER_SOURCE")
        if is_external_cluster_source(cluster_source) and not self._external_kubeconfig_its_override():
            self.external_kubeconfig_secret = cluster_source
        if not (getattr(self.args, "ocp_version", "") or "").strip():
            ocp_version = its_manifest_param(manifest, "OCP_VERSION")
            prefix = ocp_install_prefix(ocp_version)
            if prefix:
                self.args.ocp_version = prefix
        if not (
            getattr(self.args, "components_explicit", False)
            or getattr(self.args, "components_inferred", False)
        ):
            components = its_manifest_param(manifest, "COMPONENTS")
            if components:
                self.args.components = components
        if not (getattr(self.args, "channel", "") or "").strip():
            update_channel = its_manifest_param(manifest, "UPDATE_CHANNEL")
            if update_channel:
                self.args.channel = update_channel
        # Konflux lookup in resolve_image(); snapshot pin is offline fallback only.
        self._run_its_pinned_fbcf_image = self._snapshot_yaml_container_image()

    def _apply_run_its_cli_overrides(self, odh_overrides: bool) -> None:
        """Patch staged ITS tmp with CLI cluster/test/install overrides (--run-its only)."""
        if self.its_apply_tmp:
            self._clear_registry_params_from_staged_its(self.its_apply_tmp)
            self._patch_its_cli_override_params(self.its_apply_tmp, odh_overrides)

    def _run_its_generate_prefix(self, manifest: Path, odh_overrides: bool) -> str:
        cluster_source = self._cluster_source_for_its()
        if not cluster_source:
            cluster_source = its_manifest_param(manifest, "CLUSTER_SOURCE")
        if is_external_cluster_source(cluster_source):
            target_type = "external"
            cluster_label = (
                self._cluster_label_for_external_secret(cluster_source) if cluster_source else ""
            )
        elif cluster_source == CLUSTER_SOURCE_EPHC:
            target_type, cluster_label = "ephc", ""
        else:
            target_type, cluster_label = "stub", ""
        if getattr(self.args, "components_explicit", False) or getattr(
            self.args, "components_inferred", False
        ):
            components_csv = getattr(self.args, "components", "") or ""
        else:
            components_csv = its_manifest_param(manifest, "COMPONENTS") or (
                getattr(self.args, "components", "") or ""
            )
        rhoai_version_param = ""
        if self.its_apply_tmp:
            rhoai_version_param = self._read_its_params_from_tmp().get("RHOAI_VERSION", "")
        if not rhoai_version_param:
            rhoai_version_param = its_manifest_param(manifest, "RHOAI_VERSION") or ""
        return build_rhoai_e2e_generate_prefix(
            product=its_manifest_param(manifest, "PRODUCT") or self.args.product,
            version=self._catalog_version_for_naming(
                odh_overrides,
                rhoai_version_param=rhoai_version_param,
            ),
            cluster_source=cluster_source,
            cluster_label=cluster_label,
            target_type=target_type,
            tests_csv=self.args.tests,
            components_csv=components_csv,
            run_owner=self.run_owner,
        )

    def _stage_its_manifest_tmp(self, manifest: Path, *, push_context: bool) -> None:
        tmp = tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False)
        tmp_path = Path(tmp.name)
        tmp.close()
        shutil.copyfile(manifest, tmp_path)
        if push_context:
            run_cmd(
                [
                    "yq",
                    "e",
                    '.spec.contexts = [{"name": "push", "description": "Manual Snapshot (--run-its)"}]',
                    "-i",
                    str(tmp_path),
                ],
                capture=True,
                check=True,
            )
        self._yq_patch_its_konflux_git(
            tmp_path,
            konflux_repo=(getattr(self.args, "konflux_repo", "") or "").strip(),
            konflux_branch=(getattr(self.args, "konflux_branch", "") or "").strip(),
        )
        self.its_apply_tmp = str(tmp_path)

    def _resolve_its_apply_application(self, manifest_app: str) -> tuple[str, str]:
        """Return (apply application label, patch value when staging is required)."""
        manifest = (manifest_app or "").strip()
        cli_app = (self.args.app or "").strip()
        explicit = bool(getattr(self.args, "konflux_app_explicit", False))
        if explicit:
            apply_app = cli_app or manifest
        else:
            apply_app = manifest or cli_app
            if not apply_app:
                raise AppError(
                    "ITS manifest missing spec.application and --konflux-app is unset.",
                    2,
                )
        patch = apply_app if explicit and manifest and apply_app != manifest else ""
        if patch:
            print(
                f"WARN --konflux-app {apply_app!r}: patching ITS spec.application "
                f"(manifest default is {manifest!r}).",
                file=sys.stderr,
            )
        return apply_app, patch

    def _apply_integration_test_scenario(
        self,
        name: str,
        *,
        manifest_path: Path | None = None,
        param_overrides: dict[str, str] | None = None,
    ) -> None:
        manifest = manifest_path or Path(self.args.its_manifest_path)
        apply_app, patch_application = self._resolve_its_apply_application(
            integration_test_scenario_application(manifest)
        )
        konflux_repo = (getattr(self.args, "konflux_repo", "") or "").strip()
        konflux_branch = (getattr(self.args, "konflux_branch", "") or "").strip()
        apply_path = manifest
        tmp_path: Path | None = None
        needs_staging = bool(konflux_repo or konflux_branch or param_overrides or patch_application)
        if needs_staging:
            tmp = tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False)
            tmp_path = Path(tmp.name)
            tmp.close()
            shutil.copyfile(manifest, tmp_path)
            if patch_application:
                self._yq_set_its_application(tmp_path, patch_application)
            self._yq_patch_its_konflux_git(
                tmp_path,
                konflux_repo=konflux_repo,
                konflux_branch=konflux_branch,
            )
            if param_overrides:
                for param_name, param_value in param_overrides.items():
                    self._yq_upsert_its_param(tmp_path, param_name, param_value)
            apply_path = tmp_path
        print(
            f"Applying IntegrationTestScenario {name!r} to namespace {self.args.namespace!r} "
            f"(application {apply_app!r})..."
        )
        try:
            proc = run_cmd(
                ["oc", "apply", "-n", self.args.namespace, "-f", str(apply_path)],
                capture=True,
                check=False,
            )
        finally:
            if tmp_path is not None:
                tmp_path.unlink(missing_ok=True)
        filtered = filter_warning_lines(f"{proc.stdout}\n{proc.stderr}")
        if filtered.strip():
            print(filtered, file=sys.stderr)
        if proc.returncode != 0:
            raise AppError(f"oc apply failed for IntegrationTestScenario {name!r}.", 1)
        print(f"IntegrationTestScenario {name!r} enabled.")

    def _remove_integration_test_scenario(
        self, name: str, *, manifest_path: Path | None = None
    ) -> None:
        manifest = manifest_path or Path(self.args.its_manifest_path)
        apply_app, _ = self._resolve_its_apply_application(
            integration_test_scenario_application(manifest)
        )
        print(
            f"Deleting IntegrationTestScenario {name!r} from namespace {self.args.namespace!r} "
            f"(application {apply_app!r})..."
        )
        proc = run_cmd(
            [
                "oc",
                "delete",
                "integrationtestscenario",
                name,
                "-n",
                self.args.namespace,
                "--ignore-not-found",
            ],
            capture=True,
            check=False,
        )
        filtered = filter_warning_lines(f"{proc.stdout}\n{proc.stderr}")
        if filtered.strip():
            print(filtered)
        if proc.returncode != 0:
            raise AppError(f"oc delete failed for IntegrationTestScenario {name!r}.", 1)
        print(f"IntegrationTestScenario {name!r} disabled (removed from cluster).")

    @staticmethod
    def _yq_set_its_application(path: Path, application: str) -> None:
        run_cmd(
            [
                "yq",
                "e",
                ".spec.application = strenv(YQ_ITS_APPLICATION)",
                "-i",
                str(path),
            ],
            capture=True,
            check=True,
            env={**os.environ, "YQ_ITS_APPLICATION": application},
        )

