#!/usr/bin/env python3
"""Konflux OLM pipeline CLI — rhoai-e2e ITS helper. Run with no args or ``-h`` for usage."""

from __future__ import annotations

import atexit
import sys
from pathlib import Path

_RHOAI_E2E_DIR = Path(__file__).resolve().parent
from _bootstrap import ensure_rhoai_e2e_path

ensure_rhoai_e2e_path()

from runners.cli.cli import emit_click_style_error, make_parser, parse_cli_args
from suite.errors import AppError
from k8s.kubearchive import KubeArchiveAuthError
from runners.cli.runner import RhoaiE2ERunner

_HELP_DESCRIPTION = (
    "Konflux rhoai-e2e helper: enable/disable IntegrationTestScenario, patch ITS for triggers, "
    "create PipelineRuns, stream logs, list/watch runs, and fetch KubeArchive when pruned."
)

_HELP_EPILOG = """\
Tools: oc (required); tkn (optional, live logs in trigger mode); yq (ITS patches); skopeo (odh, optional).
Env: KONFLUX_UI, KA_HOST, KONFLUX_SERVER, PR_APPEAR_TIMEOUT_SECONDS — see README.

Run rules:
  • Default = trigger (always creates a new PipelineRun).
  • Default test-only (omit --product) without --external-kubeconfig: BVT placeholder only; smoke needs a cluster.
  • Default --tests bvt,smoke test-only: pass --external-kubeconfig for component smoke.
  • -w / --watch, -l / --list-pipelines, --delete-pending-pipelines, --cleanup, --enable-its, --disable-its, --run-its, --list-components = Konflux query/maintenance (pick one).
  • --enable-its / --disable-its apply or remove an in-tree IntegrationTestScenario by name (uses --konflux-namespace / --konflux-app).
  • --enable-its applies spec.application from the ITS manifest; pass --konflux-app to patch. Only Konflux rollout flags allowed.
  • --run-its NAME: one-shot debug PipelineRun from ITS manifest params (cluster/test overrides allowed; no ITS apply).
  • --list-supported-ocp = supported OCP query (product & catalog; pick alone or with --ocp-version).
  • --list-components = smoke component catalog query (reads config/rhoai-e2e-components-smoke.yaml).
  • Do not mix trigger-only flags with -w, -l, --delete-pending-pipelines, --enable-its, --disable-its, --run-its, --list-supported-ocp, or --list-components.
  • --ocp-version may accompany --list-supported-ocp or a trigger run.

Examples:
  %(prog)s                                     # trigger with defaults
  %(prog)s -w                                  # watch newest run for --konflux-app
  %(prog)s -w e2e-cli-nmanos-ephc-rhoai-smoke-xyz
  %(prog)s -l                                  # list last 10 runs
  %(prog)s --delete-pending-pipelines          # stop stuck/incomplete live runs
  %(prog)s --delete-pending-pipelines --dry-run
  %(prog)s --enable-its rhoai-e2e-rh-nightly-pm-ocp420
  %(prog)s --run-its rhoai-e2e-ephc-playpen-a --tests smoke --components all
  %(prog)s --enable-its rhoai-e2e-ephc-ocp421
  %(prog)s --disable-its rhoai-e2e-rh-nightly-pm-ocp420
  %(prog)s --list-supported-ocp --ocp-version 4.19
  %(prog)s --tests bvt
  %(prog)s --tests smoke --components workbenches
  %(prog)s --product rhoai --rhoai-version 3.5
  %(prog)s --tests bvt,smoke,tier1
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
        args.trigger_argv = list(argv_list)
        runner = RhoaiE2ERunner(args)
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
