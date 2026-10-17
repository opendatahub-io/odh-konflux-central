"""OpenShift `oc` subprocess helpers."""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlparse

from suite.errors import AppError

_DEFAULT_TIMEOUT_S = 180
_OC_VERBOSE_TRUTHY = frozenset({"1", "true", "yes", "on"})
_BULK_OUTPUT_FORMATS = frozenset({"json", "yaml", "yml"})


def oc_verbose_enabled() -> bool:
    """True when ``OLMINSTALL_OC_VERBOSE`` requests terminal-like ``oc`` logging."""
    return os.environ.get("OLMINSTALL_OC_VERBOSE", "").strip().lower() in _OC_VERBOSE_TRUTHY


def _oc_path() -> str:
    """Resolve ``oc`` for Tekton steps; opendatahub-tests image often has ``kubectl`` only."""
    artifacts = Path(os.environ.get("ARTIFACTS_DIR", "/artifacts"))
    from steps.tests_payload import resolve_tests_payload_root, tests_payload_tools_bin_dir

    tools_oc = tests_payload_tools_bin_dir(resolve_tests_payload_root(artifacts)) / "oc"
    if tools_oc.is_file():
        return str(tools_oc)
    artifacts_bin = artifacts / "bin" / "oc"
    if artifacts_bin.is_file():
        return str(artifacts_bin)
    path = shutil.which("oc") or shutil.which("kubectl")
    if path:
        return path
    for candidate in (
        "/usr/bin/oc",
        "/usr/bin/kubectl",
        "/usr/local/bin/oc",
        "/usr/local/bin/kubectl",
    ):
        if Path(candidate).is_file():
            return candidate
    raise AppError("'oc' binary not found in PATH", 1)


def _oc_output_format_bulk(args: list[str]) -> bool:
    """True when ``oc`` is likely to emit large structured output we should not tee."""
    idx = 0
    while idx < len(args):
        arg = args[idx]
        if arg in ("-o", "--output"):
            if idx + 1 < len(args):
                fmt = args[idx + 1].split("=", 1)[0].strip().lower()
                return fmt in _BULK_OUTPUT_FORMATS
            return False
        if arg.startswith("-o="):
            fmt = arg[3:].split("=", 1)[0].strip().lower()
            return fmt in _BULK_OUTPUT_FORMATS
        if arg.startswith("--output="):
            fmt = arg.split("=", 1)[1].split("=", 1)[0].strip().lower()
            return fmt in _BULK_OUTPUT_FORMATS
        if arg.startswith("-o") and len(arg) > 2:
            fmt = arg[2:].split("=", 1)[0].strip().lower()
            if fmt in _BULK_OUTPUT_FORMATS:
                return True
        idx += 1
    return False


def _oc_output_sensitive(args: list[str]) -> bool:
    """True when ``oc`` output may contain credentials we should not tee."""
    lowered = [arg.lower() for arg in args]
    return lowered[:2] == ["whoami", "-t"] or any(
        arg in ("-p", "--password", "--token")
        or arg.startswith("--token=")
        or arg.startswith("--password=")
        or "password" in arg
        for arg in lowered
    )


_SENSITIVE_FLAGS = frozenset({"-p", "--password", "--token"})


def _redact_oc_args(args: list[str]) -> list[str]:
    redacted: list[str] = []
    skip_next = False
    for arg in args:
        if skip_next:
            redacted.append("***")
            skip_next = False
            continue
        lowered = arg.lower()
        if lowered in _SENSITIVE_FLAGS:
            redacted.append(arg)
            skip_next = True
        elif lowered.startswith("--token=") or lowered.startswith("--password="):
            key = arg.split("=", 1)[0]
            redacted.append(f"{key}=***")
        else:
            redacted.append(arg)
    return redacted


def _format_oc_invocation(args: list[str], *, stdin_text: str | None = None) -> str:
    line = "oc " + " ".join(_redact_oc_args(args))
    if stdin_text is not None and "-f" in args and "-" in args:
        line += "  # <stdin omitted>"
    return line


def _verbose_log_exit(returncode: int) -> None:
    print(f"→ exit {returncode}", flush=True)


