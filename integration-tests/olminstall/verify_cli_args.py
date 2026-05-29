#!/usr/bin/env python3
"""
Batch-verify argparse + parse_cli_args for olm_pipeline.py (no oc / cluster).

This process only exercises ``parse_cli_args`` — it does **not** call ``oc`` or start
``OLMInstallRunner.run()``. It is meant to pass in CI or on a laptop with no Konflux
context. A full ``olm_pipeline.py`` trigger/watch (including ``--tests bvt``) **will
fail** without a logged-in Konflux cluster and tenant; that is expected until a future
option exists to target an existing cluster URL / kubeconfig from this entrypoint.

Run:  python3 integration-tests/olminstall/verify_cli_args.py
"""

from __future__ import annotations

import contextlib
import io
import os
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from helpers.cli import make_parser, parse_cli_args  # noqa: E402
from helpers.errors import AppError  # noqa: E402


def _patch_env(updates: dict[str, str | None]) -> dict[str, str | None]:
    prev: dict[str, str | None] = {}
    for k, v in updates.items():
        prev[k] = os.environ.get(k)
        if v is None:
            os.environ.pop(k, None)
        else:
            os.environ[k] = v
    return prev


def _restore_env(prev: dict[str, str | None]) -> None:
    for k, v in prev.items():
        if v is None:
            os.environ.pop(k, None)
        else:
            os.environ[k] = v


