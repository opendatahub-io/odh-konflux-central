"""Konflux olminstall CLI orchestration (watch, list, trigger snapshot)."""

from __future__ import annotations

import argparse
import contextlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from collections.abc import Iterator
from typing import Any, TextIO
from urllib.parse import quote

from .constants import (
    DEFAULT_APP,
    DEFAULT_ARTIFACT_BROWSER_REPO_PATH,
    DEFAULT_ARTIFACT_BROWSER_URL,
    DEFAULT_LIST_COUNT,
    DEFAULT_NAMESPACE,
    ITS_TESTS_PARAM_DEFAULT,
    LIST_SUPPORTED_OCP_MAX_PRS,
    OLMINSTALL_CTX_PRINT_KEYS,
    RHOAI_FBCF_IMAGE_REF_PATTERN,
    OLMINSTALL_TESTOPS_ITS_NAME,
    OLMINSTALL_WRITE_ANNOTATION_KEYS,
    PENDING_REASONS,
    STALE_TESTOPS_PLAYPEN_ITS_NAMES,
    olminstall_smoke_only_pipelinerun,
)
from .errors import AppError
from .kubearchive import KubeArchiveAuthError, KubeArchiveClient
from .oc_util import (
    derive_konflux_ui_base,
    derive_kubearchive_host,
    filter_warning_lines,
    get_jsonpath,
    parse_json_output,
    run_cmd,
    ts_now,
)


_DNS1123_SUBDOMAIN_RE = re.compile(r"^[a-z0-9]([-a-z0-9]*[a-z0-9])?$", re.ASCII)


def _snapshot_param_is_resource_name(snap: str) -> bool:
    """True when SNAPSHOT param looks like a Kubernetes object name (not inline JSON)."""
    s = (snap or "").strip()
    if not s or s[0] in "{[":
        return False
    if len(s) > 253:
        return False
    return bool(_DNS1123_SUBDOMAIN_RE.fullmatch(s))


_CTX_ANNOTATION_LABELS: dict[str, str] = {
    "olminstall.run-owner": "Run owner",
    "olminstall.product": "Product",
    "olminstall.update-channel": "Update channel",
    "olminstall.rhoai-version": "RHOAI version",
    "olminstall.ocp-version": "OCP version (ephemeral)",
    "olminstall.scripts-repo-url": "Scripts repo",
    "olminstall.scripts-repo-revision": "Scripts branch/revision",
    "olminstall.tests": "Test phases (TESTS)",
    "olminstall.slack-channel-id": "Slack channel ID",
    "olminstall.fbcf-image": "FBCF image",
    "olminstall.operator-version": "Operator version",
    "olminstall.ephemeral-cluster": "Ephemeral CTI",
    "olminstall.test-results-url": "Test Results",
    "olminstall.artifacts-status": "Artifacts status",
    "olminstall.pipeline-test-output": "Pipeline test output",
}


# Normalise container log lines: structlog pads levels as "[info     ]"; pytest sometimes
# glues "PASSED" to the next ISO timestamp. Used for KubeArchive replay and live tkn streams.
_REPLAY_STATUS_TIMESTAMP_GLUE = re.compile(
    r"(PASSED|FAILED|SKIPPED|ERROR|XFAIL|XPASS)(?=20\d{2}-\d{2}-\d{2}T)"
)
_LOG_LEVEL_BRACKET_PAD = re.compile(
    r"\[((?:info|warning|error|debug|critical|exception|trace))\s+\]",
    re.IGNORECASE,
)
# Other "[Token    ]" padding (e.g. rare logger names) — token must start with a letter.
_REPLAY_LOG_BRACKET_PAD = re.compile(r"\[([A-Za-z][A-Za-z0-9_]*)\s{2,}\]")

# ``tkn pipelinerun logs -f`` exit codes when the user interrupts log streaming (Ctrl-C).
_TKN_LOG_STREAM_DETACH_RC = frozenset({-2, 130, 143})


def _normalize_log_line_for_display(line: str) -> str:
    """Collapse padded bracket tags and fix status/timestamp glue for one log line."""
    if not line:
        return line
    line = _REPLAY_STATUS_TIMESTAMP_GLUE.sub(r"\1\n", line)
    line = _LOG_LEVEL_BRACKET_PAD.sub(lambda m: f"[{m.group(1).lower()}]", line)
    line = _REPLAY_LOG_BRACKET_PAD.sub(r"[\1]", line)
    return line


def _format_live_tkn_log_line(raw_line: str) -> str | None:
    """Prefix one ``tkn`` log line with a capture time; return ``None`` to skip blank noise lines."""
    body = _normalize_log_line_for_display(raw_line.rstrip("\r\n"))
    if not body.strip():
        return None
    return f"[{ts_now()}] {body}"


def _normalize_replayed_pod_log(text: str) -> str:
    """Tidy common formatting glitches in archived container logs when printing."""
    if not text:
        return text
    return "".join(_normalize_log_line_for_display(line) for line in text.splitlines(keepends=True))


def _interactive_progress_stream() -> TextIO:
    """Stream for in-place progress (spinner). Prefer stdout; if not a TTY, use stderr (common in IDE runners)."""
    if sys.stdout.isatty():
        return sys.stdout
    if sys.stderr.isatty():
        return sys.stderr
    return sys.stdout


@contextlib.contextmanager
def spin_while(description: str) -> Iterator[None]:
    """Show a spinner (or ``…`` on non-TTY) while a slow block runs; TTY ends with ``description ok``."""
    wait_stream = _interactive_progress_stream()
    term = (os.environ.get("TERM") or "").strip()
    use_spinner = wait_stream.isatty() and term != "dumb"
    frames = "|/-\\"
    stop = threading.Event()
    desc = description.rstrip()
    th: threading.Thread | None = None
    if use_spinner:

        def _spin() -> None:
            i = 0
            while not stop.is_set():
                ch = frames[i % len(frames)]
                try:
                    wait_stream.write(f"\r\033[K{desc} {ch}")
                    wait_stream.flush()
                except BrokenPipeError:
                    return
                i += 1
                if stop.wait(0.12):
                    return

        th = threading.Thread(target=_spin, name="olminstall-spin", daemon=True)
        th.start()
    else:
        try:
            wait_stream.write(f"{desc}...\n")
            wait_stream.flush()
        except BrokenPipeError:
            pass
    failed = False
    try:
        yield
    except BaseException:
        failed = True
        raise
    finally:
        if th is not None:
            stop.set()
            th.join(timeout=5.0)
        if use_spinner:
            try:
                wait_stream.write("\r\033[K")
                wait_stream.flush()
                if failed:
                    wait_stream.write(f"{desc} (stopped)\n")
                else:
                    wait_stream.write(f"{desc} ok\n")
                wait_stream.flush()
            except BrokenPipeError:
                pass
        elif failed:
            try:
                wait_stream.write(f"{desc} (stopped)\n")
                wait_stream.flush()
            except BrokenPipeError:
                pass


def format_olm_pipeline_watch_cli(
    *,
    olminstall_dir: Path,
    namespace: str,
    app: str,
    pipelinerun: str | None,
) -> str:
    """Copy-pastable command: stream logs or KubeArchive replay (see ``--watch``)."""
    script = olminstall_dir / "olm_pipeline.py"
    base = f"{sys.executable} {script} --watch"
    if (pipelinerun or "").strip():
        base = f"{base} {pipelinerun.strip()}"
    return f"{base} -n {namespace} --app {app}"


def first_snapshot_component_name(snapshot_yaml: str) -> str:
    """Template components[].name from integration-tests/olminstall/test-snapshot.yaml."""
    m = re.search(r"(?m)^\s+-\s+name:\s+(\S+)\s*$", snapshot_yaml)
    if not m:
        snippet = snapshot_yaml[:200].replace("\n", " ")
        raise AppError(
            "Could not locate the first snapshot component name in test-snapshot.yaml "
            f"(template drift?). Snippet: {snippet!r}"
        )
    return m.group(1)


@dataclass
class PipelineRow:
    name: str
    app: str
    state: str
    created: str
    source: str