def _drain_stream(
    stream: Any,
    chunks: list[str],
    *,
    echo: bool,
    echo_to: Any,
) -> None:
    for line in iter(stream.readline, ""):
        chunks.append(line)
        if echo:
            echo_to.write(line)
            echo_to.flush()
    stream.close()


def _run_subprocess_with_tee(
    exec_cmd: list[str],
    *,
    stdin_text: str | None,
    timeout: float | None,
    env: dict[str, str] | None,
    echo_stdout: bool,
    echo_stderr: bool,
) -> subprocess.CompletedProcess[str]:
    popen = subprocess.Popen(
        exec_cmd,
        stdin=subprocess.PIPE if stdin_text is not None else None,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env=env,
    )
    if stdin_text is not None:
        assert popen.stdin is not None
        popen.stdin.write(stdin_text)
        popen.stdin.close()

    stdout_chunks: list[str] = []
    stderr_chunks: list[str] = []
    threads = [
        threading.Thread(
            target=_drain_stream,
            args=(popen.stdout, stdout_chunks),
            kwargs={"echo": echo_stdout, "echo_to": sys.stdout},
            daemon=True,
        ),
        threading.Thread(
            target=_drain_stream,
            args=(popen.stderr, stderr_chunks),
            kwargs={"echo": echo_stderr, "echo_to": sys.stderr},
            daemon=True,
        ),
    ]
    for thread in threads:
        thread.start()
    try:
        returncode = popen.wait(timeout=timeout)
    except subprocess.TimeoutExpired as exc:
        popen.kill()
        for thread in threads:
            thread.join()
        raise exc
    for thread in threads:
        thread.join()
    return subprocess.CompletedProcess(
        exec_cmd,
        returncode,
        stdout="".join(stdout_chunks),
        stderr="".join(stderr_chunks),
    )


def _execute_subprocess(
    exec_cmd: list[str],
    *,
    display_cmd: str | None,
    verbose: bool,
    capture_output: bool,
    check: bool,
    stdin_text: str | None,
    env: dict[str, str] | None,
    timeout: float | None,
) -> subprocess.CompletedProcess[str]:
    if verbose and display_cmd:
        print(f"+ {display_cmd}", flush=True)

    bulk_capture = bool(capture_output and display_cmd and _oc_output_format_bulk(exec_cmd[1:]))
    sensitive_capture = bool(capture_output and display_cmd and _oc_output_sensitive(exec_cmd[1:]))

    try:
        if (
            capture_output
            and verbose
            and display_cmd
            and not bulk_capture
            and not sensitive_capture
            and stdin_text is None
        ):
            proc = _run_subprocess_with_tee(
                exec_cmd,
                stdin_text=stdin_text,
                timeout=timeout,
                env=env,
                echo_stdout=True,
                echo_stderr=True,
            )
        else:
            proc = subprocess.run(
                exec_cmd,
                text=True,
                input=stdin_text,
                capture_output=capture_output,
                env=env,
                check=False,
                timeout=timeout,
            )
            if verbose and display_cmd and bulk_capture and proc.stdout:
                print(
                    f"  (output omitted: bulk -o format, {len(proc.stdout)} bytes)",
                    flush=True,
                )
    except subprocess.TimeoutExpired:
        raise

    if verbose and display_cmd:
        _verbose_log_exit(proc.returncode)

    if check and proc.returncode != 0:
        raise subprocess.CalledProcessError(
            proc.returncode,
            exec_cmd,
            output=proc.stdout,
            stderr=proc.stderr,
        )
    return proc


