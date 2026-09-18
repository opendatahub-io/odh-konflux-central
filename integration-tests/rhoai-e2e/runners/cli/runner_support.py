"""Shared utilities for rhoai-e2e runner (log display, PipelineRow, Tee)."""

from __future__ import annotations

import contextlib
import os
import re
import shlex
import shutil
import sys
import threading
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, TextIO

from suite.constants import PENDING_REASONS, rhoai_e2e_smoke_only_pipelinerun
from suite.pipelinerun_naming import is_rhoai_e2e_pipelinerun_name
from suite.errors import AppError
from k8s.oc_util import run_cmd, ts_now


_DNS1123_SUBDOMAIN_RE = re.compile(r"^[a-z0-9]([-a-z0-9]*[a-z0-9])?$", re.ASCII)


def _snapshot_param_is_resource_name(snap: str) -> bool:
    """True when SNAPSHOT param looks like a Kubernetes object name (not inline JSON)."""
    s = (snap or "").strip()
    if not s or s[0] in "{[":
        return False
    if len(s) > 253:
        return False
    return bool(_DNS1123_SUBDOMAIN_RE.fullmatch(s))


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


def _pod_container_waiting_lines(pod: dict[str, Any]) -> list[str]:
    """Summarize kubelet waiting state (image pull, crash loop, etc.) from a Pod object."""
    lines: list[str] = []
    status = pod.get("status") if isinstance(pod.get("status"), dict) else {}
    for group_key in ("initContainerStatuses", "containerStatuses"):
        for cs in status.get(group_key) or []:
            if not isinstance(cs, dict):
                continue
            waiting = (cs.get("state") or {}).get("waiting")
            if not isinstance(waiting, dict):
                continue
            reason = str(waiting.get("reason") or "").strip()
            message = str(waiting.get("message") or "").strip()
            if not reason and not message:
                continue
            cname = str(cs.get("name") or "container").strip()
            detail = reason or "waiting"
            if message and message not in detail:
                detail = f"{detail} — {message}"
            lines.append(f"Pod {cname}: {detail}")
    return lines


def format_taskrun_failure_detail(
    tr: dict[str, Any],
    *,
    pod: dict[str, Any] | None = None,
) -> str:
    """Human-readable failure when Tekton step logs are empty (image pull, scheduling, etc.)."""
    from steps.tekton_incluster import task_succeeded_detail

    _status, reason, message = task_succeeded_detail(tr)
    lines: list[str] = []
    if reason:
        lines.append(f"TaskRun condition reason: {reason}")
    if message:
        for line in message.splitlines():
            line = line.strip()
            if line:
                lines.append(f"TaskRun condition message: {line}")
    steps = (tr.get("status") or {}).get("steps") or []
    term_reasons = sorted(
        {
            str(step.get("terminationReason") or "").strip()
            for step in steps
            if isinstance(step, dict) and str(step.get("terminationReason") or "").strip()
        }
    )
    if term_reasons:
        lines.append(f"Step termination: {', '.join(term_reasons)}")
    if pod:
        lines.extend(_pod_container_waiting_lines(pod))
    if not lines:
        return "(no step logs — TaskRun did not execute any steps)"
    return "\n".join(lines)


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

        th = threading.Thread(target=_spin, name="rhoai-e2e-spin", daemon=True)
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


def format_rhoai_e2e_watch_cli(
    *,
    operator_install_dir: Path,
    namespace: str,
    app: str,
    pipelinerun: str | None,
) -> str:
    """Copy-pastable command: stream logs or KubeArchive replay (``-w``)."""
    script = operator_install_dir / "rhoai_e2e.py"
    base = f"{shlex.quote(sys.executable)} {shlex.quote(str(script))} -w"
    if (pipelinerun or "").strip():
        base = f"{base} {shlex.quote(pipelinerun.strip())}"
    return (
        f"{base} --konflux-namespace {shlex.quote(namespace)} "
        f"--konflux-app {shlex.quote(app)}"
    )


def first_snapshot_component_name(snapshot_yaml: str) -> str:
    """Template components[].name from integration-tests/rhoai-e2e/config/test-snapshot.yaml."""
    m = re.search(r"(?m)^\s+-\s+name:\s+(\S+)\s*$", snapshot_yaml)
    if not m:
        snippet = snapshot_yaml[:200].replace("\n", " ")
        raise AppError(
            "Could not locate the first snapshot component name in config/test-snapshot.yaml "
            f"(template drift?). Snippet: {snippet!r}"
        )
    return m.group(1)


_FAILED_UNKNOWN_REASONS = frozenset(
    {
        "ResolvingTaskRef",
        "CouldntGetPipeline",
        "PipelineValidationFailed",
        "InvalidWorkspaceBindings",
    }
)