def main() -> int:
    # make_parser() uses Path(sys.argv[0]).name as prog — match the real entrypoint.
    _argv0, sys.argv[0] = sys.argv[0], str(_ROOT / "olm_pipeline.py")
    try:
        parser = make_parser()
    finally:
        sys.argv[0] = _argv0
    failures: list[str] = []
    n_ok = 0

    def expect_ok(argv: list[str], env: dict[str, str | None] | None = None, check=None) -> None:
        nonlocal n_ok
        label = f"OK {argv!r} env={env!r}"
        prev = _patch_env(env or {})
        try:
            args = parse_cli_args(parser, argv)
            if check:
                check(args)
            n_ok += 1
        except AssertionError as exc:
            failures.append(f"{label} -> assert: {exc}")
        except Exception as exc:
            failures.append(f"{label} -> unexpected {type(exc).__name__}: {exc}")
        finally:
            _restore_env(prev)

    def expect_err(argv: list[str], substr: str, env: dict[str, str | None] | None = None) -> None:
        nonlocal n_ok
        label = f"ERR {argv!r} expect {substr!r} env={env!r}"
        prev = _patch_env(env or {})
        try:
            parse_cli_args(parser, argv)
            failures.append(f"{label} -> expected AppError, got success")
        except AppError as exc:
            if substr not in str(exc):
                failures.append(f"{label} -> got {exc!r}")
            else:
                n_ok += 1
        except Exception as exc:
            failures.append(f"{label} -> wrong type {type(exc).__name__}: {exc}")
        finally:
            _restore_env(prev)

    def expect_argparse_fail(argv: list[str]) -> None:
        nonlocal n_ok
        label = f"argparse_fail {argv!r}"
        buf = io.StringIO()
        try:
            with contextlib.redirect_stderr(buf):
                parse_cli_args(parser, argv)
            failures.append(f"{label} -> expected SystemExit")
        except SystemExit as exc:
            if exc.code != 2:
                failures.append(f"{label} -> exit code {exc.code}")
            else:
                n_ok += 1
        except Exception as exc:
            failures.append(f"{label} -> {type(exc).__name__}: {exc}")

    # --- Valid parses ---
    expect_ok([], check=lambda a: (_assert(a.product == "none"), _assert(a.list_pipelines == 0), _assert(not a.watch_mode), _assert(a.prune_stale_its)))

    expect_ok(["--no-prune-stale-its"], check=lambda a: (_assert(not a.prune_stale_its),))

    expect_ok(
        ["--product", "odh"],
        check=lambda a: (
            _assert(a.product == "odh"),
            _assert(a.list_pipelines == 0),
            _assert(not a.watch_mode),
        ),
    )

    expect_ok(
        ["--tests", "bvt"],
        check=lambda a: (
            _assert(a.product == "none"),
            _assert(a.tests == "bvt"),
            _assert(not a.watch_mode),
        ),
    )

    expect_ok(
        ["--product", "rhoai", "--version", "3.5"],
        check=lambda a: (_assert(a.version == "3.5"), _assert(a.product == "rhoai")),
    )
    expect_ok(["--watch"], check=lambda a: (_assert(a.watch_mode), _assert(a.watch == "")))
    expect_ok(["--watch", "pr-xyz"], check=lambda a: (_assert(a.watch_mode), _assert(a.watch == "pr-xyz")))
    expect_ok(["--list-pipelines"], check=lambda a: _assert(a.list_pipelines == 10))
    expect_ok(["--list", "3"], check=lambda a: _assert(a.list_pipelines == 3))
    expect_ok(
        ["--list-supported-ocp"],
        check=lambda a: (_assert(a.list_supported_ocp), _assert(a.list_pipelines == 0), _assert(not a.watch_mode)),
    )
    expect_ok(
        ["--list-supported-ocp", "--ocp-version", "4.19"],
        check=lambda a: (
            _assert(a.list_supported_ocp),
            _assert(a.ocp_version == "4.19"),
            _assert(a.list_pipelines == 0),
        ),
    )
    expect_ok(
        ["--namespace", "ns1", "--app", "app1", "--channel", "ch1"],
        check=lambda a: (_assert(a.namespace == "ns1"), _assert(a.app == "app1"), _assert(a.channel == "ch1")),
    )
    expect_ok(
        ["--konflux-ui", "https://konflux-ui.example.com"],
        check=lambda a: _assert(a.konflux_ui == "https://konflux-ui.example.com"),
    )
    expect_ok(
        ["--ka-host", "https://kubearchive.example.com"],
        check=lambda a: _assert(a.ka_host == "https://kubearchive.example.com"),
    )
    expect_ok(
        ["--konflux-server", "https://api.stone.example.com:6443"],
        check=lambda a: _assert(a.konflux_server == "https://api.stone.example.com:6443"),
    )
    expect_ok(
        ["--image", "quay.io/rhoai/x@sha256:deadbeef"],
        check=lambda a: _assert(a.image == "quay.io/rhoai/x@sha256:deadbeef"),
    )
    expect_ok(
        ["--konflux-repo", "https://github.com/o/r.git", "--konflux-branch", "your-branch"],
        check=lambda a: (_assert(a.konflux_repo.endswith(".git")), _assert(a.konflux_branch == "your-branch")),
    )
    expect_ok(["--ocp-version", "4.20"], check=lambda a: _assert(a.ocp_version == "4.20"))
    expect_ok(["--ocp-version", " 4.19 "], check=lambda a: _assert(a.ocp_version == "4.19"))

    expect_ok(
        ["--tests", "bvt", "--ocp-version", "4.19"],
        check=lambda a: (_assert(a.tests == "bvt"), _assert(a.ocp_version == "4.19")),
    )
    expect_ok(
        ["--tests", "bvt", "--image", "quay.io/rhoai/x@sha256:deadbeef"],
        check=lambda a: _assert(a.tests == "bvt"),
    )
    expect_ok(
        ["--tests", "bvt", "--channel", "stable"],
        check=lambda a: _assert(a.tests == "bvt"),
    )
    expect_ok(
        ["--tests", "tier1,bvt"],
        check=lambda a: _assert(a.tests == "bvt,tier1"),
    )

    expect_ok(
        ["--ka-host"],
        env={"KA_HOST": "https://kubearchive.apps.cluster.openshiftapps.com"},
        check=lambda a: _assert(a.ka_host.startswith("https://kubearchive")),
    )

    # Trigger path: many install options (watch/list are mutually exclusive with these)
    expect_ok(
        [
            "--product",
            "rhoai",
            "--version",
            "3.4",
            "--channel",
            "stable-3.x",
            "--namespace",
            "rhoai-tenant",
            "--app",
            "testops-playpen",
            "--image",
            "quay.io/rhoai/f@sha256:abc",
            "--konflux-repo",
            "https://github.com/you/fork.git",
            "--konflux-branch",
            "branch",
            "--konflux-ui",
            "https://ui.example.com",
            "--ka-host",
            "https://ka.example.com",
            "--konflux-server",
            "https://api.stone-prod-p02.example.com:6443",
            "--ocp-version",
            "4.19",
        ],
        check=lambda a: (
            _assert(a.product == "rhoai"),
            _assert(a.version == "3.4"),
            _assert(a.channel == "stable-3.x"),
            _assert(a.namespace == "rhoai-tenant"),
            _assert(a.app == "testops-playpen"),
            _assert(a.ocp_version == "4.19"),
            _assert(not a.watch_mode),
            _assert(a.konflux_repo.endswith(".git")),
            _assert(a.konflux_branch == "branch"),
        ),
    )

    expect_ok(
        [
            "--watch",
            "--namespace",
            "rhoai-tenant",
            "--app",
            "testops-playpen",
            "--ka-host",
            "https://ka.example.com",
        ],
        check=lambda a: (_assert(a.watch_mode), _assert(a.namespace == "rhoai-tenant"), _assert(a.app == "testops-playpen")),
    )

    # --- Expected AppError ---
    expect_err(["--product", "odh", "--version", "1"], "--version is supported only")
    expect_err(["--version", "3.5"], "--version is supported only")
    expect_err(["--ka-host"], "KA_HOST", env={"KA_HOST": ""})

    prev_ka = _patch_env({"KA_HOST": None})
    try:
        expect_err(["--ka-host"], "KA_HOST")
    finally:
        _restore_env(prev_ka)

    expect_err(["--konflux-ui", "http://insecure.local"], "https://")
    expect_err(["--ka-host", "http://insecure.local"], "https://")
    expect_err(["--konflux-server", "http://api:6443"], "https://")
    expect_err(["--list-pipelines", "0"], "positive integer")
    expect_err(["--list-pipelines", "-3"], "positive integer")
    expect_err(["--list-pipelines", "nope"], "positive integer")
    expect_err(["--ocp-version", "4"], "MAJOR.MINOR")
    expect_err(["--ocp-version", "foo"], "MAJOR.MINOR")
    expect_err(["--ocp-version", "4.20.21"], "MAJOR.MINOR")
    expect_err(["--list-supported-ocp", "--list"], "mutually exclusive")
    expect_err(["--list-supported-ocp", "--watch"], "mutually exclusive")
    expect_err(["--list-pipelines", "--watch"], "mutually exclusive")
    expect_err(["--list", "--watch"], "mutually exclusive")
    expect_err(["--list-pipelines", "--channel", "x"], "Trigger/install options cannot be used")
    expect_err(["--list-pipelines", "--image", "quay.io/x@sha256:a"], "Trigger/install options cannot be used")
    expect_err(["--watch", "--ocp-version", "4.19"], "Trigger/install options cannot be used")
    expect_err(["--watch", "--version", "3.5", "--product", "rhoai"], "Trigger/install options cannot be used")
    expect_err(["--list-supported-ocp", "--konflux-repo", "https://g/r.git"], "Trigger/install options cannot be used")
    expect_err(["--list-pipelines", "--tests", "bvt"], "Trigger/install options cannot be used")

    expect_argparse_fail(["--product", "invalid"])
    expect_argparse_fail(["--bvt-env-only"])

    if failures:
        print(f"FAIL ({len(failures)}):", file=sys.stderr)
        for f in failures:
            print(f"  - {f}", file=sys.stderr)
        return 1

    print(f"OK: {n_ok} CLI checks passed in one process")
    return 0


def _assert(cond: bool) -> None:
    if not cond:
        raise AssertionError("condition false")


if __name__ == "__main__":
    raise SystemExit(main())
