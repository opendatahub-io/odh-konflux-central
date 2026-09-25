"""Watch/log replay helpers mixin for RhoaiE2ERunner."""

from __future__ import annotations

import shutil
import subprocess
import sys
import tempfile
import time
from collections import deque
from typing import Any
from urllib.parse import quote

from suite.constants import PENDING_REASONS
from suite.errors import AppError
from k8s.oc_util import get_jsonpath, run_cmd, ts_now
from .runner_support import (
    Tee,
    _TKN_LOG_STREAM_DETACH_RC,
    _format_live_tkn_log_line,
    _normalize_replayed_pod_log,
    archived_pipelinerun_task_refs,
    format_rhoai_e2e_watch_cli,
    format_taskrun_failure_detail,
)


class RunnerWatchMixin:
    def run_watch_mode(self) -> None:
        if self.args.watch:
            print(f"Watch mode: explicit PipelineRun '{self.args.watch}'")
            if run_cmd(["oc", "get", "pipelinerun", self.args.watch, "-n", self.args.namespace], capture=True, check=False).returncode == 0:
                self.pr = self.args.watch
            elif self.ka_available():
                assert self.ka is not None
                prj = self._ka_get_json_warn_empty(
                    f"/apis/tekton.dev/v1/namespaces/{quote(self.args.namespace)}/pipelineruns/{quote(self.args.watch)}",
                    ctx="watch explicit PipelineRun (archived)",
                )
                if prj.get("metadata", {}).get("name"):
                    self.pr = prj["metadata"]["name"]
                    self.watch_from_archive = True
                    self.watch_completed = True
                    print("Found PipelineRun in KubeArchive (pruned from live cluster).")
                else:
                    raise AppError(f"PipelineRun not found in namespace '{self.args.namespace}' or in KubeArchive: {self.args.watch}")
            else:
                raise AppError(f"PipelineRun not found in namespace '{self.args.namespace}': {self.args.watch}")
        else:
            print(f"Watch mode: newest non-smoke rhoai-e2e PipelineRun for app '{self.args.app}' (same merge order as --list-pipelines)…")
            self.pr = self.find_newest_rhoai_e2e_any_owner_for_watch()
            if self.pr:
                print(f"  Selected: {self.pr}")
                if run_cmd(["oc", "get", "pipelinerun", self.pr, "-n", self.args.namespace], capture=True, check=False).returncode != 0:
                    if self.ka_available():
                        assert self.ka is not None
                        prj = self._ka_get_json_warn_empty(
                            f"/apis/tekton.dev/v1/namespaces/{quote(self.args.namespace)}/pipelineruns/{quote(self.pr)}",
                            ctx="watch selected PipelineRun (archived)",
                        )
                        if prj.get("metadata", {}).get("name"):
                            self.watch_from_archive = True
                            self.watch_completed = True
                            print("  Live copy pruned — replaying from KubeArchive.")
                        else:
                            raise AppError(
                                f"PipelineRun {self.pr!r} not found on cluster or in KubeArchive "
                                f"(namespace {self.args.namespace!r})."
                            )
                    else:
                        raise AppError(
                            f"PipelineRun {self.pr!r} not found in namespace {self.args.namespace!r} "
                            f"(KubeArchive unset or unreachable; set KA_HOST / --ka-host for archived runs)."
                        )
            if not self.pr:
                print(f"  No rhoai-e2e run in --list-pipelines window; trying run-owner / Snapshot match for {self.run_owner!r}…")
                self.pr = self.find_owned_live_watch_pr()
                if self.pr:
                    print(f"  Found latest owned PipelineRun (live): {self.pr}")
                if not self.pr:
                    self.pr = self.find_owned_archived_watch_pr()
                    if self.pr:
                        self.watch_from_archive = True
                        self.watch_completed = True
                        print(f"  Found archived owned PipelineRun: {self.pr}")
            if not self.pr:
                raise AppError(
                    f"No rhoai-e2e PipelineRun found for app '{self.args.app}' (live or archived).\n"
                    "Use '--watch <pipelinerun>' to target a specific run, or run with trigger flags (for example --product rhoai)."
                )

        if self.watch_from_archive:
            assert self.ka is not None
            prj = self._ka_get_json_warn_empty(
                f"/apis/tekton.dev/v1/namespaces/{quote(self.args.namespace)}/pipelineruns/{quote(self.pr)}",
                ctx="watch archived PipelineRun metadata",
            )
            cond = next((c for c in prj.get("status", {}).get("conditions", []) if c.get("type") == "Succeeded"), {})
            if cond.get("status") == "True":
                self.ka_succeeded = "Succeeded"
            elif cond.get("status") == "False":
                self.ka_succeeded = "Failed"
            else:
                self.ka_succeeded = "Unknown"
            ctime = prj.get("status", {}).get("completionTime", "")
            print(f"PipelineRun {self.pr} is archived ({self.ka_succeeded}, completionTime={ctime or '?'}). Replaying logs from KubeArchive.")
        else:
            ctime = get_jsonpath(
                ["oc", "get", "pipelinerun", self.pr, "-n", self.args.namespace, "-o", "jsonpath={.status.completionTime}"]
            )
            if ctime:
                self.watch_completed = True
                print(f"PipelineRun {self.pr} is already completed (completionTime={ctime}). Showing recent logs/status.")


    def _archived_pipelinerun_task_refs(self, prj: dict[str, Any], pr_name: str) -> list[tuple[str, str]]:
        """Resolve TaskRun name + pipeline task label from archived PR JSON."""
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


    def replay_archived_logs(self) -> None:
        assert self.ka is not None
        prj = self._ka_get_json_warn_empty(
            f"/apis/tekton.dev/v1/namespaces/{quote(self.args.namespace)}/pipelineruns/{quote(self.pr)}",
            ctx="replay archived PipelineRun",
        )
        if not prj.get("metadata", {}).get("name"):
            print(
                "WARN Could not load PipelineRun from KubeArchive (missing, auth, or empty); skipping log replay.",
                file=sys.stderr,
            )
            return
        task_refs = self._archived_pipelinerun_task_refs(prj, self.pr)
        if not task_refs:
            cond = next((c for c in prj.get("status", {}).get("conditions", []) if c.get("type") == "Succeeded"), {})
            reason = (cond.get("reason") or "").strip()
            message = (cond.get("message") or "").strip()
            print("(no TaskRuns found in KubeArchive for this PipelineRun — childReferences missing and no labeled TaskRuns.)")
            if reason or message:
                print("Tekton Succeeded condition (from archived PipelineRun):")
                if reason:
                    print(f"  reason: {reason}")
                if message:
                    for line in message.splitlines():
                        print(f"  message: {line}")
                if self._is_resolver_couldnt_get_pipeline(reason, message):
                    self._warn_couldnt_get_pipeline_git_source()
            else:
                print("No condition message on archived object; open the Konflux UI link above for task-level logs.")
            return
        if not prj.get("status", {}).get("childReferences"):
            print(
                "INFO Archived PipelineRun has no status.childReferences; "
                "replaying logs from TaskRuns listed by label tekton.dev/pipelineRun=…"
            )
        for tr_name, task_name in task_refs:
            tr_obj = self._ka_get_json_warn_empty(
                f"/apis/tekton.dev/v1/namespaces/{quote(self.args.namespace)}/taskruns/{quote(tr_name)}",
                ctx=f"replay archived TaskRun {task_name}",
            )
            pods = self._ka_get_json_warn_empty(
                f"/api/v1/namespaces/{quote(self.args.namespace)}/pods?labelSelector={quote(f'tekton.dev/taskRun={tr_name}')}",
                ctx=f"replay archived pods for task {task_name}",
            )
            items = pods.get("items", [])
            if not items:
                print(f"\n[{task_name}] (no pod found)")
                print(format_taskrun_failure_detail(tr_obj))
                continue
            pod = items[0].get("metadata", {}).get("name", "")
            pod_obj = self._ka_get_json_warn_empty(
                f"/api/v1/namespaces/{quote(self.args.namespace)}/pods/{quote(pod)}",
                ctx=f"replay archived pod {pod}",
            )
            containers = [c.get("name", "") for c in pod_obj.get("spec", {}).get("initContainers", [])] + [
                c.get("name", "") for c in pod_obj.get("spec", {}).get("containers", [])
            ]
            task_emitted = False
            for ctr in containers:
                if ctr in {"prepare", "place-scripts", "place-tools"}:
                    continue
                print(f"\n[{ts_now()}] [{task_name}:{ctr}]")
                t_log = time.monotonic()
                log_path = (
                    f"/api/v1/namespaces/{quote(self.args.namespace)}/pods/{quote(pod)}"
                    f"/log?container={quote(ctr)}"
                )
                log_text = _normalize_replayed_pod_log(
                    self._ka_get_text_warn_empty(log_path, ctx=f"replay [{task_name}:{ctr}]")
                )
                log_elapsed = time.monotonic() - t_log
                if log_text.strip():
                    task_emitted = True
                    print(log_text, end="")
                    if not log_text.endswith("\n"):
                        print(flush=True)
                print(f"— KubeArchive: {log_elapsed:.1f}s, {len(log_text)} chars", flush=True)
            if not task_emitted:
                print(format_taskrun_failure_detail(tr_obj, pod=pod_obj))


    def _replay_archived_logs_to_log_file(self) -> None:
        tmp_log = tempfile.NamedTemporaryFile(prefix="rhoai-e2e-run.", delete=False)
        self.log_file = tmp_log.name
        tmp_log.close()
        print(f"[{ts_now()}] Replaying archived logs from KubeArchive...")
        with open(self.log_file, "w", encoding="utf-8") as fh:
            old_stdout = sys.stdout
            try:
                sys.stdout = Tee(sys.stdout, fh)
                self.replay_archived_logs()
            finally:
                sys.stdout = old_stdout


    def _try_kubearchive_log_replay(self) -> bool:
        """When the PipelineRun exists on-cluster but tkn has no pod logs, replay from KubeArchive."""
        if not self.ka_available():
            print(
                "WARN Live task logs are unavailable and KubeArchive is not configured "
                "(set KA_HOST / --ka-host).",
                file=sys.stderr,
            )
            return False
        assert self.ka is not None
        prj = self._ka_get_json_warn_empty(
            f"/apis/tekton.dev/v1/namespaces/{quote(self.args.namespace)}/pipelineruns/{quote(self.pr)}",
            ctx="replay PipelineRun logs from KubeArchive",
        )
        if not prj.get("metadata", {}).get("name"):
            print(
                "WARN PipelineRun not found in KubeArchive; logs unavailable.",
                file=sys.stderr,
            )
            return False
        self._kubearchive_log_replay = True
        self.ka_succeeded = self._ka_succeeded_from_prj(prj)
        print("Live task logs unavailable — replaying from KubeArchive.")
        self._replay_archived_logs_to_log_file()
        return True


    def stream_live_logs(self) -> bool:
        tmp_log = tempfile.NamedTemporaryFile(prefix="rhoai-e2e-run.", delete=False)
        self.log_file = tmp_log.name
        tmp_log.close()
        tkn_bin = shutil.which("tkn")
        pr_name = (self.pr or "").strip()
        if not pr_name:
            print("WARN No PipelineRun name set; skipping tkn log stream.", file=sys.stderr)
            return False
        watch_hint = format_rhoai_e2e_watch_cli(
            operator_install_dir=self.script_dir,
            namespace=self.args.namespace,
            app=self.args.app,
            pipelinerun=pr_name,
        )
        if tkn_bin:
            if self.watch_completed:
                print("Pipeline is already finished - showing last 200 log lines via tkn...")
                # tkn expects: pipelinerun logs <name> -n <namespace> (name after logs, not before -n).
                lines: list[str] = []
                last_rc = 0
                for extra in ([], ["-a"]):
                    cmd = [tkn_bin, "pipelinerun", "logs", pr_name, "-n", self.args.namespace, *extra]
                    proc = subprocess.Popen(
                        cmd,
                        stdout=subprocess.PIPE,
                        stderr=subprocess.STDOUT,
                        text=True,
                        encoding="utf-8",
                        errors="replace",
                    )
                    assert proc.stdout is not None
                    tail: deque[str] = deque(maxlen=200)
                    for line in proc.stdout:
                        tail.append(line.rstrip("\n"))
                    last_rc = proc.wait()
                    lines = list(tail)
                    if last_rc == 0 and lines:
                        break
                if not lines or last_rc != 0:
                    print(
                        f"WARN No log lines from tkn (exit {last_rc}); "
                        "will try KubeArchive if available.",
                        file=sys.stderr,
                    )
                    return False
                with open(self.log_file, "w", encoding="utf-8") as fh:
                    for line in lines:
                        out = _format_live_tkn_log_line(line)
                        if out is None:
                            continue
                        print(out)
                        fh.write(out + "\n")
                return True
            print("Streaming logs via tkn (Ctrl-C to detach, pipeline keeps running)...")
            with open(self.log_file, "w", encoding="utf-8") as fh:
                p = subprocess.Popen(
                    [tkn_bin, "pipelinerun", "logs", pr_name, "-n", self.args.namespace, "-f"],
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                )
                assert p.stdout is not None
                detached = False
                saw_log_lines = False
                stream_failed = False
                try:
                    for line in p.stdout:
                        out = _format_live_tkn_log_line(line)
                        if out is None:
                            continue
                        saw_log_lines = True
                        print(out)
                        fh.write(out + "\n")
                except KeyboardInterrupt:
                    self.mark_detached_from_logs()
                    self._print_log_stream_detach_hint(watch_hint)
                    raise
                finally:
                    try:
                        p.stdout.close()
                    except OSError:
                        pass
                    try:
                        rc = p.wait(timeout=120)
                    except subprocess.TimeoutExpired:
                        p.kill()
                        rc = p.wait()
                        print("WARN tkn did not exit after log stream closed; killed process", file=sys.stderr)
                        stream_failed = True
                    except KeyboardInterrupt:
                        self.mark_detached_from_logs()
                        self._print_log_stream_detach_hint(watch_hint)
                        raise
                    if rc in _TKN_LOG_STREAM_DETACH_RC:
                        self.mark_detached_from_logs()
                        self._print_log_stream_detach_hint(watch_hint)
                        detached = True
                    elif rc != 0:
                        print(f"WARN tkn exited with code {rc}", file=sys.stderr)
                        print(f"  Reattach or replay logs with:\n  {watch_hint}", file=sys.stderr)
                        stream_failed = not saw_log_lines
                if detached:
                    return True
                if stream_failed:
                    return False
            return True
        print(
            "tkn not found — polling status with oc (install tkn for live streaming in trigger mode). "
            f"Or run:\n  {watch_hint}"
        )
        deadline = time.time() + 5400
        while time.time() < deadline:
            cstat, reason = self.succeeded_condition(self.pr)
            print(f"  {ts_now()}  succeeded-condition: {cstat}  reason: {reason or '?'}")
            if cstat == "True":
                print("Pipeline succeeded")
                return True
            if cstat == "False":
                self.pipeline_exit = 1
                _, r, m = self.succeeded_condition_detail(self.pr)
                print(f"Pipeline failed ({r or 'Failed'})")
                if self._is_resolver_couldnt_get_pipeline(r, m):
                    self._warn_couldnt_get_pipeline_git_source()
                return True
            time.sleep(15)
        self.pipeline_exit = 1
        raise AppError("Polling timed out before pipeline reached a terminal state")

    def _run_post_trigger_watch(self) -> int:
        """Wait for pipeline start, stream logs, and return exit code (trigger and --run-its)."""
        self.print_run_summary(self._status_label_for_summary_preview(), phase="preview")

        if self.watch_from_archive:
            self._replay_archived_logs_to_log_file()
            self.print_run_summary(self.ka_succeeded, phase="final")
            if self.ka_succeeded != "Succeeded":
                self.pipeline_exit = 1
            return self.pipeline_exit

        if not self.watch_completed:
            wait_deadline = time.time() + self.pipeline_start_wait_seconds
            wait_start = time.time()
            print(
                f"Waiting for pipeline to start running (up to {self.pipeline_start_wait_seconds}s, "
                "override with OLMINSTALL_PIPELINE_START_WAIT_SECONDS)..."
            )
            while time.time() < wait_deadline:
                cstat, reason, message = self.succeeded_condition_detail(self.pr)
                if cstat == "False" and self._is_resolver_couldnt_get_pipeline(reason, message):
                    self._raise_resolver_terminal(self.pr, reason, message)
                if reason in PENDING_REASONS:
                    elapsed = int(time.time() - wait_start)
                    print(f"  {ts_now()}  {reason or 'pending'} ({elapsed}s)")
                    time.sleep(10)
                    continue
                print(f"  {ts_now()}  {reason or 'starting'} - ready to stream")
                break
            else:
                self.pipeline_exit = 1
                wmin = max(1, self.pipeline_start_wait_seconds // 60)
                raise AppError(
                    f"Pipeline still pending after {wmin}m ({self.pipeline_start_wait_seconds}s). Check Konflux:\n"
                    f"{self.konflux_ui}/ns/{self.args.namespace}/applications/{self.args.app}/pipelineruns/{self.pr}"
                )

        try:
            logs_shown = self.stream_live_logs()
        except KeyboardInterrupt:
            self.mark_detached_from_logs()
            return 130
        if self._user_detached_from_logs:
            return 130

        if not logs_shown and self.watch_completed:
            self._try_kubearchive_log_replay()

        final_cstat, final_reason, final_msg = self.succeeded_condition_detail(self.pr)
        self._reenable_external_secret_cleanup_on_terminal(final_cstat)
        if final_cstat == "False" and self._is_resolver_couldnt_get_pipeline(final_reason, final_msg):
            self._warn_couldnt_get_pipeline_git_source()
        self.print_run_summary(self._terminal_status_label(), phase="final")
        if final_cstat != "True":
            self.pipeline_exit = 1
        return self.pipeline_exit


