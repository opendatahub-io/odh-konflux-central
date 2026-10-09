"""OCP version / EPHC supported-minors helpers mixin for RhoaiE2ERunner."""

from __future__ import annotations

import json
import sys
from typing import Any
from urllib.parse import quote

from runners.report.pipelinerun_summary import task_result
from suite.constants import LIST_SUPPORTED_OCP_MAX_PRS
from suite.errors import AppError
from k8s.oc_util import parse_json_output, run_cmd

from .runner_support import archived_pipelinerun_task_refs


class RunnerOcpMixin:
    @staticmethod
    def _parse_supported_versions_line(log_text: str) -> list[str] | None:
        for raw in log_text.splitlines():
            if "Supported versions:" not in raw:
                continue
            _, _, rest = raw.partition("Supported versions:")
            rest = rest.strip()
            if not rest:
                continue
            try:
                val = json.loads(rest)
            except json.JSONDecodeError:
                continue
            if isinstance(val, list) and all(isinstance(x, str) for x in val):
                return val
        return None

    def _fetch_step_log_live(self, pr_name: str, pipeline_task: str, container: str) -> str:
        prj = parse_json_output(["oc", "get", "pipelinerun", pr_name, "-n", self.args.namespace, "-o", "json"])
        tr_name = ""
        for ref in prj.get("status", {}).get("childReferences", []):
            if ref.get("pipelineTaskName") == pipeline_task:
                tr_name = ref.get("name", "") or ""
                break
        pod = ""
        if tr_name:
            tr = parse_json_output(["oc", "get", "taskrun", tr_name, "-n", self.args.namespace, "-o", "json"])
            pod = tr.get("status", {}).get("podName", "") or ""
        if not pod:
            data = parse_json_output(
                [
                    "oc",
                    "get",
                    "taskrun",
                    "-n",
                    self.args.namespace,
                    "-l",
                    f"tekton.dev/pipelineRun={pr_name}",
                    "-o",
                    "json",
                ]
            )
            for item in data.get("items", []):
                labels = item.get("metadata", {}).get("labels", {})
                if labels.get("tekton.dev/pipelineTask") != pipeline_task:
                    continue
                pod = item.get("status", {}).get("podName", "") or ""
                if pod:
                    break
        if not pod:
            return ""
        proc = run_cmd(
            ["oc", "logs", pod, "-n", self.args.namespace, "-c", container],
            capture=True,
            check=False,
        )
        if proc.returncode != 0:
            return ""
        return proc.stdout or ""

    def _archived_pipelinerun_task_refs(self, prj: dict[str, Any], pr_name: str) -> list[tuple[str, str]]:
        def _list_archived_taskruns() -> dict[str, Any]:
            sel = f"tekton.dev/pipelineRun={pr_name}"
            return self._ka_get_json_warn_empty(
                f"/apis/tekton.dev/v1/namespaces/{quote(self.args.namespace)}/taskruns?labelSelector={quote(sel)}",
                ctx="list archived TaskRuns for PipelineRun",
            )

        return archived_pipelinerun_task_refs(
            prj,
            pr_name,
            list_archived_taskruns=_list_archived_taskruns if self.ka_available() else None,
        )

    def _fetch_step_log_archived(self, pr_name: str, pipeline_task: str, container: str) -> str:
        if not self.ka_available():
            return ""
        assert self.ka is not None
        prj = self._ka_get_json_warn_empty(
            f"/apis/tekton.dev/v1/namespaces/{quote(self.args.namespace)}/pipelineruns/{quote(pr_name)}",
            ctx=f"archived step log PipelineRun {pr_name}",
        )
        if not prj.get("metadata", {}).get("name"):
            return ""
        tr_name = ""
        for ref in prj.get("status", {}).get("childReferences", []):
            if ref.get("pipelineTaskName") == pipeline_task:
                tr_name = ref.get("name", "") or ""
                break
        if not tr_name:
            for ttr, task_label in self._archived_pipelinerun_task_refs(prj, pr_name):
                if task_label == pipeline_task:
                    tr_name = ttr
                    break
        if not tr_name:
            return ""
        pods = self._ka_get_json_warn_empty(
            f"/api/v1/namespaces/{quote(self.args.namespace)}/pods?labelSelector={quote(f'tekton.dev/taskRun={tr_name}')}",
            ctx=f"archived step log pods {pipeline_task}",
        )
        items = pods.get("items", [])
        if not items:
            return ""
        pod = items[0].get("metadata", {}).get("name", "")
        if not pod:
            return ""
        log_path = (
            f"/api/v1/namespaces/{quote(self.args.namespace)}/pods/{quote(pod)}"
            f"/log?container={quote(container)}"
        )
        return self._ka_get_text_warn_empty(log_path, ctx=f"archived step log {pipeline_task}:{container}")

    def _fetch_install_ocp_cluster_supported_log(self, pr_name: str, source: str) -> str:
        out = ""
        for pipeline_task in ("stage-ephemeral-kubeconfig", "install-ocp-cluster", "provision-cluster"):
            if source == "live":
                out = self._fetch_step_log_live(pr_name, pipeline_task, "step-get-supported-versions")
            if (not out or not out.strip()) and self.ka_available():
                archived = self._fetch_step_log_archived(
                    pr_name, pipeline_task, "step-get-supported-versions"
                )
                if archived.strip():
                    out = archived
            if out.strip():
                break
        return out

    def _taskruns_for_pipelinerun(self, pr_name: str, source: str) -> list[dict[str, Any]]:
        if source == "live":
            proc = run_cmd(
                [
                    "oc",
                    "get",
                    "taskrun",
                    "-n",
                    self.args.namespace,
                    "-l",
                    f"tekton.dev/pipelineRun={pr_name}",
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
        if not self.ka_available():
            return []
        sel = f"tekton.dev/pipelineRun={pr_name}"
        data = self._ka_get_json_warn_empty(
            f"/apis/tekton.dev/v1/namespaces/{quote(self.args.namespace)}/taskruns?labelSelector={quote(sel)}",
            ctx=f"archived TaskRuns for PipelineRun {pr_name}",
        )
        items = data.get("items")
        if not isinstance(items, list):
            return []
        return [x for x in items if isinstance(x, dict)]

    def _fetch_ephc_supported_versions(self, pr_name: str, source: str) -> list[str] | None:
        log_text = self._fetch_install_ocp_cluster_supported_log(pr_name, source)
        versions = self._parse_supported_versions_line(log_text)
        if versions:
            return versions
        taskruns = self._taskruns_for_pipelinerun(pr_name, source)
        for pipeline_task in ("resolve-oci-releases", "stage-ephemeral-kubeconfig"):
            minor = task_result(taskruns, pipeline_task, "ocpMinor").strip()
            if minor:
                return [minor]
        return None

    def _validate_ocp_version_in_supported_list(self, versions: list[str]) -> None:
        want = (self.args.ocp_version or "").strip()
        if not want:
            return
        if want in versions:
            print(f"\n--ocp-version {want!r} is in the supported list above.")
            return
        raise AppError(
            f"--ocp-version {want!r} is not in the EPHC-supported minors from this log snapshot: {versions}. "
            "Choose a minor from the list, or drop --list-supported-ocp to trigger a run without this check.",
            2,
        )

    def list_supported_ocp(self) -> None:
        merged = self._merged_pipelinerun_rows(LIST_SUPPORTED_OCP_MAX_PRS, rhoai_e2e_family_only=True)
        print(
            f"EPHC-supported OpenShift minors (from pipeline task results or step logs), "
            f"app={self.args.app!r} namespace={self.args.namespace!r}, "
            f"scanning up to {LIST_SUPPORTED_OCP_MAX_PRS} newest rhoai-e2e PipelineRun(s):"
        )
        if not merged:
            print(f"No rhoai-e2e PipelineRuns found for app '{self.args.app}'.")
            print("Tip: use --konflux-app <name> or trigger a run; set --ka-host / KA_HOST if runs are archived off-cluster.")
            prj = run_cmd(["oc", "project", "-q"], capture=True, check=False)
            if prj.returncode == 0:
                current_ns = (prj.stdout or "").strip()
                if current_ns and current_ns != self.args.namespace:
                    print(
                        f"Tip: active oc project is '{current_ns}', but this command uses "
                        f"namespace '{self.args.namespace}'. Use --konflux-namespace {current_ns} or: oc project {self.args.namespace}"
                    )
            raise AppError("No candidate PipelineRuns to scan", 1)

        for row in merged:
            versions = self._fetch_ephc_supported_versions(row.name, row.source)
            if versions:
                print("")
                print("Supported minors (newest first):")
                for v in versions:
                    print(f"  {v}")
                print("")
                print(f"Source: PipelineRun {row.name} ({row.source})")
                self._validate_ocp_version_in_supported_list(versions)
                return

        raise AppError(
            "Could not read EPHC-supported OpenShift minors from resolve-oci-releases / "
            "stage-ephemeral-kubeconfig task results or legacy get-supported-versions logs "
            f"for any of {len(merged)} scanned run(s). "
            "The tasks may not have run yet, logs may be rotated, or the task name may differ — "
            "try -l and watch a fresh run, or confirm KubeArchive (--ka-host).",
            1,
        )
