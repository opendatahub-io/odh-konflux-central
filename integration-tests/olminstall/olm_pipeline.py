#!/usr/bin/env python3
"""Konflux OLM pipeline CLI — olminstall ITS helper. Run with no args or ``-h`` for usage."""

from __future__ import annotations

import atexit
import sys
from pathlib import Path

_OLMINSTALL_DIR = Path(__file__).resolve().parent
if str(_OLMINSTALL_DIR) not in sys.path:
    sys.path.insert(0, str(_OLMINSTALL_DIR))

from helpers.cli import emit_click_style_error, make_parser, parse_cli_args
from helpers.errors import AppError
from helpers.kubearchive import KubeArchiveAuthError
from helpers.runner import OLMInstallRunner

_HELP_DESCRIPTION = (
    "Konflux olminstall ITS helper: apply/patch ITS, trigger PipelineRuns, stream logs, "
    "list/watch runs, KubeArchive when pruned."
)

_HELP_EPILOG = """\
Tools: oc (required); tkn (optional, live logs during trigger mode); yq (repo/branch/channel/odh); skopeo (odh, optional).
Env: KONFLUX_UI, KA_HOST, KONFLUX_SERVER, PR_APPEAR_TIMEOUT_SECONDS — README / contributing doc.

Modes: default (trigger) OR --watch OR --list-pipelines OR --list-supported-ocp.
Trigger always creates a new PipelineRun; use --watch to stream an existing run.
Do not mix trigger flags (--image, --version, …) with --watch/--list* (except --ocp-version with --list-supported-ocp).

Examples:
  %(prog)s --watch                       # newest olminstall for --app (same merge order as --list)
  %(prog)s --watch odh-olminstall-testops-xyz
  %(prog)s --list
  %(prog)s --list-supported-ocp --ocp-version 4.19
  %(prog)s --tests bvt
  %(prog)s --tests smoke
  %(prog)s --product rhoai --version 3.5
  %(prog)s --tests bvt,smoke,tier1
  %(prog)s --tests bvt --product rhoai --version 3.5
  %(prog)s --tests bvt --slack-channel-id C01234ABCDE
  %(prog)s --konflux-repo https://github.com/you/fork.git --konflux-branch your-branch

Exit codes: 0 ok, 1 error, 2 bad args, 130 interrupt."""


def main(argv: list[str] | None = None) -> int:
    argv_list = argv if argv is not None else sys.argv[1:]
    parser = make_parser(_HELP_DESCRIPTION, _HELP_EPILOG)
    if not argv_list:
        parser.print_help()
        return 0
    try:
        args = parse_cli_args(parser, argv_list)
        runner = OLMInstallRunner(args)
        atexit.register(runner.cleanup)
        return runner.run()
    except KeyboardInterrupt:
        if "runner" in locals():
            runner.mark_detached_from_logs()
        return 130
    except AppError as exc:
        emit_click_style_error(parser, str(exc), usage=(exc.code == 2))
        return exc.code
    except KubeArchiveAuthError as exc:
        msg = (
            f"{exc}\n"
            "Re-authenticate against the Konflux cluster with the same kubeconfig you use for `oc`, "
            "then retry (for example: `KUBECONFIG=… oc login --server=<api> --web`)."
        )
        emit_click_style_error(parser, msg, usage=False)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