def run_cmd(
    cmd: list[str],
    *,
    capture: bool = True,
    check: bool = True,
    input_text: str | None = None,
    env: dict[str, str] | None = None,
    timeout: float | None = _DEFAULT_TIMEOUT_S,
) -> subprocess.CompletedProcess[str]:
    exec_cmd = list(cmd)
    oc_args: list[str] | None = None
    if exec_cmd and exec_cmd[0] == "oc":
        oc_args = exec_cmd[1:]
        exec_cmd = [_oc_path(), *oc_args]
    verbose = bool(oc_args is not None and oc_verbose_enabled())
    display_cmd = _format_oc_invocation(oc_args, stdin_text=input_text) if oc_args is not None else None
    try:
        proc = _execute_subprocess(
            exec_cmd,
            display_cmd=display_cmd,
            verbose=verbose,
            capture_output=capture,
            check=False,
            stdin_text=input_text,
            env=env,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired as exc:
        raise AppError(f"Command timed out after {timeout}s: {' '.join(cmd)}", 1) from exc
    if check and proc.returncode != 0:
        stderr = (proc.stderr or "").strip()
        stdout = (proc.stdout or "").strip()
        detail = stderr or stdout or "<no output>"
        raise AppError(
            f"Command failed ({proc.returncode}): {' '.join(cmd)}\n{detail}",
            1,
        )
    return proc


def run_oc(
    args: list[str],
    *,
    check: bool = False,
    capture_output: bool = True,
    text: bool = True,
    stdin_text: str | None = None,
    input_text: str | None = None,
    timeout: float | None = _DEFAULT_TIMEOUT_S,
    on_timeout: Callable[[str], None] | None = None,
    env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    """Run ``oc`` with Tekton-step-friendly defaults (alias of capture_output → capture)."""
    del text  # kept for call-site compatibility; subprocess always uses text=True
    if input_text is not None:
        if stdin_text is not None:
            raise TypeError("run_oc(): pass only one of stdin_text or input_text")
        stdin_text = input_text
    oc_path = _oc_path()
    exec_cmd = [oc_path, *args]
    verbose = oc_verbose_enabled()
    display_cmd = _format_oc_invocation(args, stdin_text=stdin_text)
    try:
        return _execute_subprocess(
            exec_cmd,
            display_cmd=display_cmd,
            verbose=verbose,
            capture_output=capture_output,
            check=check,
            stdin_text=stdin_text,
            env=env,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        tail = " ".join(args[:10]) + (" ..." if len(args) > 10 else "")
        msg = f"oc timed out ({timeout}s): {tail}"
        if on_timeout is not None:
            on_timeout(msg)
        raise AppError(msg, 1) from None


def parse_json_output(cmd: list[str]) -> dict[str, Any]:
    proc = run_cmd(cmd, capture=True, check=False)
    if proc.returncode != 0 or not proc.stdout.strip():
        return {}
    try:
        return json.loads(proc.stdout)
    except json.JSONDecodeError:
        return {}


def get_jsonpath(cmd: list[str]) -> str:
    proc = run_cmd(cmd, capture=True, check=False)
    if proc.returncode != 0:
        return ""
    return proc.stdout.strip()


def ts_now() -> str:
    return time.strftime("%H:%M:%S")


def filter_warning_lines(text: str) -> str:
    return "\n".join(line for line in text.splitlines() if not line.startswith("Warning"))


def _openshift_apps_middle_labels_valid(middle: str) -> bool:
    """Reject api..openshiftapps.com, empty labels, and invalid DNS label chars."""
    if not middle or ".." in middle or middle.startswith(".") or middle.endswith("."):
        return False
    label_re = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$")
    for label in middle.split("."):
        if not label or not label_re.match(label):
            return False
    return True


def hosted_openshift_apps_cluster_suffix(api_server: str) -> str:
    """Return DNS suffix after ``api.`` for ``api.<cluster>.openshiftapps.com`` API servers, else ``\"\"``."""
    parsed = urlparse((api_server or "").strip())
    host = (parsed.hostname or "").lower()
    if not host.startswith("api.") or not host.endswith(".openshiftapps.com"):
        return ""
    tail = ".openshiftapps.com"
    middle = host[4 : -len(tail)]
    if not middle or not _openshift_apps_middle_labels_valid(middle):
        return ""
    return host[4:]


def derive_kubearchive_host(api_server: str) -> str:
    """
    Best-effort KubeArchive base URL for Konflux on hosted OpenShift (api.*.openshiftapps.com).

    Returns empty string when the API hostname does not match the expected pattern.
    """
    suffix = hosted_openshift_apps_cluster_suffix(api_server)
    if not suffix:
        return ""
    return f"https://kubearchive-api-server-product-kubearchive.apps.{suffix}"


def derive_konflux_ui_base(api_server: str) -> str:
    """Best-effort Konflux UI base URL (``https://konflux-ui.apps.<suffix>``) for the same pattern."""
    suffix = hosted_openshift_apps_cluster_suffix(api_server)
    if not suffix:
        return ""
    return f"https://konflux-ui.apps.{suffix}"
