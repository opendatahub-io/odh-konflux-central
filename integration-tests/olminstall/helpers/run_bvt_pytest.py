#!/usr/bin/env python3
"""Run BVT pytest inside the opendatahub-tests image.

Parameterised via env vars so a single script covers cluster_health,
operator_health, and --collect-only modes.

Env (required):
    ARTIFACT_PREFIX  -- filename prefix for JUnit XML + console log
Env (optional):
    PYTEST_MARKER    -- pytest -m expression (empty = no -m flag)
    PYTEST_EXTRA_ARGS -- extra pytest CLI args (e.g. "--collect-only -q" or "-svv")
    ARTIFACTS_DIR    -- directory for JUnit + logs (default /artifacts; must be under
                       /artifacts or TEST_ARTIFACTS_DIR when set for local runs)
    TEST_ARTIFACTS_DIR -- optional extra allowed root (e.g. ./artifacts) for local debugging
    TESTS_SUBDIR     -- subdirectory under tests root (default "tests/cluster_health")
"""

from __future__ import annotations

import os
import shlex
import shutil
import subprocess
import sys
import threading
from datetime import datetime, timezone
from pathlib import Path

_KNOWN_ROOTS = [
    "/home/odh/opendatahub-tests",
    "/opendatahub-tests",
    "/opt/app-root/src",
    "/workspace/source",
]


def _safe_artifact_prefix(raw: str) -> str | None:
    """ARTIFACT_PREFIX must be a single filename segment (no path separators)."""
    if not raw or raw in {".", ".."}:
        return None
    if "/" in raw or "\\" in raw:
        return None
    p = Path(raw)
    if p.is_absolute() or ".." in p.parts or p.name != raw:
        return None
    return raw


def _artifacts_dir_bases() -> tuple[Path, ...]:
    """Allowed roots for ARTIFACTS_DIR (pipeline + local dev)."""
    bases: list[Path] = [Path("/artifacts")]
    extra = os.environ.get("TEST_ARTIFACTS_DIR", "").strip()
    if extra:
        bases.append(Path(extra))
    return tuple(bases)


def _validate_artifacts_dir(raw: str) -> Path:
    """Resolve *raw* and ensure it stays under an allowed artifacts root."""
    resolved = Path(raw).resolve()
    for base in _artifacts_dir_bases():
        root = base.resolve()
        if resolved == root:
            return resolved
        try:
            resolved.relative_to(root)
            return resolved
        except ValueError:
            continue
    print(
        f"ARTIFACTS_DIR must resolve under {', '.join(str(b) for b in _artifacts_dir_bases())}; got {resolved}",
        file=sys.stderr,
    )
    sys.exit(1)


def _safe_tests_subdir(raw: str) -> str | None:
    """TESTS_SUBDIR must be a relative path without .. components."""
    if not raw:
        return None
    p = Path(raw)
    if p.is_absolute() or ".." in p.parts:
        return None
    return raw


def _locate_tests_root(tests_subdir: str) -> str | None:
    sub = Path(tests_subdir)
    if sub.is_absolute() or ".." in sub.parts:
        return None
    for d in _KNOWN_ROOTS:
        if (Path(d) / sub).is_dir():
            return d
    return None


def _build_pytest_args(
    marker: str,
    extra_args: str,
    tests_subdir: str,
    junit_path: str,
) -> list[str]:
    args: list[str] = []
    if marker:
        args.extend(["-m", marker])
    if extra_args:
        args.extend(shlex.split(extra_args))
    args.extend([tests_subdir, f"--junitxml={junit_path}", "--tb=native"])
    return args


def _bvt_timeout_seconds() -> float | None:
    raw = os.environ.get("BVT_RUN_TIMEOUT_SECS", "").strip()
    if not raw:
        return None
    try:
        secs = float(raw)
    except ValueError as exc:
        raise ValueError(
            f"BVT_RUN_TIMEOUT_SECS must be a positive number, got: {raw!r}"
        ) from exc
    if secs <= 0:
        raise ValueError(f"BVT_RUN_TIMEOUT_SECS must be a positive number, got: {raw!r}")
    return secs


def _run_with_tee(
    cmd: list[str],
    log_path: str,
    *,
    env: dict[str, str] | None = None,
    timeout: float | None = None,
) -> int:
    """Run *cmd*, tee stdout+stderr to *log_path*, return exit code.

    *timeout* overrides ``BVT_RUN_TIMEOUT_SECS`` when not ``None`` (omit for env-based timeout).
    """
    timeout_s = _bvt_timeout_seconds() if timeout is None else (timeout if timeout > 0 else None)
    attempt_header = (
        f"\n--- BVT pytest attempt {datetime.now(timezone.utc).isoformat()} "
        f"({' '.join(cmd[:3])}...) ---\n"
    )
    with open(log_path, "a", encoding="utf-8") as log:
        log.write(attempt_header)
        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            env=env,
        )
        assert proc.stdout is not None

        def _tee_stdout() -> None:
            for line in proc.stdout:
                sys.stdout.write(line)
                log.write(line)

        reader = threading.Thread(target=_tee_stdout, daemon=True)
        reader.start()
        try:
            if timeout_s is not None:
                proc.wait(timeout=timeout_s)
            else:
                proc.wait()
        except subprocess.TimeoutExpired:
            print(
                f"ERROR: command timed out after {timeout_s}s (BVT_RUN_TIMEOUT_SECS): {' '.join(cmd)}",
                file=sys.stderr,
            )
            proc.terminate()
            try:
                proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait(timeout=30)
            reader.join(timeout=5)
            return 124
        reader.join()
    return proc.returncode if proc.returncode is not None else 1


