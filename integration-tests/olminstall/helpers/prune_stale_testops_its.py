#!/usr/bin/env python3
"""Delete legacy IntegrationTestScenario CRs before raw ``oc create -f test-snapshot.yaml``.

Deletes ``STALE_TESTOPS_PLAYPEN_ITS_NAMES`` (``helpers/constants.py``) — the same set
``OLMInstallRunner.prune_stale_integration_test_scenarios`` removes before Snapshot create.

Exit code 1 if any ``oc delete`` returned non-zero (best-effort per object; failures are surfaced).

Usage:
  python3 integration-tests/olminstall/helpers/prune_stale_testops_its.py
  python3 integration-tests/olminstall/helpers/prune_stale_testops_its.py -n my-tenant
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

_OLMINSTALL_DIR = Path(__file__).resolve().parent.parent
if str(_OLMINSTALL_DIR) not in sys.path:
    sys.path.insert(0, str(_OLMINSTALL_DIR))

from helpers.constants import DEFAULT_NAMESPACE, STALE_TESTOPS_PLAYPEN_ITS_NAMES
from helpers.oc_util import filter_warning_lines, run_cmd


def delete_stale_testops_playpen_integration_test_scenarios(namespace: str) -> int:
    """``oc delete`` each name in ``STALE_TESTOPS_PLAYPEN_ITS_NAMES`` (ignore-not-found).

    Returns the number of ``oc delete`` invocations that exited non-zero (warnings printed).
    """
    failures = 0
    for name in sorted(STALE_TESTOPS_PLAYPEN_ITS_NAMES):
        proc = run_cmd(
            ["oc", "delete", "integrationtestscenario", name, "-n", namespace, "--ignore-not-found"],
            capture=True,
            check=False,
        )
        if proc.returncode != 0:
            failures += 1
            msg = filter_warning_lines(f"{proc.stdout}\n{proc.stderr}").strip()
            print(f"WARN oc delete integrationtestscenario/{name}: {msg or proc.returncode}", file=sys.stderr)
    return failures


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    p.add_argument(
        "-n",
        "--namespace",
        default=DEFAULT_NAMESPACE,
        help=f"Kubernetes namespace (default: {DEFAULT_NAMESPACE})",
    )
    args = p.parse_args(argv)
    ns = args.namespace.strip()
    if not ns:
        print("ERROR: empty namespace", file=sys.stderr)
        return 2
    stale = sorted(STALE_TESTOPS_PLAYPEN_ITS_NAMES)
    if not stale:
        print("Nothing to prune (STALE_TESTOPS_PLAYPEN_ITS_NAMES is empty).")
        return 0
    print(f"Deleting legacy IntegrationTestScenario objects in namespace {ns!r}: {', '.join(stale)}")
    failures = delete_stale_testops_playpen_integration_test_scenarios(ns)
    if failures:
        print(f"ERROR: {failures} oc delete command(s) failed (see stderr above).", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