def archived_pipelinerun_task_refs(
    prj: dict[str, Any],
    pr_name: str,
    *,
    list_archived_taskruns: Callable[[], dict[str, Any]] | None = None,
) -> list[tuple[str, str]]:
    """Resolve TaskRun name + pipeline task label from archived PipelineRun JSON."""
    refs = prj.get("status", {}).get("childReferences", [])
    out: list[tuple[str, str]] = []
    for ref in refs:
        tr_name = ref.get("name", "") or ""
        task_name = ref.get("pipelineTaskName", "") or tr_name
        if tr_name:
            out.append((tr_name, task_name))
    if out:
        return out
    if list_archived_taskruns is None:
        return []
    data = list_archived_taskruns()
    for item in data.get("items", []):
        md = item.get("metadata", {}) or {}
        tr_name = md.get("name", "") or ""
        labels = md.get("labels", {}) or {}
        task_name = labels.get("tekton.dev/pipelineTask", "") or tr_name
        if tr_name:
            out.append((tr_name, task_name))
    return out


def filter_pipelinerun_items(
    items: list[dict[str, Any]],
    *,
    app: str,
    rhoai_e2e_only: bool = False,
    rhoai_e2e_family_only: bool = False,
    name_substr: str | None = None,
    skip_smoke: bool = True,
) -> list[dict[str, Any]]:
    """Filter Tekton PipelineRun objects for ``--app`` (optional E2E/rhoai-e2e name gate)."""
    out: list[dict[str, Any]] = []
    for item in items:
        md = item.get("metadata") or {}
        labels = md.get("labels") or {}
        name = md.get("name", "")
        app_label = labels.get("appstudio.openshift.io/application", "")
        if rhoai_e2e_only or rhoai_e2e_family_only:
            if not is_rhoai_e2e_pipelinerun_name(name):
                continue
        if app_label != app:
            continue
        if name_substr is not None and name_substr not in name:
            continue
        if skip_smoke:
            pipe = labels.get("tekton.dev/pipeline", "")
            if rhoai_e2e_smoke_only_pipelinerun(name, pipe):
                continue
        out.append(item)
    return out


def pipelinerun_snapshot_param(item: dict[str, Any]) -> str:
    """SNAPSHOT PipelineRun param (resource name or inline JSON), or empty."""
    for p in item.get("spec", {}).get("params", []):
        if p.get("name") == "SNAPSHOT":
            return str(p.get("value", ""))
    return ""


def pipelinerun_resolved_owner(item: dict[str, Any], *, snapshot_owner: str = "") -> str:
    """PR ``rhoai-e2e.run-owner`` annotation, else Snapshot owner when SNAPSHOT is a resource name."""
    owner = (item.get("metadata", {}).get("annotations") or {}).get("rhoai-e2e.run-owner", "")
    if owner:
        return owner
    snap = pipelinerun_snapshot_param(item)
    if snapshot_owner and _snapshot_param_is_resource_name(snap):
        return snapshot_owner
    return ""


def pipelinerun_has_started_tasks(item: dict[str, Any]) -> bool:
    """True when Tekton has created at least one child TaskRun/PipelineRun."""
    refs = (item.get("status") or {}).get("childReferences")
    return isinstance(refs, list) and bool(refs)


def pipelinerun_stuck_no_tasks_started(item: dict[str, Any]) -> bool:
    """True for incomplete runs where Tekton never created TaskRuns (quota/resolver hang)."""
    status = item.get("status") if isinstance(item.get("status"), dict) else {}
    if status.get("completionTime") or pipelinerun_has_started_tasks(item):
        return False
    conditions = status.get("conditions") if isinstance(status.get("conditions"), list) else []
    cond = next((c for c in conditions if isinstance(c, dict) and c.get("type") == "Succeeded"), {})
    cstat = cond.get("status") or "Unknown"
    if cstat in ("True", "False"):
        return False
    reason = (cond.get("reason") or "").strip()
    return not reason or reason in PENDING_REASONS or reason == "Running"


def pipelinerun_list_state(item: dict[str, Any]) -> str:
    """Map Tekton PipelineRun JSON to a short label for ``rhoai_e2e.py -l``."""
    status = item.get("status") if isinstance(item.get("status"), dict) else {}
    conditions = status.get("conditions") if isinstance(status.get("conditions"), list) else []
    cond = next((c for c in conditions if isinstance(c, dict) and c.get("type") == "Succeeded"), {})
    cstat = cond.get("status") or "Unknown"
    reason = (cond.get("reason") or "").strip()

    if cstat == "True":
        return "completed"
    if cstat == "False":
        return "failed"
    if reason in _FAILED_UNKNOWN_REASONS:
        return "failed"
    if reason in ("PipelineRunPending", "PipelineRunStopping"):
        return "pending"
    if reason == "Running":
        return "running"
    if status.get("completionTime"):
        return "failed"
    child_refs = status.get("childReferences")
    if isinstance(child_refs, list) and child_refs:
        return "running"
    if reason and any(token in reason for token in ("Resolv", "Couldnt", "Validation", "Invalid")):
        return "failed"
    if reason:
        return "pending"
    return "unknown"