def main() -> int:
    artifact_prefix = os.environ.get("ARTIFACT_PREFIX", "").strip()
    if not artifact_prefix:
        print("ARTIFACT_PREFIX is required", file=sys.stderr)
        return 1
    if _safe_artifact_prefix(artifact_prefix) is None:
        print("ARTIFACT_PREFIX must be a single filename segment (no / or ..)", file=sys.stderr)
        return 1

    artifacts_path = _validate_artifacts_dir(os.environ.get("ARTIFACTS_DIR", "/artifacts").strip())
    tests_subdir = os.environ.get("TESTS_SUBDIR", "tests/cluster_health").strip()
    if _safe_tests_subdir(tests_subdir) is None:
        print("TESTS_SUBDIR must be a relative path without ..", file=sys.stderr)
        return 1
    pytest_marker = os.environ.get("PYTEST_MARKER", "").strip()
    pytest_extra = os.environ.get("PYTEST_EXTRA_ARGS", "").strip()

    root = _locate_tests_root(tests_subdir)
    if not root:
        print(f"ERROR: could not find {tests_subdir} under known opendatahub-tests paths.", file=sys.stderr)
        return 1

    os.chdir(root)
    Path("results").mkdir(exist_ok=True)

    artifacts_path.mkdir(parents=True, exist_ok=True)
    junit = str(artifacts_path / f"{artifact_prefix}.xml")
    log = str(artifacts_path / f"{artifact_prefix}.console.log")
    pytest_args = _build_pytest_args(pytest_marker, pytest_extra, tests_subdir, junit)

    # Official opendatahub-tests image: ENTRYPOINT is `uv run pytest` after
    # build-time `uv sync`. Tekton script mode bypasses ENTRYPOINT. Upstream
    # Dockerfile puts uv in /.local/bin; ensure that and ~/.local/bin are on PATH.
    extra_path = "/.local/bin:/home/odh/.local/bin"
    env = dict(os.environ)
    env["PATH"] = f"{extra_path}:{env.get('PATH', '')}"

    ec = 1
    ran_uv = False

    uv = shutil.which("uv", path=env["PATH"])
    pyproject = Path("pyproject.toml")
    if uv and pyproject.is_file():
        ran_uv = True
        ec = _run_with_tee([uv, "run", "pytest", *pytest_args], log, env=env)
        if ec == 124:
            return ec
        # pytest exit 0=pass 1=tests failed; anything ≥2 is an infra/collection error.
        if ec >= 2:
            print(f"WARN: uv run pytest exited {ec} (infra error); retrying without UV_NO_SYNC...", file=sys.stderr)
            retry_env = {k: v for k, v in env.items() if k != "UV_NO_SYNC"}
            ec = _run_with_tee([uv, "run", "pytest", *pytest_args], log, env=retry_env)
            if ec == 124:
                return ec

    if not ran_uv:
        # ec was never set by uv; treat as infra so pip+pytest runs instead of exiting 1 without tests.
        ec = 2

    if ec == 124:
        return ec

    # Only fall back to bare python when uv is unavailable or produced an infra error.
    # ec==1 means tests ran but some failed — keep that result rather than re-running.
    if ec >= 2:
        print("WARN: uv unavailable or failed; using pip --target + PYTHONPATH (may miss non-pytest deps).", file=sys.stderr)
        pylibs = "/tmp/tekton-pytest-libs"
        Path(pylibs).mkdir(exist_ok=True)
        pip_env = dict(env)
        pip_env["PYTHONPATH"] = pylibs
        try:
            subprocess.run(
                ["python3", "-c", "import pytest, shortuuid, kubernetes"],
                env=pip_env, check=True, capture_output=True,
            )
        except subprocess.CalledProcessError:
            pip_proc = subprocess.run(
                [
                    "python3",
                    "-m",
                    "pip",
                    "install",
                    "--no-cache-dir",
                    "-q",
                    "-t",
                    pylibs,
                    "pytest",
                    "shortuuid",
                    "kubernetes",
                ],
                env=env,
                check=False,
                capture_output=True,
                text=True,
            )
            if pip_proc.returncode != 0:
                tail = ((pip_proc.stdout or "") + (pip_proc.stderr or "")).strip()[:4000]
                print(
                    f"ERROR: pip install pytest shortuuid kubernetes failed (exit {pip_proc.returncode}): "
                    f"{tail or '(no output)'}",
                    file=sys.stderr,
                )
                return pip_proc.returncode
        existing = env.get("PYTHONPATH", "")
        pip_env["PYTHONPATH"] = f"{pylibs}:{existing}" if existing else pylibs
        ec = _run_with_tee(["python3", "-m", "pytest", *pytest_args], log, env=pip_env)

    print(f"JUnit ({artifact_prefix}): {junit}")
    return ec


if __name__ == "__main__":
    raise SystemExit(main())
