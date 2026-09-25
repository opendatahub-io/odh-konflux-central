"""Konflux rhoai-e2e CLI orchestration (watch, list, trigger snapshot)."""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from pathlib import Path
from typing import Any
from urllib.parse import quote

from suite.constants import (
    ANNOTATION_CLUSTER,
    ANNOTATION_OPERATOR_VERSION,
    ANNOTATION_PRODUCT,
    ANNOTATION_RUN_OWNER,
    ANNOTATION_TEST_RESULTS_URL,
    ANNOTATION_TESTS,
    DEFAULT_UPSTREAM_KONFLUX_GIT,
    ITS_TEST_GATES_PARAM_DEFAULT,
    RHOAI_E2E_ANNOTATION_LABELS,
    RHOAI_E2E_CTX_PRINT_KEYS,
    RHOAI_E2E_WRITE_ANNOTATION_KEYS,
    LABEL_TRIGGER_EVENT_TYPE,
    EVENT_TYPE_INCOMING,
    TRIGGER_TYPE_MANUAL,
    TRIGGER_TYPE_RH_NIGHTLY_AUTO,
)
from suite.component_catalog import (
    ComponentsSmokeCatalog,
    default_components_smoke_config_path,
    load_components_smoke_catalog,
    resolve_shift_left_env_secret,
)
from suite.component_plan import parse_components_selection
from suite.its_trigger_params import CLUSTER_SOURCE_EPHC, is_external_cluster_source
from suite.pipelinerun_naming import default_pipelinerun_generate_prefix
from suite.errors import AppError
from k8s.external_kubeconfig import (
    cluster_label_from_tenant_secret,
    cluster_lock_key_from_tenant_secret,
    delete_external_kubeconfig_secret,
    ensure_external_kubeconfig_secret,
    verify_external_cluster_secret,
    validate_kubeconfig_path,
)
from install.kubeconfig_cluster_label import cluster_label_from_kubeconfig, cluster_lock_key_from_kubeconfig
from k8s.kubearchive import KubeArchiveAuthError, KubeArchiveClient
from k8s.oc_util import (
    derive_konflux_ui_base,
    derive_kubearchive_host,
    get_jsonpath,
    run_cmd,
)
from runners.report.pipelinerun_config_display import (
    format_pipelinerun_config_lines,
    pipelinerun_outcome_line,
    pipelinerun_timing_lines,
)
from k8s.smoke_aws_credentials import (
    backfill_shift_left_smoke_secret_from_mlflow,
    ensure_router_ca_in_smoke_secret,
)
from .runner_mixin_cleanup import RunnerCleanupMixin
from .runner_mixin_delete import RunnerDeleteMixin
from .runner_mixin_its import RunnerItsAdminMixin
from .runner_mixin_list import RunnerListMixin
from .runner_mixin_trigger import RunnerTriggerMixin
from .runner_mixin_watch import RunnerWatchMixin
from .runner_support import (
    PipelineRow,
    Tee,
    first_snapshot_component_name,
    format_rhoai_e2e_watch_cli,
    spin_while,
)

__all__ = [
    "RhoaiE2ERunner",
    "PipelineRow",
    "Tee",
    "first_snapshot_component_name",
    "format_rhoai_e2e_watch_cli",
    "spin_while",
]