def try_cancel_pipelinerun(name: str, namespace: str) -> tuple[bool, str]:
    """Gracefully cancel a live PipelineRun (Konflux UI Stop/Cancel equivalent).

    Uses ``tkn pipelinerun cancel --grace StoppedRunFinally`` when ``tkn`` is available.
    Returns ``(ok, detail)``; *detail* is a short status string for logging.
    """
    if not name or not namespace:
        return False, "missing name or namespace"
    if not shutil.which("tkn"):
        return False, "tkn not in PATH"
    last_detail = ""
    for grace in ("StoppedRunFinally", ""):
        cmd = ["tkn", "pipelinerun", "cancel", name, "-n", namespace]
        if grace:
            cmd.extend(["--grace", grace])
        proc = run_cmd(cmd, capture=True, check=False)
        if proc.returncode == 0:
            msg = (proc.stdout or proc.stderr or "").strip().splitlines()
            return True, msg[-1] if msg else "cancelled"
        last_detail = (proc.stderr or proc.stdout or "").strip()
    return False, last_detail or "cancel failed"


def pipelinerun_delete_candidate(
    item: dict[str, Any],
    *,
    app: str,
    run_owner: str,
    snapshot_owner: str = "",
    stop_owned_running: bool = False,
    include_unowned_stuck: bool = False,
) -> tuple[bool, str]:
    """Return (delete, reason) for --delete-pending-pipelines selection (live cluster actions)."""
    name = item.get("metadata", {}).get("name", "")
    labels = item.get("metadata", {}).get("labels", {}) or {}
    app_label = labels.get("appstudio.openshift.io/application", "")
    if not is_rhoai_e2e_pipelinerun_name(name):
        return False, ""
    status = item.get("status") if isinstance(item.get("status"), dict) else {}
    if status.get("completionTime"):
        return False, ""
    conditions = status.get("conditions") if isinstance(status.get("conditions"), list) else []
    cond = next((c for c in conditions if isinstance(c, dict) and c.get("type") == "Succeeded"), {})
    reason = (cond.get("reason") or "").strip()
    resolved_owner = pipelinerun_resolved_owner(item, snapshot_owner=snapshot_owner)
    owned_by_run = bool(resolved_owner and resolved_owner == run_owner)
    matches_app = app_label == app
    if app_label and not matches_app:
        return False, ""
    pipe = labels.get("tekton.dev/pipeline", "")
    unowned_stuck = (
        include_unowned_stuck
        and pipelinerun_stuck_no_tasks_started(item)
        and not rhoai_e2e_smoke_only_pipelinerun(name, pipe)
    )
    if not matches_app and not owned_by_run and not unowned_stuck:
        return False, ""
    if reason in ("PipelineRunPending", "ResolvingPipelineRef") and (matches_app or owned_by_run):
        return True, "pending"
    if owned_by_run:
        if reason == "Running" and pipelinerun_has_started_tasks(item) and not stop_owned_running:
            return False, ""
        return True, "owned"
    if unowned_stuck:
        return True, "stuck-no-tasks"
    return False, ""


@dataclass
class PipelineRow:
    name: str
    app: str
    state: str
    created: str
    source: str
    snapshot: str = ""




def pipelinerun_external_cluster_id(item: dict[str, Any], *, namespace: str) -> str:
    """Resolve physical cluster id for an rhoai-e2e PipelineRun (label or CLUSTER_SOURCE)."""
    from k8s.external_kubeconfig import _pipelinerun_cluster_label, resolve_cluster_id_for_external_cluster
    from runners.report.pipelinerun_metadata import cluster_label_from_cluster_source, pipelinerun_param_value

    label = _pipelinerun_cluster_label(item)
    if label:
        return label
    source = pipelinerun_param_value(item, "CLUSTER_SOURCE", "")
    if not (source or "").strip():
        return ""
    resolved = resolve_cluster_id_for_external_cluster(namespace=namespace, cluster_source=source)
    return resolved or cluster_label_from_cluster_source(source)


class Tee:
    def __init__(self, *streams: Any) -> None:
        self.streams = streams

    def write(self, data: str) -> None:
        for s in self.streams:
            s.write(data)

    def flush(self) -> None:
        for s in self.streams:
            s.flush()