class OLMInstallRunner:
    def __init__(self, args: argparse.Namespace) -> None:
        self.args = args
        self.script_dir = Path(__file__).resolve().parent.parent
        self.snapshot_file = self.script_dir / "test-snapshot.yaml"
        self.its_file = self.script_dir / "its-olminstall-rhoai-tenant.yaml"
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
        self.image = args.image or ""
        self.update_channel_override = args.channel or ""
        # Filled after ``create_snapshot`` — used to match PipelineRun ``SNAPSHOT`` JSON params to this trigger.
        self._trigger_snapshot_spec: dict[str, Any] | None = None
        self._trigger_snapshot_created_ts = ""

    def _tests_its_override(self) -> bool:
        """True when CLI should inject TESTS into the ITS (matches annotation logic)."""
        return getattr(self.args, "tests_explicit", False) or self.args.tests != getattr(
            self.args, "tests_catalog_default_csv", ITS_TESTS_PARAM_DEFAULT
        )

    def build_olminstall_context_annotations(self) -> dict[str, str]:
        """Safe, non-secret trigger context for Snapshot / PipelineRun metadata."""
        out: dict[str, str] = {"olminstall.product": self.args.product}
        if self.update_channel_override:
            out["olminstall.update-channel"] = self.update_channel_override
        ver = (self.args.version or "").strip()
        if self.args.product == "rhoai" and ver:
            out["olminstall.rhoai-version"] = ver
        if self.args.ocp_version:
            out["olminstall.ocp-version"] = self.args.ocp_version
        if self.args.konflux_repo:
            out["olminstall.scripts-repo-url"] = self.args.konflux_repo
        if self.args.konflux_branch:
            out["olminstall.scripts-repo-revision"] = self.args.konflux_branch
        if self._tests_its_override():
            out["olminstall.tests"] = self.args.tests
        if (self.args.slack_channel_id or "").strip():
            out["olminstall.slack-channel-id"] = self.args.slack_channel_id.strip()
        return out

    def early_summary_annotate_argv(self) -> list[str]:
        """Predicted BVT artifact URL on the PipelineRun so Konflux UI shows it while the run is in progress."""
        from .bvt_artifacts import tests_include_bvt
        from .pipelinerun_summary import predicted_artifacts_browser_url

        if not (self.pr or "").strip():
            return []
        tests_csv = (self.args.tests or "").strip()
        if not tests_include_bvt(tests_csv):
            return []
        prj = self.get_pipelinerun_json_for_display()
        url = predicted_artifacts_browser_url(prj, self.pr)
        return [f"olminstall.test-results-url={url}"]

    def olminstall_context_annotate_argv(self) -> list[str]:
        ctx = self.build_olminstall_context_annotations()
        return [f"{k}={ctx[k]}" for k in OLMINSTALL_WRITE_ANNOTATION_KEYS if k in ctx]

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
        for key in OLMINSTALL_CTX_PRINT_KEYS:
            val = ann.get(key)
            if val:
                label = _CTX_ANNOTATION_LABELS.get(key, key)
                lines.append(f"  {label}: {val}")
        return lines

    def _pipelinerun_param_value(self, prj: dict[str, Any], name: str, default: str = "") -> str:
        for p in prj.get("spec", {}).get("params", []) or []:
            if p.get("name") != name:
                continue
            val = p.get("value")
            if isinstance(val, str) and val.strip():
                return val.strip()
        return default

    def bvt_artifacts_browser_url(self, prj: dict[str, Any] | None = None) -> str:
        """Per-run BVT folder in the OCI artifact browser (URL pattern from pipeline params / defaults)."""
        from .pipelinerun_summary import predicted_artifacts_browser_url

        data = prj if prj is not None else self.get_pipelinerun_json_for_display()
        return predicted_artifacts_browser_url(data, (self.pr or "").strip())

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

    def read_bvt_artifacts_url_from_taskruns(self) -> str:
        """Published ``ARTIFACTS_URL`` from a BVT TaskRun; empty if not uploaded."""
        from .bvt_artifacts import published_artifacts_url_from_taskruns

        return published_artifacts_url_from_taskruns(self.read_taskruns_for_pr())

    def test_results_url(self, prj: dict[str, Any] | None = None) -> str:
        """BVT artifact browser URL when published; otherwise a short reason or omission hint."""
        from .bvt_artifacts import resolve_artifacts_notification_line

        data = prj if prj is not None else self.get_pipelinerun_json_for_display()
        ann = (data.get("metadata") or {}).get("annotations") or {}
        url_ann = (ann.get("olminstall.test-results-url") or "").strip()
        if url_ann:
            return url_ann
        tests = self._pipelinerun_param_value(data, "TESTS", "")
        line = resolve_artifacts_notification_line(
            tests_csv=tests,
            pipeline_run=(self.pr or "").strip(),
            taskruns=self.read_taskruns_for_pr(),
        )
        if line is None:
            return "(BVT not in TESTS)"
        if line.startswith("Artifacts: "):
            return line[len("Artifacts: ") :]
        return line

    def read_provision_cluster_cti_name(self) -> str:
        """Best-effort CTI / HyperShift object name from the provision-cluster TaskRun (live cluster only)."""
        prj = self.get_pipelinerun_json_for_display()
        ann = (prj.get("metadata") or {}).get("annotations") or {}
        cti_ann = (ann.get("olminstall.ephemeral-cluster") or "").strip()
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
            if labels.get("tekton.dev/pipelineTask") != "provision-cluster":
                continue
            for r in (item.get("status") or {}).get("results", []) or []:
                if r.get("name") == "clusterName" and isinstance(r.get("value"), str):
                    return r["value"].strip()
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
        op_ver = (ann.get("olminstall.operator-version") or "").strip()
        if phase == "final" and not op_ver and self.log_file and Path(self.log_file).exists():
            txt = Path(self.log_file).read_text(encoding="utf-8", errors="ignore")
            m = re.findall(r"Operator version\s*:\s*([^\s]+)", txt)
            op_ver = m[-1] if m else ""
        watch_cmd = format_olm_pipeline_watch_cli(
            olminstall_dir=self.script_dir,
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
        if op_ver:
            print(f"  Operator     : {op_ver}")
        artifacts_status = (ann.get("olminstall.artifacts-status") or "").strip()
        if artifacts_status:
            print(f"  Artifacts    : {artifacts_status}")
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
        if cti:
            print(f"  Ephemeral CTI: {cti}")
        ctx = self._trigger_context_lines(prj)
        print("")
        if ctx:
            print("Trigger context (PipelineRun annotations):")
            for ln in ctx:
                print(ln)
        else:
            print("Trigger context: (no olminstall.* annotations on this PipelineRun)")
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

    def get_pipelineruns(self, namespace: str, selector: str | None = None) -> list[dict[str, Any]]:
        cmd = ["oc", "get", "pipelineruns", "-n", namespace, "-o", "json"]
        if selector:
            cmd.extend(["-l", selector])
        data = parse_json_output(cmd)
        return data.get("items", []) if data else []

    def succeeded_condition_detail(self, pr_name: str) -> tuple[str, str, str]:
        """``Succeeded`` condition: status, reason, message (empty strings if missing)."""
        data = parse_json_output(["oc", "get", "pipelinerun", pr_name, "-n", self.args.namespace, "-o", "json"])
        for cond in data.get("status", {}).get("conditions", []):
            if cond.get("type") == "Succeeded":
                return (
                    cond.get("status", "Unknown"),
                    (cond.get("reason") or "").strip(),
                    (cond.get("message") or "").strip(),
                )
        return "Unknown", "", ""

    def succeeded_condition(self, pr_name: str) -> tuple[str, str]:
        c, r, _ = self.succeeded_condition_detail(pr_name)
        return c, r

    @staticmethod
    def _is_resolver_couldnt_get_pipeline(reason: str, message: str) -> bool:
        r = (reason or "").strip()
        m = (message or "").lower()
        if r == "CouldntGetPipeline":
            return True
        return "couldntgetpipeline" in r.lower() or "resolver failed" in m or "file does not exist" in m

    @staticmethod
    def _coerce_snapshot_payload_to_spec(obj: Any) -> dict[str, Any] | None:
        """Normalize Konflux ``SNAPSHOT`` param JSON to a ``Snapshot.spec``-shaped dict."""
        if not isinstance(obj, dict):
            return None
        if isinstance(obj.get("application"), str) and isinstance(obj.get("components"), list):
            return obj
        spec = obj.get("spec")
        if isinstance(spec, dict) and isinstance(spec.get("application"), str) and isinstance(spec.get("components"), list):
            return spec
        return None

    @classmethod
    def _parse_snapshot_param_as_spec(cls, snap_value: str) -> dict[str, Any] | None:
        try:
            obj = json.loads(snap_value)
        except json.JSONDecodeError:
            return None
        return cls._coerce_snapshot_payload_to_spec(obj)

    def _raise_resolver_terminal(self, pr_name: str, reason: str, message: str) -> None:
        """Fail fast: Tekton never started tasks (CouldntGetPipeline / resolver)."""
        self._warn_couldnt_get_pipeline_git_source()
        excerpt = (message or reason or "").strip()
        if len(excerpt) > 500:
            excerpt = excerpt[:500] + "…"
        raise AppError(
            "PipelineRun failed before tasks started (pipeline definition could not be loaded). "
            f"``{pr_name}``: {excerpt}\n"
            f"Konflux: {self.konflux_ui}/ns/{self.args.namespace}/applications/{self.args.app}/pipelineruns/{pr_name}"
        )

    def _fail_fast_resolver_terminal(self, pr_name: str) -> None:
        """Raise if the run already failed on pipeline resolution (no tasks)."""
        cstat, reason, message = self.succeeded_condition_detail(pr_name)
        if cstat != "False" or not self._is_resolver_couldnt_get_pipeline(reason, message):
            return
        self._raise_resolver_terminal(pr_name, reason, message)

    def _warn_couldnt_get_pipeline_git_source(self) -> None:
        """Contextual hint after CouldntGetPipeline / missing pipeline file in Git resolver."""
        repo = (getattr(self.args, "konflux_repo", None) or "").strip()
        branch = (getattr(self.args, "konflux_branch", None) or "").strip()
        head = (
            "WARN Pipeline did not start: Git resolver could not load "
            "``integration-tests/olminstall/olminstall-pipeline.yaml`` (CouldntGetPipeline).\n"
        )
        if repo and branch:
            ns = self.args.namespace
            its_name = OLMINSTALL_TESTOPS_ITS_NAME
            print(
                f"{head}"
                f"  This run applied the ITS with ``--konflux-repo`` / ``--konflux-branch``: **{repo}** @ **{branch}**.\n"
                "  Tekton still could not open that path at that ref: confirm the branch is pushed, the file exists "
                "on GitHub at that ref, and ``oc get integrationtestscenario -n "
                f"{ns} {its_name} -o yaml`` shows the same ``resolverRef`` url/revision after "
                "``olm_pipeline.py`` applied it.\n"
                "  If the branch is correct but the file is missing, add ``olminstall-pipeline.yaml`` (or fix "
                "``pathInRepo`` in the ITS) on that branch.",
                file=sys.stderr,
            )
            return
        if repo and not branch:
            print(
                f"{head}"
                f"  ``--konflux-repo`` is set ({repo}) but ``--konflux-branch`` is not; the ITS pipeline revision "
                "may still be the template default (often **main**), so the resolver may not see your fork branch.\n"
                "  Pass ``--konflux-branch <ref>`` and trigger again so ``resolverRef`` matches the ref that contains this path.",
                file=sys.stderr,
            )
            return
        if branch and not repo:
            print(
                f"{head}"
                f"  ``--konflux-branch`` is set ({branch!r}) but ``--konflux-repo`` is not; the ITS URL may still be the "
                "template default. Pass ``--konflux-repo`` as well so ``resolverRef`` points at your fork.",
                file=sys.stderr,
            )
            return
        print(
            f"{head}"
            "  With no ``--konflux-repo`` / ``--konflux-branch`` on the CLI, the ITS keeps the committed default: "
            "**opendatahub-io/odh-konflux-central** @ **main** (see its-olminstall-*.yaml). That ref may not have this path.\n"
            "  Re-apply the ITS with a fork + branch, then trigger again, e.g.\n"
            "    python3 olm_pipeline.py --tests bvt \\\n"
            "      --konflux-repo https://github.com/<you>/odh-konflux-central.git \\\n"
            "      --konflux-branch <branch>",
            file=sys.stderr,
        )

    def _ka_get_json_warn_empty(self, path: str, *, ctx: str) -> dict[str, Any]:
        assert self.ka is not None
        try:
            raw = self.ka.get_json(path)
        except KubeArchiveAuthError as exc:
            print(f"WARN KubeArchive auth failed ({ctx}): {exc}", file=sys.stderr)
            return {}
        return raw if isinstance(raw, dict) else {}

    def _ka_get_text_warn_empty(self, path: str, *, ctx: str) -> str:
        assert self.ka is not None
        try:
            return self.ka.get_text(path)
        except KubeArchiveAuthError as exc:
            print(f"WARN KubeArchive auth failed ({ctx}): {exc}", file=sys.stderr)
            if not getattr(self, "_ka_archive_text_auth_tip_shown", False):
                self._ka_archive_text_auth_tip_shown = True
                print(
                    "TIP: Re-login (``oc login``) so the KubeArchive client gets a fresh token; "
                    "see README ``KA_HOST`` / ``--ka-host``.",
                    file=sys.stderr,
                )
            return ""

    def _merged_pipelinerun_rows(self, limit: int, *, name_substr: str | None) -> list[PipelineRow]:
        rows: list[PipelineRow] = []
        for item in self.get_pipelineruns(self.args.namespace):
            name = item.get("metadata", {}).get("name", "")
            app = item.get("metadata", {}).get("labels", {}).get("appstudio.openshift.io/application", "-")
            if app != self.args.app:
                continue
            if name_substr is not None and name_substr not in name:
                continue
            pipe = item.get("metadata", {}).get("labels", {}).get("tekton.dev/pipeline", "")
            if olminstall_smoke_only_pipelinerun(name, pipe):
                continue
            rows.append(
                PipelineRow(
                    name=name,
                    app=app,
                    state="completed" if item.get("status", {}).get("completionTime") else "running",
                    created=item.get("metadata", {}).get("creationTimestamp", ""),
                    source="live",
                )
            )
        rows.sort(key=lambda r: r.created, reverse=True)
        rows = rows[:limit]

        needed = limit - len(rows)
        if needed > 0 and self.ka_available():
            assert self.ka is not None
            ka_limit = needed + limit
            path = (
                f"/apis/tekton.dev/v1/namespaces/{quote(self.args.namespace)}/pipelineruns"
                f"?labelSelector={quote(f'appstudio.openshift.io/application={self.args.app}')}&limit={ka_limit}"
            )
            data = self._ka_get_json_warn_empty(path, ctx="list archived PipelineRuns")
            for item in data.get("items", []):
                name = item.get("metadata", {}).get("name", "")
                if name_substr is not None and name_substr not in name:
                    continue
                pipe = item.get("metadata", {}).get("labels", {}).get("tekton.dev/pipeline", "")
                if olminstall_smoke_only_pipelinerun(name, pipe):
                    continue
                cond = next((c for c in item.get("status", {}).get("conditions", []) if c.get("type") == "Succeeded"), {})
                status = cond.get("status")
                if status == "True":
                    state = "completed"
                elif status == "False":
                    state = "failed"
                elif status:
                    state = "running"
                else:
                    state = "unknown"
                rows.append(
                    PipelineRow(
                        name=name,
                        app=item.get("metadata", {}).get("labels", {}).get("appstudio.openshift.io/application", "-"),
                        state=state,
                        created=item.get("metadata", {}).get("creationTimestamp", ""),
                        source="archived",
                    )
                )

        merged: list[PipelineRow] = []
        seen: set[str] = set()
        for row in sorted(rows, key=lambda r: r.created, reverse=True):
            if row.name and row.name not in seen:
                merged.append(row)
                seen.add(row.name)
            if len(merged) >= limit:
                break
        return merged

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

    def _fetch_provision_cluster_supported_log(self, pr_name: str, source: str) -> str:
        out = ""
        if source == "live":
            out = self._fetch_step_log_live(pr_name, "provision-cluster", "step-get-supported-versions")
        if (not out or not out.strip()) and self.ka_available():
            archived = self._fetch_step_log_archived(pr_name, "provision-cluster", "step-get-supported-versions")
            if archived.strip():
                out = archived
        return out

    def _validate_ocp_version_in_supported_list(self, versions: list[str]) -> None:
        want = (self.args.ocp_version or "").strip()
        if not want:
            return
        if want in versions:
            print(f"\n--ocp-version {want!r} is in the supported list above.")
            return
        raise AppError(
            f"--ocp-version {want!r} is not in the EaaS-supported minors from this log snapshot: {versions}. "
            "Choose a minor from the list, or drop --list-supported-ocp to trigger a run without this check.",
            2,
        )

    def list_supported_ocp(self) -> None:
        merged = self._merged_pipelinerun_rows(LIST_SUPPORTED_OCP_MAX_PRS, name_substr="olminstall")
        print(
            f"EaaS-supported OpenShift minors (from get-supported-versions step logs), "
            f"app={self.args.app!r} namespace={self.args.namespace!r}, "
            f"scanning up to {LIST_SUPPORTED_OCP_MAX_PRS} newest olminstall PipelineRun(s):"
        )
        if not merged:
            print(f"No olminstall PipelineRuns found for app '{self.args.app}'.")
            print("Tip: use --app <name> or trigger a run; set --ka-host / KA_HOST if runs are archived off-cluster.")
            prj = run_cmd(["oc", "project", "-q"], capture=True, check=False)
            if prj.returncode == 0:
                current_ns = (prj.stdout or "").strip()
                if current_ns and current_ns != self.args.namespace:
                    print(
                        f"Tip: active oc project is '{current_ns}', but this command uses "
                        f"namespace '{self.args.namespace}'. Use -n/--namespace {current_ns} or: oc project {self.args.namespace}"
                    )
            raise AppError("No candidate PipelineRuns to scan", 1)

        for row in merged:
            log_text = self._fetch_provision_cluster_supported_log(row.name, row.source)
            versions = self._parse_supported_versions_line(log_text)
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
            "Could not read 'Supported versions:' from provision-cluster step-get-supported-versions logs "
            f"for any of {len(merged)} scanned run(s). "
            "The step may not have run yet, logs may be rotated, or the task name may differ — "
            "try --list-pipelines and watch a fresh run, or confirm KubeArchive (--ka-host).",
            1,
        )

    def _konflux_pipelinerun_url(self, application: str, pipelinerun_name: str) -> str:
        base = (self.konflux_ui or "").rstrip("/")
        if not base or not application or application == "-" or not pipelinerun_name:
            return ""
        return f"{base}/ns/{self.args.namespace}/applications/{application}/pipelineruns/{pipelinerun_name}"

    def list_pipelines(self) -> None:
        merged = self._merged_pipelinerun_rows(self.args.list_pipelines, name_substr=None)

        print(f"Latest {self.args.list_pipelines} PipelineRuns for app '{self.args.app}' in namespace '{self.args.namespace}':")
        if not merged:
            print(f"No PipelineRuns found for app '{self.args.app}'.")
            print("Tip: use --app <name> to target another application.")
            if self.ka is None:
                print(
                    "Tip: completed runs are often pruned from the cluster; set KA_HOST or --ka-host "
                    "(KubeArchive) to list archived PipelineRuns, or confirm oc context / namespace."
                )
            prj = run_cmd(["oc", "project", "-q"], capture=True, check=False)
            if prj.returncode == 0:
                current_ns = (prj.stdout or "").strip()
                if current_ns and current_ns != self.args.namespace:
                    print(
                        f"Tip: active oc project is '{current_ns}', but this command lists "
                        f"namespace '{self.args.namespace}' (not your current project). "
                        f"Use -n/--namespace {current_ns} or switch: oc project {self.args.namespace}"
                    )
            return
        print("NAME\tAPP\tSTATE\tCREATED\tSOURCE\tLINK")
        for r in merged:
            link = self._konflux_pipelinerun_url(r.app, r.name) or "-"
            print(f"{r.name}\t{r.app}\t{r.state}\t{r.created}\t{r.source}\t{link}")

    def get_snapshot_owner(self, snap: str) -> str:
        if not snap:
            return ""
        return get_jsonpath(
            [
                "oc",
                "get",
                "snapshot",
                snap,
                "-n",
                self.args.namespace,
                "-o",
                "jsonpath={.metadata.annotations.olminstall\\.run-owner}",
            ]
        )

    def _pick_newest_owned_pipelinerun(self, rows: list[tuple[str, str, str, str, str]]) -> str:
        """Pick newest PipelineRun name from rows (creation_ts, name, snapshot, run-owner, pipeline label)."""
        filtered = [r for r in rows if not olminstall_smoke_only_pipelinerun(r[1], r[4])]
        owned = [
            row
            for row in filtered
            if row[3] == self.run_owner
            or (
                _snapshot_param_is_resource_name(row[2])
                and self.get_snapshot_owner(row[2]) == self.run_owner
            )
        ]
        if not owned:
            return ""
        owned.sort(key=lambda x: x[0], reverse=True)
        return owned[0][1]

    def find_owned_live_watch_pr(self) -> str:
        items = self.get_pipelineruns(self.args.namespace)
        cands: list[tuple[str, str, str, str, str]] = []
        for item in items:
            name = item.get("metadata", {}).get("name", "")
            app = item.get("metadata", {}).get("labels", {}).get("appstudio.openshift.io/application", "")
            if "olminstall" not in name or app != self.args.app:
                continue
            snap = ""
            for p in item.get("spec", {}).get("params", []):
                if p.get("name") == "SNAPSHOT":
                    snap = str(p.get("value", ""))
                    break
            owner = item.get("metadata", {}).get("annotations", {}).get("olminstall.run-owner", "")
            pipe = item.get("metadata", {}).get("labels", {}).get("tekton.dev/pipeline", "")
            cands.append((item.get("metadata", {}).get("creationTimestamp", ""), name, snap, owner, pipe))
        return self._pick_newest_owned_pipelinerun(cands)

    def find_owned_archived_watch_pr(self) -> str:
        if not self.ka_available():
            return ""
        assert self.ka is not None
        # Match find_owned_live_watch_pr + _pick_newest_owned_pipelinerun: ownership may live only on
        # the Snapshot (olminstall.run-owner), while the archived PipelineRun annotation is unset or stale.
        path = f"/apis/tekton.dev/v1/namespaces/{quote(self.args.namespace)}/pipelineruns?labelSelector={quote(f'appstudio.openshift.io/application={self.args.app}')}"
        items = self._ka_get_json_warn_empty(path, ctx="archived PipelineRuns for watch").get("items", [])
        rows: list[tuple[str, str, str, str, str]] = []
        for item in items:
            name = item.get("metadata", {}).get("name", "")
            if "olminstall" not in name:
                continue
            snap = ""
            for p in item.get("spec", {}).get("params", []):
                if p.get("name") == "SNAPSHOT":
                    snap = str(p.get("value", ""))
                    break
            owner = item.get("metadata", {}).get("annotations", {}).get("olminstall.run-owner", "")
            pipe = item.get("metadata", {}).get("labels", {}).get("tekton.dev/pipeline", "")
            rows.append((item.get("metadata", {}).get("creationTimestamp", ""), name, snap, owner, pipe))
        return self._pick_newest_owned_pipelinerun(rows)

    def _pipelinerun_resolver_unusable_for_logs(self, name: str, source: str) -> bool:
        """True when Tekton never resolved the Pipeline (e.g. CouldntGetPipeline) — nothing useful to replay."""
        prj: dict[str, Any] = {}
        if source == "live":
            proc = run_cmd(
                ["oc", "get", "pipelinerun", name, "-n", self.args.namespace, "-o", "json"],
                capture=True,
                check=False,
            )
            if proc.returncode != 0:
                return False
            try:
                prj = json.loads(proc.stdout or "{}")
            except json.JSONDecodeError:
                return False
        elif self.ka_available():
            prj = self._ka_get_json_warn_empty(
                f"/apis/tekton.dev/v1/namespaces/{quote(self.args.namespace)}/pipelineruns/{quote(name)}",
                ctx="pipelinerun resolver check (archived)",
            )
        if not isinstance(prj, dict) or not prj.get("metadata", {}).get("name"):
            return False
        for cond in prj.get("status", {}).get("conditions", []) or []:
            if cond.get("type") != "Succeeded":
                continue
            if cond.get("status") != "False":
                continue
            reason = (cond.get("reason") or "").strip()
            if reason == "CouldntGetPipeline":
                return True
            msg = (cond.get("message") or "").lower()
            if "couldntgetpipeline" in msg or "resolver failed" in msg:
                return True
        return False

    def find_newest_olminstall_any_owner_for_watch(self) -> str:
        """Newest non-smoke olminstall PipelineRun for ``--app`` (any owner), same merge order as ``--list``."""
        scan = max(DEFAULT_LIST_COUNT, LIST_SUPPORTED_OCP_MAX_PRS)
        merged = self._merged_pipelinerun_rows(scan, name_substr="olminstall")
        fallback = ""
        for row in merged:
            if "olminstall" not in row.name:
                continue
            if olminstall_smoke_only_pipelinerun(row.name, ""):
                continue
            if not fallback:
                fallback = row.name
            if self._pipelinerun_resolver_unusable_for_logs(row.name, row.source):
                continue
            return row.name
        return fallback

    def get_applications(self, namespace: str) -> list[str]:
        data = parse_json_output(["oc", "get", "applications", "-n", namespace, "-o", "json"])
        return [item.get("metadata", {}).get("name", "") for item in data.get("items", []) if item.get("metadata", {}).get("name")]

    def latest_matching_image(self, namespace: str, app_name: str, pattern: str) -> tuple[str, str]:
        """Return (creationTimestamp, containerImage) for the newest Snapshot whose components match ``pattern``.

        Avoids ``oc get … -o json`` for the full Snapshot list (can be tens of MB for busy apps), which can
        make ``json.loads`` slow or brittle; use a compact sorted list then small per-Snapshot reads.
        """
        proc = run_cmd(
            [
                "oc",
                "get",
                "snapshots",
                "-n",
                namespace,
                "-l",
                f"appstudio.openshift.io/application={app_name}",
                "--sort-by=.metadata.creationTimestamp",
                "-o",
                "custom-columns=NAME:.metadata.name,TS:.metadata.creationTimestamp",
            ],
            capture=True,
            check=False,
            timeout=120,
        )
        if proc.returncode != 0 or not (proc.stdout or "").strip():
            return "", ""
        rows: list[tuple[str, str]] = []
        for line in (proc.stdout or "").splitlines():
            ln = line.strip()
            if not ln or ln.upper().startswith("NAME "):
                continue
            parts = ln.split()
            if len(parts) < 2:
                continue
            ts = parts[-1]
            name = parts[0]
            if name and ts and re.match(r"^\d{4}-\d{2}-\d{2}T", ts):
                rows.append((ts, name))
        if (proc.stdout or "").strip() and not rows:
            print(
                f"WARN Snapshot list column parse skipped non-ISO timestamps for app={app_name!r}; "
                "using slow path or empty match.",
                file=sys.stderr,
            )
        if not rows:
            return "", ""
        # Only walk the newest N snapshots per app; each oc get is small but hundreds add up.
        max_walk = 120
        scan = rows[-max_walk:] if len(rows) > max_walk else rows
        for ts, snap_name in reversed(scan):
            proc2 = run_cmd(
                [
                    "oc",
                    "get",
                    "snapshot",
                    snap_name,
                    "-n",
                    namespace,
                    "-o",
                    "jsonpath={range .spec.components[*]}{.containerImage}{'\\n'}{end}",
                ],
                capture=True,
                check=False,
                timeout=60,
            )
            if proc2.returncode != 0:
                continue
            for raw in (proc2.stdout or "").splitlines():
                img = raw.strip()
                if img and re.search(pattern, img):
                    return ts, img
        if len(rows) <= max_walk:
            return "", ""
        print(
            f"WARN No component matched {pattern!r} in the newest {max_walk} snapshots for {app_name}; "
            "falling back to full Snapshot list (may be slow).",
            file=sys.stderr,
        )
        proc_big = run_cmd(
            ["oc", "get", "snapshots", "-n", namespace, "-l", f"appstudio.openshift.io/application={app_name}", "-o", "json"],
            capture=True,
            check=False,
            timeout=300,
        )
        data: dict[str, Any] = {}
        if proc_big.returncode == 0 and (proc_big.stdout or "").strip():
            try:
                data = json.loads(proc_big.stdout)
            except json.JSONDecodeError:
                data = {}
        best_ts = ""
        best_img = ""
        for item in data.get("items", []):
            ts = item.get("metadata", {}).get("creationTimestamp", "")
            for comp in item.get("spec", {}).get("components", []):
                img = comp.get("containerImage", "")
                if re.search(pattern, img):
                    if ts > best_ts:
                        best_ts = ts
                        best_img = img
        return best_ts, best_img

    def resolve_image(self, odh_overrides: bool) -> None:
        if self.image:
            print(f"Using provided image: {self.image}")
            return

        if self.args.product == "none":
            print(
                "INFO product=none: skipping rhoai/odh FBC/catalog auto-resolution; "
                "Snapshot uses containerImage from test-snapshot.yaml unless --image is set."
            )
            return

        if self.args.product == "rhoai" and self.args.version:
            prefix = f"rhoai-v{self.args.version.replace('.', '-')}"
            with spin_while(
                f"Resolving latest FBCF image for RHOAI {self.args.version} (apps matching {prefix}*)"
            ):
                apps = [a for a in self.get_applications("rhoai-tenant") if re.match(rf"^{re.escape(prefix)}(-|$)", a)]
                if not apps:
                    raise AppError(f"No Konflux application found matching {prefix}* in rhoai-tenant")
                best_ts = ""
                for app in apps:
                    ts, img = self.latest_matching_image("rhoai-tenant", app, RHOAI_FBCF_IMAGE_REF_PATTERN)
                    if img and ts > best_ts:
                        best_ts = ts
                        self.image = img
                        self.resolved_app = app
                if not self.image:
                    apps_s = ", ".join(sorted(apps))
                    raise AppError(
                        f"No FBCF snapshot found for RHOAI {self.args.version} "
                        f"(apps tried: {apps_s}; image must match {RHOAI_FBCF_IMAGE_REF_PATTERN!r}). "
                        "If Snapshots only list another image name, use --image <ref> or check Konflux application naming."
                    )
            print(f"RHOAI {self.args.version} FBCF image: {self.image} (from {self.resolved_app})")
        elif self.args.product == "rhoai":
            with spin_while("Fetching latest FBCF image across all RHOAI apps (highest version)"):
                apps = [a for a in self.get_applications("rhoai-tenant") if a.startswith("rhoai-v")]
                best_ts = ""
                for app in apps:
                    ts, img = self.latest_matching_image("rhoai-tenant", app, RHOAI_FBCF_IMAGE_REF_PATTERN)
                    if img and ts > best_ts:
                        best_ts = ts
                        self.image = img
                        self.resolved_app = app
            if self.image:
                print(f"Latest FBCF image: {self.image} (from {self.resolved_app})")
            else:
                print("WARN Could not fetch latest image - falling back to pinned image in test-snapshot.yaml")
        elif self.args.product == "odh":
            repo = "quay.io/opendatahub/opendatahub-operator-catalog"
            tag = "odh-stable"
            with spin_while("Fetching latest ODH catalog snapshot from open-data-hub-tenant"):
                data = parse_json_output(["oc", "get", "snapshots", "-n", "open-data-hub-tenant", "-o", "json"])
                best_ts = ""
                for item in data.get("items", []):
                    if item.get("metadata", {}).get("labels", {}).get("appstudio.openshift.io/application") != "opendatahub-builds":
                        continue
                    ts = item.get("metadata", {}).get("creationTimestamp", "")
                    for comp in item.get("spec", {}).get("components", []):
                        img = comp.get("containerImage", "")
                        if re.search(r"opendatahub-operator-catalog@|odh-operator-catalog@", img) and ts > best_ts:
                            best_ts = ts
                            self.image = img
            if not self.image:
                print("  No snapshots found (likely no access to open-data-hub-tenant)")
                print(f"  Resolving from {repo}:{tag} via skopeo...")
                if shutil.which("skopeo"):
                    out = parse_json_output(["skopeo", "inspect", "--no-tags", f"docker://{repo}:{tag}"])
                    digest = out.get("Digest", "")
                    if digest:
                        self.image = f"{repo}@{digest}"
                if not self.image:
                    print("  skopeo unavailable or inspect failed - using tag reference")
                    self.image = f"{repo}:{tag}"
            print(f"Latest ODH catalog image: {self.image}")

        if not self.update_channel_override and self.args.product == "odh":
            self.update_channel_override = "odh-stable"
            print(f"Auto-selected channel: {self.update_channel_override} (product={self.args.product})")
        elif (
            not self.update_channel_override
            and self.resolved_app
            and self.resolved_app.startswith("rhoai-v3-")
        ):
            self.update_channel_override = "stable-3.x"
            print(f"Auto-selected channel: {self.update_channel_override} (from {self.resolved_app})")

    def prune_stale_integration_test_scenarios(self) -> None:
        """Remove legacy ITS objects so one Snapshot does not fan out to smoke / rhoai-test pipelines."""
        if not getattr(self.args, "prune_stale_its", True):
            return
        if self.args.namespace != DEFAULT_NAMESPACE or self.args.app != DEFAULT_APP:
            return
        stale = sorted(STALE_TESTOPS_PLAYPEN_ITS_NAMES)
        if not stale:
            return
        print(
            "Pruning legacy IntegrationTestScenario objects so the next Snapshot only starts "
            f"{OLMINSTALL_TESTOPS_ITS_NAME!r} for application {self.args.app!r}: "
            + ", ".join(stale)
        )
        for name in stale:
            proc = run_cmd(
                ["oc", "delete", "integrationtestscenario", name, "-n", self.args.namespace, "--ignore-not-found"],
                capture=True,
                check=False,
            )
            if proc.returncode != 0:
                msg = filter_warning_lines(f"{proc.stdout}\n{proc.stderr}").strip()
                print(f"  WARN oc delete integrationtestscenario/{name}: {msg or proc.returncode}", file=sys.stderr)

    def ensure_its_applied(self, odh_overrides: bool) -> None:
        print("Ensuring IntegrationTestScenario is applied...")
        tests_baseline = getattr(self.args, "tests_catalog_default_csv", ITS_TESTS_PARAM_DEFAULT)
        if not shutil.which("yq"):
            raise AppError(
                "yq is required to patch the ITS (PRODUCT, --konflux-repo, --channel, etc.)."
            )

        tmp = tempfile.NamedTemporaryFile(delete=False)
        tmp.close()
        self.its_apply_tmp = tmp.name
        del_names: list[str] = ["PRODUCT"]
        if self.args.konflux_repo:
            del_names.append("SCRIPTS_REPO_URL")
        if self.args.konflux_branch:
            del_names.append("SCRIPTS_REPO_REVISION")
        if self.update_channel_override:
            del_names.append("UPDATE_CHANNEL")
        if self.args.ocp_version:
            del_names.append("OCP_VERSION_PREFIX")
        if odh_overrides:
            del_names.extend(["OPERATOR_NAME", "OPERATOR_NAMESPACE", "FBCF_COMPONENT_NAME"])
        if self._tests_its_override():
            del_names.append("TESTS")
        if (self.args.slack_channel_id or "").strip():
            del_names.append("SLACK_CHANNEL_ID")

        expr = " or ".join(f'.name == "{n}"' for n in del_names)
        proc = run_cmd(["yq", "e", f"del(.spec.params[] | select({expr}))", str(self.its_file)], capture=True, check=True)
        Path(self.its_apply_tmp).write_text(proc.stdout, encoding="utf-8")

        run_cmd(
            ["yq", "e", '.spec.params += [{"name":"PRODUCT","value":strenv(YQ_PRODUCT)}]', "-i", self.its_apply_tmp],
            capture=True,
            check=True,
            env={**os.environ, "YQ_PRODUCT": self.args.product},
        )
        if self.args.konflux_repo:
            run_cmd(
                ["yq", "e", '.spec.params += [{"name":"SCRIPTS_REPO_URL","value":strenv(YQ_SCRIPTS_URL)}]', "-i", self.its_apply_tmp],
                capture=True,
                check=True,
                env={**os.environ, "YQ_SCRIPTS_URL": self.args.konflux_repo},
            )
            run_cmd(
                ["yq", "e", '(.spec.resolverRef.params[] | select(.name == "url")).value = strenv(YQ_RESOLVER_URL)', "-i", self.its_apply_tmp],
                capture=True,
                check=True,
                env={**os.environ, "YQ_RESOLVER_URL": self.args.konflux_repo},
            )
        if self.args.konflux_branch:
            run_cmd(
                ["yq", "e", '.spec.params += [{"name":"SCRIPTS_REPO_REVISION","value":strenv(YQ_SCRIPTS_REV)}]', "-i", self.its_apply_tmp],
                capture=True,
                check=True,
                env={**os.environ, "YQ_SCRIPTS_REV": self.args.konflux_branch},
            )
            run_cmd(
                ["yq", "e", '(.spec.resolverRef.params[] | select(.name == "revision")).value = strenv(YQ_RESOLVER_REV)', "-i", self.its_apply_tmp],
                capture=True,
                check=True,
                env={**os.environ, "YQ_RESOLVER_REV": self.args.konflux_branch},
            )
        if self.update_channel_override:
            run_cmd(
                ["yq", "e", '.spec.params += [{"name":"UPDATE_CHANNEL","value":strenv(YQ_UPDATE_CHANNEL)}]', "-i", self.its_apply_tmp],
                capture=True,
                check=True,
                env={**os.environ, "YQ_UPDATE_CHANNEL": self.update_channel_override},
            )
        if self.args.ocp_version:
            run_cmd(
                ["yq", "e", '.spec.params += [{"name":"OCP_VERSION_PREFIX","value":strenv(YQ_OCP_PREFIX)}]', "-i", self.its_apply_tmp],
                capture=True,
                check=True,
                env={**os.environ, "YQ_OCP_PREFIX": self.args.ocp_version},
            )
        if odh_overrides:
            # Jenkins odhTestConfigOperator: odh-stable Konflux catalog uses rhods-operator + RHOAI namespaces.
            run_cmd(["yq", "e", '.spec.params += [{"name":"OPERATOR_NAME","value":"rhods-operator"}]', "-i", self.its_apply_tmp], capture=True, check=True)
            run_cmd(["yq", "e", '.spec.params += [{"name":"OPERATOR_NAMESPACE","value":"redhat-ods-operator"}]', "-i", self.its_apply_tmp], capture=True, check=True)
            run_cmd(["yq", "e", '.spec.params += [{"name":"FBCF_COMPONENT_NAME","value":"odh-operator-catalog"}]', "-i", self.its_apply_tmp], capture=True, check=True)
        if self._tests_its_override():
            run_cmd(
                ["yq", "e", '.spec.params += [{"name":"TESTS","value":strenv(YQ_TESTS)}]', "-i", self.its_apply_tmp],
                capture=True,
                check=True,
                env={**os.environ, "YQ_TESTS": self.args.tests},
            )
        slack_channel = (self.args.slack_channel_id or "").strip()
        if slack_channel:
            run_cmd(
                ["yq", "e", '.spec.params += [{"name":"SLACK_CHANNEL_ID","value":strenv(YQ_SLACK_CHANNEL)}]', "-i", self.its_apply_tmp],
                capture=True,
                check=True,
                env={**os.environ, "YQ_SLACK_CHANNEL": slack_channel},
            )

        print(
            "  ITS overrides:"
            f" resolverRef={self.args.konflux_repo or '<default>'}@{self.args.konflux_branch or '<default>'}"
            f" SCRIPTS_REPO={self.args.konflux_repo or '<default>'}@{self.args.konflux_branch or '<default>'}"
            f" UPDATE_CHANNEL={self.update_channel_override or '<pipeline default>'}"
            f" OCP_VERSION_PREFIX={self.args.ocp_version or '<pipeline default>'}"
            f" TESTS={self.args.tests if self.args.tests != tests_baseline else '<ITS default>'}"
            f" SLACK_CHANNEL_ID={slack_channel or '<disabled>'}"
            f" PRODUCT={self.args.product}"
        )
        proc = run_cmd(["oc", "apply", "-n", self.args.namespace, "-f", self.its_apply_tmp], capture=True, check=False)
        filtered = filter_warning_lines(f"{proc.stdout}\n{proc.stderr}")
        if filtered.strip():
            print(filtered, file=sys.stderr)
        if proc.returncode != 0:
            raise AppError("ITS apply failed")
        print("ITS ready")

    def create_snapshot(self, odh_overrides: bool) -> None:
        snap_yaml = self.snapshot_file.read_text(encoding="utf-8")
        snap_yaml = re.sub(
            r"(^\s*application:\s*).*$",
            lambda m: m.group(1) + self.args.app,
            snap_yaml,
            flags=re.MULTILINE,
        )
        if self.image:
            snap_yaml = re.sub(
                r"(^\s*containerImage:\s*).*$",
                lambda m: m.group(1) + self.image,
                snap_yaml,
                flags=re.MULTILINE,
            )
        if odh_overrides:
            tpl_comp = first_snapshot_component_name(snap_yaml)
            snap_yaml = snap_yaml.replace(f"name: {tpl_comp}", "name: odh-operator-catalog", 1)
        with spin_while(f"Creating Snapshot to trigger pipeline (app: {self.args.app})"):
            proc = run_cmd(
                ["oc", "create", "-n", self.args.namespace, "-f", "-", "-o", "jsonpath={.metadata.name}"],
                capture=True,
                check=True,
                input_text=snap_yaml,
            )
            self.snapshot_name = proc.stdout.strip()
            snap_ann = [
                "oc",
                "annotate",
                "snapshot",
                self.snapshot_name,
                "-n",
                self.args.namespace,
                f"olminstall.run-owner={self.run_owner}",
                *self.olminstall_context_annotate_argv(),
                "--overwrite",
            ]
            self._oc_annotate_required(snap_ann, f"snapshot/{self.snapshot_name}")
            snap_rec = parse_json_output(
                ["oc", "get", "snapshot", self.snapshot_name, "-n", self.args.namespace, "-o", "json"]
            )
            self._trigger_snapshot_spec = snap_rec.get("spec") if isinstance(snap_rec, dict) else None
            self._trigger_snapshot_created_ts = (
                (snap_rec.get("metadata") or {}).get("creationTimestamp", "") if isinstance(snap_rec, dict) else ""
            )
        print(f"Snapshot: {self.snapshot_name}")
        print(f"  Snapshot owner marker: {self.run_owner}")
        if not self._trigger_snapshot_spec:
            print(
                "WARN Could not read Snapshot ``spec`` after create; matching PipelineRuns whose ``SNAPSHOT`` "
                "param is JSON (not the snapshot name string) may not find this run until the Snapshot is visible.",
                file=sys.stderr,
            )

    def snapshot_matches_trigger(self, snap_value: str) -> bool:
        """True if this PipelineRun ``SNAPSHOT`` param is the Snapshot we created for this trigger.

        When ``_trigger_snapshot_spec`` is set (normal after ``create_snapshot``), comparison is strict
        full-spec equality. The image/substring heuristic runs only if that spec was not captured.
        """
        if not self.snapshot_name:
            return False
        if snap_value == self.snapshot_name:
            return True
        par = self._parse_snapshot_param_as_spec(snap_value)
        if par is None:
            return False
        if par.get("application") != self.args.app:
            return False
        ref = self._trigger_snapshot_spec
        if ref:
            return par.get("application") == ref.get("application") and par.get("components") == ref.get("components")
        if self.image:
            components = par.get("components") or []
            return any((c or {}).get("containerImage") == self.image for c in components)
        return False

    def _poll_pipelinerun_for_snapshot(self, snap_created: str) -> bool:
        """Return True when a matching olminstall PipelineRun name is stored in ``self.pr``."""
        items = self.get_pipelineruns(self.args.namespace)
        cands: list[tuple[str, str]] = []
        for item in items:
            name = item.get("metadata", {}).get("name", "")
            app = item.get("metadata", {}).get("labels", {}).get("appstudio.openshift.io/application", "")
            if "olminstall" not in name or app != self.args.app:
                continue
            snap = ""
            for p in item.get("spec", {}).get("params", []):
                if p.get("name") == "SNAPSHOT":
                    snap = str(p.get("value", ""))
                    break
            if not self.snapshot_matches_trigger(snap):
                continue
            pr_created = (item.get("metadata", {}).get("creationTimestamp", "") or "").strip()
            if snap_created and pr_created and pr_created < snap_created:
                continue
            self._fail_fast_resolver_terminal(name)
            cands.append((item.get("metadata", {}).get("creationTimestamp", ""), name))
        if not cands:
            return False
        cands.sort()
        self.pr = cands[-1][1]
        return True

    def wait_for_pipelinerun(self) -> None:
        attempts = max(1, (self.pr_appear_timeout + 4) // 5)
        msg_prefix = f"Waiting for PipelineRun to start (snapshot: {self.snapshot_name})"
        snap_created = (self._trigger_snapshot_created_ts or "").strip()
        with spin_while(msg_prefix):
            for attempt in range(1, attempts + 1):
                if self._poll_pipelinerun_for_snapshot(snap_created):
                    return
                if attempt < attempts:
                    time.sleep(5)
        if not self.pr:
            self.cleanup_snapshot_on_exit = False
            watch_hint = format_olm_pipeline_watch_cli(
                olminstall_dir=self.script_dir,
                namespace=self.args.namespace,
                app=self.args.app,
                pipelinerun="",
            )
            raise AppError(
                f"PipelineRun did not appear after {self.pr_appear_timeout}s. Check Konflux:\n"
                f"{self.konflux_ui}/ns/{self.args.namespace}/applications/{self.args.app}/activity/pipelineruns\n"
                f"When the run appears, follow logs with:\n  {watch_hint}"
            )

        pr_ann = [
            "oc",
            "annotate",
            "pipelinerun",
            self.pr,
            "-n",
            self.args.namespace,
            f"olminstall.run-owner={self.run_owner}",
            *self.olminstall_context_annotate_argv(),
            *self.early_summary_annotate_argv(),
            "--overwrite",
        ]
        self._oc_annotate_required(pr_ann, f"pipelinerun/{self.pr}")

    def _oc_annotate_required(self, cmd: list[str], resource: str) -> None:
        proc = run_cmd(cmd, capture=True, check=False)
        if proc.returncode == 0:
            return
        detail = filter_warning_lines(f"{proc.stdout or ''}\n{proc.stderr or ''}").strip()
        raise AppError(
            f"Failed to annotate {resource} in namespace {self.args.namespace}: "
            f"{detail or f'oc exited {proc.returncode}'}"
        )

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
            print(f"Watch mode: newest non-smoke olminstall PipelineRun for app '{self.args.app}' (same merge order as --list)…")
            self.pr = self.find_newest_olminstall_any_owner_for_watch()
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
                print(f"  No olminstall run in --list window; trying run-owner / Snapshot match for {self.run_owner!r}…")
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
                    f"No olminstall PipelineRun found for app '{self.args.app}' (live or archived).\n"
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

    def _infer_konflux_git_pipeline_ref_from_env_only(self) -> tuple[str, str]:
        """Optional (repo, revision) from env — both required; otherwise ITS keeps opendatahub-io @ ``main``."""
        env_url = (os.environ.get("OLMINSTALL_PIPELINE_REPO") or os.environ.get("KONFLUX_PIPELINE_REPO") or "").strip()
        env_rev = (os.environ.get("OLMINSTALL_PIPELINE_REVISION") or os.environ.get("KONFLUX_PIPELINE_REVISION") or "").strip()
        if env_url and env_rev:
            return env_url, env_rev
        return "", ""

    def _apply_konflux_git_inference_from_clone_or_env(self) -> None:
        """Apply ``--konflux-repo`` / ``--konflux-branch`` only from env when both are set there.

        With no CLI flags and no env pair, the committed ITS default applies: opendatahub-io/odh-konflux-central @ main.
        """
        if self.args.konflux_repo or self.args.konflux_branch:
            return
        before_repo, before_branch = self.args.konflux_repo, self.args.konflux_branch
        url, rev = self._infer_konflux_git_pipeline_ref_from_env_only()
        if url:
            self.args.konflux_repo = url
        if rev:
            self.args.konflux_branch = rev
        if self.args.konflux_repo != before_repo or self.args.konflux_branch != before_branch:
            print(
                "INFO Konflux pipeline Git resolver from OLMINSTALL_PIPELINE_* / KONFLUX_PIPELINE_* "
                f"(override with --konflux-repo / --konflux-branch): {self.args.konflux_repo} @ {self.args.konflux_branch}",
                file=sys.stderr,
            )

    def run_trigger_mode(self) -> None:
        self._apply_konflux_git_inference_from_clone_or_env()
        rows: list[tuple[str, str, str, str, str]] = []
        for item in self.get_pipelineruns(self.args.namespace):
            name = item.get("metadata", {}).get("name", "")
            app = item.get("metadata", {}).get("labels", {}).get("appstudio.openshift.io/application", "")
            if "olminstall" not in name or app != self.args.app:
                continue
            if item.get("status", {}).get("completionTime"):
                continue
            snap = ""
            for p in item.get("spec", {}).get("params", []):
                if p.get("name") == "SNAPSHOT":
                    snap = str(p.get("value", ""))
                    break
            owner = item.get("metadata", {}).get("annotations", {}).get("olminstall.run-owner", "")
            pipe = item.get("metadata", {}).get("labels", {}).get("tekton.dev/pipeline", "")
            rows.append((item.get("metadata", {}).get("creationTimestamp", ""), name, snap, owner, pipe))
        rows.sort(key=lambda x: x[0], reverse=True)
        owned_running = self._pick_newest_owned_pipelinerun(rows)
        if owned_running:
            watch_owned = format_olm_pipeline_watch_cli(
                olminstall_dir=self.script_dir,
                namespace=self.args.namespace,
                app=self.args.app,
                pipelinerun=owned_running,
            )
            print(
                f"INFO Owned olminstall PipelineRun still running: {owned_running}. "
                "Trigger mode starts a new run (your flags apply to the new run only). "
                f"To stream the existing run instead:\n  {watch_owned}"
            )
        elif rows:
            print(
                f"WARN Found active PipelineRun(s) for app '{self.args.app}' without a matching owner marker; "
                "triggering a new run."
            )
        if self.args.konflux_repo and not self.args.konflux_branch:
            print(
                "WARN --konflux-repo is set without --konflux-branch; the ITS resolver revision stays the "
                "YAML default (``main``). Pass --konflux-branch <ref> to use your fork branch.",
                file=sys.stderr,
            )
        odh_overrides = self.args.product == "odh"
        self.resolve_image(odh_overrides)
        self.prune_stale_integration_test_scenarios()
        self.ensure_its_applied(odh_overrides)
        self.create_snapshot(odh_overrides)
        self.wait_for_pipelinerun()

    def _archived_pipelinerun_task_refs(self, prj: dict[str, Any], pr_name: str) -> list[tuple[str, str]]:
        """Resolve TaskRun name + pipeline task label from archived PR JSON.

        KubeArchive sometimes stores a PipelineRun without ``status.childReferences``;
        fall back to listing TaskRuns by ``tekton.dev/pipelineRun`` (same as live ``oc`` path).
        """
        refs = prj.get("status", {}).get("childReferences", [])
        out: list[tuple[str, str]] = []
        for ref in refs:
            tr_name = ref.get("name", "") or ""
            task_name = ref.get("pipelineTaskName", "") or tr_name
            if tr_name:
                out.append((tr_name, task_name))
        if out:
            return out
        sel = f"tekton.dev/pipelineRun={pr_name}"
        data = self._ka_get_json_warn_empty(
            f"/apis/tekton.dev/v1/namespaces/{quote(self.args.namespace)}/taskruns?labelSelector={quote(sel)}",
            ctx="list archived TaskRuns for PipelineRun",
        )
        for item in data.get("items", []):
            md = item.get("metadata", {}) or {}
            tr_name = md.get("name", "") or ""
            labels = md.get("labels", {}) or {}
            task_name = labels.get("tekton.dev/pipelineTask", "") or tr_name
            if tr_name:
                out.append((tr_name, task_name))
        return out

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
            pods = self._ka_get_json_warn_empty(
                f"/api/v1/namespaces/{quote(self.args.namespace)}/pods?labelSelector={quote(f'tekton.dev/taskRun={tr_name}')}",
                ctx=f"replay archived pods for task {task_name}",
            )
            items = pods.get("items", [])
            if not items:
                print(f"[{task_name}] (no pod found)")
                continue
            pod = items[0].get("metadata", {}).get("name", "")
            pod_obj = self._ka_get_json_warn_empty(
                f"/api/v1/namespaces/{quote(self.args.namespace)}/pods/{quote(pod)}",
                ctx=f"replay archived pod {pod}",
            )
            containers = [c.get("name", "") for c in pod_obj.get("spec", {}).get("initContainers", [])] + [
                c.get("name", "") for c in pod_obj.get("spec", {}).get("containers", [])
            ]
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
                print(log_text, end="")
                if log_text and not log_text.endswith("\n"):
                    print(flush=True)
                print(f"— KubeArchive: {log_elapsed:.1f}s, {len(log_text)} chars", flush=True)

    def _replay_archived_logs_to_log_file(self) -> None:
        tmp_log = tempfile.NamedTemporaryFile(prefix="olminstall-run.", delete=False)
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
        tmp_log = tempfile.NamedTemporaryFile(prefix="olminstall-run.", delete=False)
        self.log_file = tmp_log.name
        tmp_log.close()
        tkn_bin = shutil.which("tkn")
        pr_name = (self.pr or "").strip()
        if not pr_name:
            print("WARN No PipelineRun name set; skipping tkn log stream.", file=sys.stderr)
            return False
        watch_hint = format_olm_pipeline_watch_cli(
            olminstall_dir=self.script_dir,
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
                    proc = subprocess.run(
                        cmd,
                        stdout=subprocess.PIPE,
                        stderr=subprocess.STDOUT,
                        text=True,
                        encoding="utf-8",
                        errors="replace",
                        check=False,
                    )
                    last_rc = proc.returncode
                    lines = (proc.stdout or "").splitlines()
                    if proc.returncode == 0 and lines:
                        break
                if len(lines) > 200:
                    lines = lines[-200:]
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
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                )
                assert p.stdout is not None
                detached = False
                try:
                    for line in p.stdout:
                        out = _format_live_tkn_log_line(line)
                        if out is None:
                            continue
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
                if detached:
                    return True
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

    def run(self) -> int:
        self.check_login()

        if self.args.list_supported_ocp:
            self.list_supported_ocp()
            return 0

        if self.args.list_pipelines:
            self.list_pipelines()
            return 0

        self.ensure_konflux_cluster()

        if self.args.watch_mode:
            self.run_watch_mode()
        else:
            self.run_trigger_mode()

        self.print_run_summary(self._status_label_for_summary_preview(), phase="preview")

        if self.watch_from_archive:
            self._replay_archived_logs_to_log_file()
            self.print_run_summary(self.ka_succeeded, phase="final")
            if self.ka_succeeded == "Failed":
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
        if final_cstat == "False" and self._is_resolver_couldnt_get_pipeline(final_reason, final_msg):
            self._warn_couldnt_get_pipeline_git_source()
        self.print_run_summary(self._terminal_status_label(), phase="final")
        if final_cstat != "True":
            self.pipeline_exit = 1
        return self.pipeline_exit


class Tee:
    def __init__(self, *streams: Any) -> None:
        self.streams = streams

    def write(self, data: str) -> None:
        for s in self.streams:
            s.write(data)

    def flush(self) -> None:
        for s in self.streams:
            s.flush()