class RhoaiE2ERunner(
    RunnerListMixin,
    RunnerDeleteMixin,
    RunnerCleanupMixin,
    RunnerTriggerMixin,
    RunnerWatchMixin,
    RunnerItsAdminMixin,
):
    def __init__(self, args: argparse.Namespace) -> None:
        self.args = args
        self.script_dir = Path(__file__).resolve().parent.parent.parent
        self.snapshot_file = self.script_dir / "config" / "test-snapshot.yaml"
        self.its_file = self.script_dir / "tekton" / "its" / "its-rhoai-e2e-ephc-ocp421.yaml"
        self.konflux_ui = args.konflux_ui or ""
        self.ka_host = args.ka_host or ""
        self.konflux_server = args.konflux_server or ""
        raw_to = os.environ.get("PR_APPEAR_TIMEOUT_SECONDS", "600")
        try:
            self.pr_appear_timeout = int(raw_to)
        except ValueError:
            print(f"WARN Invalid PR_APPEAR_TIMEOUT_SECONDS={raw_to!r}; using 600", file=sys.stderr)
            self.pr_appear_timeout = 600
        raw_sw = os.environ.get("OLMINSTALL_PIPELINE_START_WAIT_SECONDS", "1200")
        try:
            self.pipeline_start_wait_seconds = max(60, int(raw_sw))
        except ValueError:
            print(
                f"WARN Invalid OLMINSTALL_PIPELINE_START_WAIT_SECONDS={raw_sw!r}; using 1200",
                file=sys.stderr,
            )
            self.pipeline_start_wait_seconds = 1200
        self.cleanup_snapshot_on_exit = True
        self._user_detached_from_logs = False
        self.snapshot_name = ""
        self.its_apply_tmp = ""
        self.log_file = ""
        self.pr = ""
        self.watch_completed = False
        self.watch_from_archive = False
        self._kubearchive_log_replay = False
        self.ka_succeeded = "Unknown"
        self.pipeline_exit = 0
        self.run_owner = ""
        self.token = ""
        self.ka: KubeArchiveClient | None = None
        self.konflux_host_api = ""
        self.resolved_app = ""
        self.resolved_rhoai_fbc_name = ""
        self.resolved_ocp_minor = ""
        self.image = args.image or ""
        self.update_channel_override = args.channel or ""
        self._fbc_source_snapshot_meta: dict[str, Any] | None = None
        # Filled after ``create_direct_pipelinerun`` — used to match PipelineRun ``SNAPSHOT`` JSON params to this trigger.
        self._trigger_snapshot_spec: dict[str, Any] | None = None
        self._trigger_snapshot_created_ts = ""
        self.trigger_argv: list[str] = list(getattr(args, "trigger_argv", None) or [])
        self.external_kubeconfig_secret = ""
        self._external_secret_created_by_cli = False
        self.smoke_aws_secret = ""
        self.cleanup_external_secret_on_exit = True
        self._pipelinerun_generate_prefix = default_pipelinerun_generate_prefix()
        self._cli_direct_pipelinerun = False


    def _tests_its_override(self) -> bool:
        """True when CLI should inject TEST_GATES into the ITS (matches annotation logic)."""
        return getattr(self.args, "tests_explicit", False) or self.args.tests != getattr(
            self.args, "tests_catalog_default_csv", ITS_TEST_GATES_PARAM_DEFAULT
        )


    def _components_its_override(self) -> bool:
        return getattr(self.args, "components_explicit", False) or getattr(
            self.args, "components_inferred", False
        )


    def _test_timeout_its_override(self) -> bool:
        return bool((getattr(self.args, "test_timeout", "") or "").strip())

    def _test_tags_its_override(self) -> bool:
        return bool((getattr(self.args, "test_tags", "") or "").strip())

    def _tests_version_its_override(self) -> bool:
        return bool((getattr(self.args, "tests_rhoai_version", "") or "").strip())


    def _cleanup_its_override(self) -> bool:
        return bool(getattr(self.args, "cleanup", False))


    def _trigger_target_type(self) -> str:
        if self._external_kubeconfig_its_override() or (self.external_kubeconfig_secret or "").strip():
            return "external"
        if self.args.product == "rhoai":
            return "ephc"
        return "stub"


    def _trigger_external_cluster_target(self) -> tuple[Path | None, str]:
        ext_path = getattr(self.args, "external_kubeconfig_path", None)
        if ext_path is not None:
            return validate_kubeconfig_path(str(ext_path)), ""
        secret = (
            (getattr(self.args, "external_kubeconfig_secret", "") or "").strip()
            or (self.external_kubeconfig_secret or "").strip()
        )
        return None, secret

    def _trigger_cluster_label(self) -> str:
        path, secret = self._trigger_external_cluster_target()
        if path is not None:
            return cluster_label_from_kubeconfig(path)
        if secret:
            return self._cluster_label_for_external_secret(secret)
        return ""

    def _cluster_label_for_naming(self, cluster_source: str) -> str:
        """Resolve cluster label for PLR ``generateName`` using the same CLUSTER_SOURCE as the ITS patch."""
        source = (cluster_source or "").strip()
        if is_external_cluster_source(source):
            return self._cluster_label_for_external_secret(source)
        return self._trigger_cluster_label()

    def _target_type_for_naming(self, cluster_source: str) -> str:
        source = (cluster_source or "").strip()
        if is_external_cluster_source(source):
            return "external"
        if source == CLUSTER_SOURCE_EPHC:
            return "ephc"
        return self._trigger_target_type()

    def _trigger_cluster_lock_key(self) -> str:
        path, secret = self._trigger_external_cluster_target()
        if path is not None:
            return cluster_lock_key_from_kubeconfig(path)
        if secret:
            return cluster_lock_key_from_tenant_secret(
                namespace=self.args.namespace,
                secret_name=secret,
            )
        return ""


    def _trigger_scripts_git_source(self) -> tuple[str, str]:
        url = (getattr(self.args, "konflux_repo", "") or "").strip()
        rev = (getattr(self.args, "konflux_branch", "") or "").strip()
        if not url:
            url = DEFAULT_UPSTREAM_KONFLUX_GIT
        if not rev:
            rev = "main"
        return url, rev


    def build_rhoai_e2e_context_annotations(self) -> dict[str, str]:
        """Trigger-time annotations (minimal; params hold the rest)."""
        from runners.report.pipelinerun_metadata import build_cli_trigger_metadata

        tests = self.args.tests if self._tests_its_override() else ""
        git_url, git_rev = self._trigger_scripts_git_source()
        return build_cli_trigger_metadata(
            script_dir=self.script_dir,
            trigger_argv=self.trigger_argv,
            product=self.args.product,
            tests=tests,
            cluster=self._trigger_cluster_label(),
            cluster_key=self._trigger_cluster_lock_key(),
            fbcf_image=self._trigger_fbcf_image(),
            ocp_version=(getattr(self.args, "ocp_version", "") or "").strip(),
            scripts_git_url=git_url,
            scripts_git_revision=git_rev,
            upstream_git_url=DEFAULT_UPSTREAM_KONFLUX_GIT,
            fbc_snapshot_meta=self._fbc_source_snapshot_meta,
            local_git_repo=self.script_dir.parent.parent,
            trigger_type=TRIGGER_TYPE_MANUAL,
        )


    def _trigger_fbcf_image(self) -> str:
        if self.image:
            return self.image.strip()
        from runners.report.pipelinerun_metadata import fbcf_image_from_snapshot_spec

        return fbcf_image_from_snapshot_spec(self._trigger_snapshot_spec)


    def build_rhoai_e2e_trigger_labels(self) -> dict[str, str]:
        from runners.report.pipelinerun_metadata import (
            build_cli_trigger_labels,
            build_konflux_test_pipelinerun_type_labels,
            build_trigger_labels,
        )

        git_url, git_rev = self._trigger_scripts_git_source()
        labels = build_konflux_test_pipelinerun_type_labels()
        labels.update(
            build_trigger_labels(
                run_owner=self.run_owner,
                product=self.args.product,
                target_type=self._trigger_target_type(),
                cluster=self._trigger_cluster_label(),
            )
        )
        labels.update(
            build_cli_trigger_labels(
                fbcf_image=self._trigger_fbcf_image(),
                scripts_git_url=git_url,
                scripts_git_revision=git_rev,
                upstream_git_url=DEFAULT_UPSTREAM_KONFLUX_GIT,
                fbc_snapshot_meta=self._fbc_source_snapshot_meta,
                local_git_repo=self.script_dir.parent.parent,
            )
        )
        if self._cli_direct_pipelinerun and not getattr(self.args, "auto_rh_nightly", False):
            labels[LABEL_TRIGGER_EVENT_TYPE] = EVENT_TYPE_INCOMING
        return labels


    def early_summary_annotate_argv(self) -> list[str]:
        """Do not pre-seed artifact browser URLs — they 404 until OCI upload succeeds."""
        return []


    def rhoai_e2e_context_annotate_argv(self) -> list[str]:
        ctx = self.build_rhoai_e2e_context_annotations()
        return [f"{k}={ctx[k]}" for k in RHOAI_E2E_WRITE_ANNOTATION_KEYS if k in ctx]


    def get_pipelinerun_json_for_display(self) -> dict[str, Any]:
        if self.watch_from_archive or self._kubearchive_log_replay:
            assert self.ka is not None
            path = f"/apis/tekton.dev/v1/namespaces/{quote(self.args.namespace)}/pipelineruns/{quote(self.pr)}"
            try:
                prj = self.ka.get_json(path)
            except KubeArchiveAuthError as exc:
                print(f"WARN KubeArchive auth failed for display JSON: {exc}", file=sys.stderr)
                return {}
            return prj if isinstance(prj, dict) else {}
        proc = run_cmd(
            ["oc", "get", "pipelinerun", self.pr, "-n", self.args.namespace, "-o", "json"],
            capture=True,
            check=False,
        )
        if proc.returncode != 0:
            return {}
        try:
            return json.loads(proc.stdout or "{}")
        except json.JSONDecodeError:
            return {}


    def _trigger_context_lines(self, prj: dict[str, Any]) -> list[str]:
        ann = prj.get("metadata", {}).get("annotations") or {}
        lines: list[str] = []
        for key in RHOAI_E2E_CTX_PRINT_KEYS:
            val = ann.get(key)
            if val:
                label = RHOAI_E2E_ANNOTATION_LABELS.get(key, key)
                lines.append(f"  {label}: {val}")
        return lines


    def _cluster_label_for_external_secret(self, secret_name: str) -> str:
        """Best-effort context/cluster name from an external kubeconfig Secret (watch summary)."""
        return cluster_label_from_tenant_secret(
            namespace=self.args.namespace,
            secret_name=secret_name,
        )


    def _pipelinerun_param_value(self, prj: dict[str, Any], name: str, default: str = "") -> str:
        for p in prj.get("spec", {}).get("params", []) or []:
            if p.get("name") != name:
                continue
            val = p.get("value")
            if isinstance(val, str) and val.strip():
                return val.strip()
        return default


    def read_pipeline_install_results(self, prj: dict[str, Any]) -> list[tuple[str, str]]:
        """Tekton ``status.pipelineResults`` (install/catalog summary), when the API exposes them."""
        status = prj.get("status") or {}
        raw = status.get("pipelineResults")
        if not isinstance(raw, list):
            return []
        out: list[tuple[str, str]] = []
        for item in raw:
            if not isinstance(item, dict):
                continue
            name = item.get("name")
            val = item.get("value")
            if name is None:
                continue
            s = (val if isinstance(val, str) else str(val)).strip()
            if s:
                out.append((str(name), s))
        return out


    def read_taskruns_for_pr(self) -> list[dict[str, Any]]:
        """TaskRun objects for the current PipelineRun (live cluster); empty if unavailable."""
        if self.watch_from_archive or not (self.pr or "").strip():
            return []
        proc = run_cmd(
            [
                "oc",
                "get",
                "taskrun",
                "-n",
                self.args.namespace,
                "-l",
                f"tekton.dev/pipelineRun={self.pr}",
                "-o",
                "json",
            ],
            capture=True,
            check=False,
            timeout=90,
        )
        if proc.returncode != 0 or not (proc.stdout or "").strip():
            return []
        try:
            data = json.loads(proc.stdout)
        except json.JSONDecodeError:
            return []
        items = data.get("items")
        if not isinstance(items, list):
            return []
        return [x for x in items if isinstance(x, dict)]


    def test_results_url(self, prj: dict[str, Any] | None = None) -> str:
        """Published or predicted artifact browser URL(s) for phases in TESTS."""
        from runners.report.test_artifacts import resolve_artifacts_notification_lines

        data = prj if prj is not None else self.get_pipelinerun_json_for_display()
        ann = (data.get("metadata") or {}).get("annotations") or {}
        url_ann = (ann.get(ANNOTATION_TEST_RESULTS_URL) or "").strip()
        if url_ann:
            return url_ann
        tests = self._pipelinerun_param_value(
            data, "TEST_GATES", self._pipelinerun_param_value(data, "TESTS", "")
        )
        lines = resolve_artifacts_notification_lines(
            tests_csv=tests,
            pipeline_run=(self.pr or "").strip(),
            taskruns=self.read_taskruns_for_pr(),
        )
        if not lines:
            return "(no BVT/smoke in TEST_GATES)"
        return "; ".join(lines)


    def read_provision_cluster_cti_name(self) -> str:
        """Best-effort CTI / HyperShift object name from the stage-ephemeral-kubeconfig TaskRun (live cluster only)."""
        prj = self.get_pipelinerun_json_for_display()
        ann = (prj.get("metadata") or {}).get("annotations") or {}
        cti_ann = (ann.get(ANNOTATION_CLUSTER) or "").strip()
        if cti_ann:
            return cti_ann
        if self.watch_from_archive or not (self.pr or "").strip():
            return ""
        proc = run_cmd(
            [
                "oc",
                "get",
                "taskrun",
                "-n",
                self.args.namespace,
                "-l",
                f"tekton.dev/pipelineRun={self.pr}",
                "-o",
                "json",
            ],
            capture=True,
            check=False,
            timeout=90,
        )
        if proc.returncode != 0 or not (proc.stdout or "").strip():
            return ""
        try:
            data = json.loads(proc.stdout)
        except json.JSONDecodeError:
            return ""
        for item in data.get("items", []):
            labels = (item.get("metadata") or {}).get("labels") or {}
            task = labels.get("tekton.dev/pipelineTask", "")
            if task not in (
                "stage-ephemeral-kubeconfig",
                "install-ocp-cluster",
                "provision-cluster",
                "external-cluster-ready",
            ):
                continue
            for r in (item.get("status") or {}).get("results", []) or []:
                if r.get("name") == "clusterName" and isinstance(r.get("value"), str):
                    val = r["value"].strip()
                    if val:
                        return val
        return ""

    @staticmethod

    def _status_label_from_succeeded_condition(cstat: str, reason: str) -> str:
        if cstat == "True":
            return "Succeeded"
        if cstat == "False":
            return reason or "Failed"
        return reason or "Unknown"


    def _status_label_for_summary_preview(self) -> str:
        if self.watch_from_archive:
            return self.ka_succeeded or "Unknown"
        cstat, reason, _ = self.succeeded_condition_detail(self.pr)
        if cstat in ("True", "False"):
            return self._status_label_from_succeeded_condition(cstat, reason)
        if self.watch_completed:
            return reason or "Completed"
        return reason or "Running"


    def _terminal_status_label(self) -> str:
        if self.watch_from_archive:
            return self.ka_succeeded or "Unknown"
        cstat, reason, _ = self.succeeded_condition_detail(self.pr)
        return self._status_label_from_succeeded_condition(cstat, reason)

    @staticmethod

    def _ka_succeeded_from_prj(prj: dict[str, Any]) -> str:
        cond = next((c for c in prj.get("status", {}).get("conditions", []) if c.get("type") == "Succeeded"), {})
        if cond.get("status") == "True":
            return "Succeeded"
        if cond.get("status") == "False":
            return (cond.get("reason") or "").strip() or "Failed"
        return "Unknown"


    def print_run_summary(self, final_status: str, *, phase: str = "final") -> None:
        """Run identity, reattach command, links, trigger context, install results.

        ``phase='preview'`` is printed before log replay/stream; ``phase='final'`` after.
        """
        prj = self.get_pipelinerun_json_for_display()
        ann = (prj.get("metadata") or {}).get("annotations") or {}
        op_ver = (ann.get(ANNOTATION_OPERATOR_VERSION) or "").strip()
        if phase == "final" and not op_ver and self.log_file and Path(self.log_file).exists():
            txt = Path(self.log_file).read_text(encoding="utf-8", errors="ignore")
            m = re.findall(r"Operator version\s*:\s*([^\s]+)", txt)
            op_ver = m[-1] if m else ""
        watch_cmd = format_rhoai_e2e_watch_cli(
            operator_install_dir=self.script_dir,
            namespace=self.args.namespace,
            app=self.args.app,
            pipelinerun=self.pr,
        )
        title = " Olminstall run summary"
        if phase == "preview":
            title += " (before logs)"
        elif phase == "final":
            title += " (after logs)"
        print("\n===========================================================")
        print(title)
        print("===========================================================")
        print(f"  PipelineRun  : {self.pr}  [{final_status or 'unknown'}]")
        for ln in pipelinerun_timing_lines(prj):
            print(ln)
        outcome = pipelinerun_outcome_line(prj)
        if outcome:
            print(outcome)
        if op_ver:
            print(f"  Operator     : {op_ver}")
        print(f"  Watch logs   : {watch_cmd}")
        if self.watch_from_archive:
            print("  Source       : KubeArchive (pruned from live cluster)")
        if phase == "preview":
            if self.watch_from_archive:
                print("  Next         : Replaying logs from KubeArchive below")
            elif self.watch_completed:
                print("  Next         : Showing pipeline logs below (KubeArchive if live logs are pruned)")
            else:
                print("  Next         : Streaming pipeline logs below")
        print("")
        print("Related links:")
        ui = self._konflux_pipelinerun_url(self.args.app, self.pr)
        print(f"  Konflux UI   : {ui or '(unknown)'}")
        print(f"  Test Results : {self.test_results_url(prj)}")
        if (self.konflux_host_api or "").strip():
            print(f"  Konflux API  : {self.konflux_host_api}")
        cti = self.read_provision_cluster_cti_name()
        if cti and not is_external_cluster_source(self._pipelinerun_param_value(prj, "CLUSTER_SOURCE") or ""):
            print(f"  Target cluster: {cti}")
        print("")
        cluster_hint = (ann.get(ANNOTATION_CLUSTER) or "").strip()
        cfg_lines = format_pipelinerun_config_lines(
            prj,
            cluster_label_hint=cluster_hint,
            resolve_external_cluster=self._cluster_label_for_external_secret,
        )
        print("Run configuration (PipelineRun params):")
        for ln in cfg_lines:
            print(ln)
        ctx = self._trigger_context_lines(prj)
        print("")
        if ctx:
            print("Trigger context (PipelineRun annotations):")
            for ln in ctx:
                print(ln)
        else:
            print("Trigger context: (no rhoai-e2e.* annotations on this PipelineRun)")
        pairs = self.read_pipeline_install_results(prj)
        if pairs:
            print("")
            print("Install results:")
            for k, v in pairs:
                print(f"  - {k}: {v}")
        print("===========================================================")


    def mark_detached_from_logs(self) -> None:
        """User stopped local log streaming (Ctrl-C); do not delete the trigger Snapshot on exit."""
        self._user_detached_from_logs = True
        self.cleanup_snapshot_on_exit = False
        self.cleanup_external_secret_on_exit = False


    def _resolve_external_kubeconfig_secret(self) -> str:
        if self.external_kubeconfig_secret:
            return self.external_kubeconfig_secret
        path = getattr(self.args, "external_kubeconfig_path", None)
        secret = (getattr(self.args, "external_kubeconfig_secret", "") or "").strip()
        if path is None and not secret:
            return ""
        if path is not None:
            self.external_kubeconfig_secret = ensure_external_kubeconfig_secret(
                namespace=self.args.namespace,
                kubeconfig_path=path,
                secret_name="",
                run_owner=self.run_owner,
                preferred_context=(getattr(self.args, "external_kubeconfig_context", "") or "").strip(),
            )
            self._external_secret_created_by_cli = True
            return self.external_kubeconfig_secret
        who = verify_external_cluster_secret(
            namespace=self.args.namespace,
            secret_name=secret,
        )
        print(f"External kubeconfig Secret login OK as {who}")
        self.external_kubeconfig_secret = secret
        return secret


    def _components_catalog(self) -> ComponentsSmokeCatalog:
        cat = getattr(self.args, "components_catalog", None)
        if cat is not None:
            return cat
        return load_components_smoke_catalog(default_components_smoke_config_path())


    def _smoke_in_tests(self) -> bool:
        tests = (getattr(self.args, "tests", "") or "").strip()
        return "smoke" in {p.strip().lower() for p in tests.split(",") if p.strip()}


    def _selected_component_ids(self) -> frozenset[str]:
        cat = self._components_catalog()
        csv = (getattr(self.args, "components", "") or "").strip()
        if not csv:
            return frozenset(cat.component_ids)
        return parse_components_selection(csv, cat)


    def _config_shift_left_env_secret(self) -> str:
        if not self._smoke_in_tests():
            return ""
        return resolve_shift_left_env_secret(
            self._components_catalog(),
            selected_ids=self._selected_component_ids(),
            explicit="",
        )


    def _resolve_smoke_aws_secret(self) -> str:
        if self.smoke_aws_secret:
            return self.smoke_aws_secret
        config_secret = self._config_shift_left_env_secret()
        if config_secret:
            kc_path = getattr(self.args, "external_kubeconfig_path", None)
            if kc_path is not None:
                ensure_router_ca_in_smoke_secret(
                    tenant_namespace=self.args.namespace,
                    secret_name=config_secret,
                    target_kubeconfig=kc_path,
                )
                backfill_shift_left_smoke_secret_from_mlflow(
                    tenant_namespace=self.args.namespace,
                    secret_name=config_secret,
                )
            self.smoke_aws_secret = config_secret
            return config_secret
        return ""


    def _smoke_aws_its_override(self) -> bool:
        return bool(self._config_shift_left_env_secret())


    def _external_kubeconfig_its_override(self) -> bool:
        return bool(
            (getattr(self.args, "external_kubeconfig_path", None) is not None)
            or (getattr(self.args, "external_kubeconfig_secret", "") or "").strip()
        )


    def _print_log_stream_detach_hint(self, watch_hint: str) -> None:
        print("\nDetached from logs — PipelineRun still running on the cluster.")
        print(f"  Reattach with:\n  {watch_hint}\n")


    def cleanup(self) -> None:
        if self.its_apply_tmp and Path(self.its_apply_tmp).exists():
            Path(self.its_apply_tmp).unlink(missing_ok=True)
        if self.log_file and Path(self.log_file).exists():
            Path(self.log_file).unlink(missing_ok=True)
        if self._user_detached_from_logs:
            return
        if self.cleanup_snapshot_on_exit and self.snapshot_name:
            print("\n-- Cleaning up --")
            proc = run_cmd(
                ["oc", "delete", "snapshot", self.snapshot_name, "-n", self.args.namespace, "--ignore-not-found"],
                capture=True,
                check=False,
            )
            if proc.returncode == 0:
                print(f"  Deleted Snapshot {self.snapshot_name}")
        elif self.snapshot_name:
            print("\n-- Cleaning up --")
            print(f"  Keeping Snapshot {self.snapshot_name} for delayed trigger/debug")
        if (
            self.cleanup_external_secret_on_exit
            and self._external_secret_created_by_cli
            and self.external_kubeconfig_secret
        ):
            if not self._user_detached_from_logs:
                print("\n-- Cleaning up --")
            delete_external_kubeconfig_secret(
                namespace=self.args.namespace,
                secret_name=self.external_kubeconfig_secret,
                exclude_pipelinerun=self.pr or "",
            )


    def list_components(self) -> None:
        """Print the table of available smoke components and descriptions."""
        cat = self._components_catalog()
        print("\nAvailable rhoai-e2e smoke components:")
        print("================================================================================")
        print(f"{'Component ID':<30} | {'Description'}")
        print("--------------------------------------------------------------------------------")
        for cid in cat.component_ids:
            comp = cat.components[cid]
            desc = comp.description.strip() if comp.description else ""
            # Handle multi-line descriptions by taking the first line or truncating
            first_line = desc.split("\n")[0].strip()
            print(f"{cid:<30} | {first_line}")
        print("================================================================================")
        print(f"Total: {len(cat.component_ids)} components")
        print("Config: integration-tests/rhoai-e2e/config/rhoai-e2e-components-smoke.yaml\n")

    def check_login(self) -> None:
        who = run_cmd(["oc", "whoami"], capture=True, check=False)
        if who.returncode != 0:
            raise AppError("Not logged in. Run: oc login --server=<api-url> --web")
        self.run_owner = who.stdout.strip()
        self.token = get_jsonpath(["oc", "whoami", "-t"])
        print(
            f"User: {self.run_owner}  Product: {self.args.product}  "
            f"Namespace: {self.args.namespace}  App: {self.args.app}"
        )
        self.konflux_host_api = get_jsonpath(["oc", "whoami", "--show-server"]) or ""
        if not self.ka_host or not self.konflux_ui:
            api_server = self.konflux_host_api or get_jsonpath(["oc", "whoami", "--show-server"])
            if not self.ka_host:
                inferred_ka = derive_kubearchive_host(api_server)
                if inferred_ka:
                    self.ka_host = inferred_ka
                    print(
                        f"INFO KubeArchive URL inferred from cluster API (override with KA_HOST / --ka-host): "
                        f"{self.ka_host}"
                    )
            if not self.konflux_ui:
                inferred_ui = derive_konflux_ui_base(api_server)
                if inferred_ui:
                    self.konflux_ui = inferred_ui
                    print(
                        f"INFO Konflux UI base inferred from cluster API "
                        f"(override with KONFLUX_UI / --konflux-ui): {self.konflux_ui}"
                    )
        if self.ka_host and self.token:
            try:
                self.ka = KubeArchiveClient(self.ka_host, self.token)
            except ValueError as exc:
                raise AppError(f"Invalid --ka-host/KA_HOST value: {exc}", 2) from exc
        else:
            self.ka = None


    def ka_available(self) -> bool:
        if self.ka is None:
            return False
        ok = self.ka.check()
        if not ok:
            print(f"WARN KubeArchive API unreachable ({self.ka_host}); archived runs will not be shown.")
        return ok


    def ensure_konflux_cluster(self) -> None:
        res = run_cmd(["oc", "api-resources", "--api-group=appstudio.redhat.com"], capture=True, check=False)
        if "IntegrationTestScenario" in (res.stdout or ""):
            return
        print(f"\nWARN Current cluster ({get_jsonpath(['oc', 'whoami', '--show-server'])}) is not Konflux.")
        if not self.konflux_server:
            raise AppError("Current cluster is not Konflux and KONFLUX_SERVER/--konflux-server is not set.")
        ans = "Y"
        if sys.stdin.isatty():
            ans = input(f"   Log in to {self.konflux_server} now? [Y/n] ") or "Y"
        if not ans.lower().startswith("y"):
            raise AppError("Aborting - not connected to a Konflux cluster.")
        run_cmd(
            ["oc", "login", f"--server={self.konflux_server}", "--web"],
            capture=False,
            check=True,
            timeout=None,
        )
        res2 = run_cmd(["oc", "api-resources", "--api-group=appstudio.redhat.com"], capture=True, check=False)
        if "IntegrationTestScenario" not in (res2.stdout or ""):
            raise AppError("Still no IntegrationTestScenario CRD after login. Aborting.")
        print(f"OK Re-logged in as {get_jsonpath(['oc', 'whoami'])} on Konflux cluster")
        self.run_owner = get_jsonpath(["oc", "whoami"]) or self.run_owner
        self.token = get_jsonpath(["oc", "whoami", "-t"])
        self.konflux_host_api = get_jsonpath(["oc", "whoami", "--show-server"]) or ""
        if not (getattr(self.args, "ka_host", "") or "").strip():
            self.ka_host = derive_kubearchive_host(self.konflux_host_api) or ""
        if not (getattr(self.args, "konflux_ui", "") or "").strip():
            self.konflux_ui = derive_konflux_ui_base(self.konflux_host_api) or ""
        if self.ka_host and self.token:
            try:
                self.ka = KubeArchiveClient(self.ka_host, self.token)
            except ValueError as exc:
                raise AppError(f"Invalid --ka-host/KA_HOST value: {exc}", 2) from exc
        else:
            self.ka = None

    def _print_effective_trigger_context(self) -> None:
        """Re-print product/tests after ITS manifest defaults override CLI defaults."""
        parts = [f"Product: {self.args.product}", f"Tests: {self.args.tests}"]
        fbc = (getattr(self, "resolved_rhoai_fbc_name", "") or "").strip()
        if fbc:
            parts.append(f"FBC: {fbc}")
        print(f"Effective: {'  '.join(parts)}")


    def run(self) -> int:
        if getattr(self.args, "list_components", False):
            self.list_components()
            return 0

        self.check_login()

        if self.args.list_supported_ocp:
            self.list_supported_ocp()
            return 0

        self.ensure_konflux_cluster()

        if (self.args.run_its or "").strip():
            return self.run_integration_test_scenario()
        if (self.args.enable_its or "").strip():
            return self.enable_integration_test_scenario()
        if (self.args.disable_its or "").strip():
            return self.disable_integration_test_scenario()

        if self.args.list_pipelines:
            self.list_pipelines()
            return 0

        if self.args.delete_pending_pipelines:
            return self.delete_pending_pipelines()

        if getattr(self.args, "cleanup_maintenance", False):
            return self.run_operator_cleanup()

        if self.args.watch_mode:
            self.run_watch_mode()
        else:
            self.run_trigger_mode()

        return self._run_post_trigger_watch()



