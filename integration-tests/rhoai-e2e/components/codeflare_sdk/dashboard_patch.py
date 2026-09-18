"""Patch bundled run-tests.sh for gateway ConsoleLink gaps on external clusters."""

from __future__ import annotations

from pathlib import Path

from components.codeflare_sdk.ephc import prepend_codeflare_ephc_kubeconfig_auth

_RUN_TESTS = "run-tests.sh"
_OLD_PREFIX = "ODH_DASHBOARD_URL=$(oc get consolelink rhodslink"
_REPLACEMENT = (
    'if [ -n "${ODH_DASHBOARD_URL:-}" ]; then :; else '
    "ODH_DASHBOARD_URL=$(oc get consolelink rhodslink -o jsonpath='{.spec.href}' 2>/dev/null || true); fi"
)

_PATCH_HELPER = f'''#!/usr/bin/env python3
import sys
from pathlib import Path

OLD_PREFIX = {_OLD_PREFIX!r}
REPLACEMENT = {_REPLACEMENT!r}

def patch(path: Path) -> bool:
    text = path.read_text(encoding="utf-8")
    if OLD_PREFIX not in text:
        return False
    out = []
    patched = False
    for line in text.splitlines(keepends=True):
        stripped = line.rstrip("\\n\\r")
        if stripped.startswith(OLD_PREFIX):
            out.append(REPLACEMENT + "\\n")
            patched = True
        else:
            out.append(line)
    if patched:
        path.write_text("".join(out), encoding="utf-8")
    return patched

if __name__ == "__main__":
    target = Path(sys.argv[1] if len(sys.argv) > 1 else {_RUN_TESTS!r})
    if not target.is_file():
        raise SystemExit(0)
    if patch(target):
        print("codeflare: patched run-tests.sh dashboard URL lookup")
'''


def patch_run_tests_dashboard(path: Path) -> bool:
    """Replace rhodslink lookup line so missing ConsoleLink does not fail under ``set -e``."""
    text = path.read_text(encoding="utf-8")
    if _OLD_PREFIX not in text:
        return False
    out: list[str] = []
    patched = False
    for line in text.splitlines(keepends=True):
        stripped = line.rstrip("\n\r")
        if stripped.startswith(_OLD_PREFIX):
            out.append(_REPLACEMENT + "\n")
            patched = True
        else:
            out.append(line)
    if patched:
        path.write_text("".join(out), encoding="utf-8")
    return patched


def stage_codeflare_dashboard_patch_helper(artifacts_dir: Path) -> Path:
    helper = artifacts_dir / "codeflare_patch_run_tests_dashboard.py"
    helper.write_text(_PATCH_HELPER, encoding="utf-8")
    helper.chmod(0o755)
    return helper


def codeflare_run_tests_dashboard_patch_shell(artifacts_dir: Path) -> str:
    """Stage a helper under artifacts and invoke it from the component test container."""
    helper = stage_codeflare_dashboard_patch_helper(artifacts_dir)
    quoted = str(helper).replace("'", "'\"'\"'")
    return f"python3 '{quoted}' {_RUN_TESTS}"


def prepend_codeflare_dashboard_patch(
    run_command: str,
    *,
    dashboard_url: str = "",
    artifacts_dir: Path | None = None,
) -> str:
    cmd = (run_command or "").strip()
    if not cmd:
        return cmd
    parts: list[str] = []
    url = dashboard_url.strip()
    if url:
        quoted = url.replace("'", "'\"'\"'")
        parts.append(
            f"export ODH_DASHBOARD_URL='{quoted}' BASE_URL='{quoted}' DASHBOARD_URL='{quoted}'"
        )
    if artifacts_dir is not None:
        parts.append(codeflare_run_tests_dashboard_patch_shell(artifacts_dir))
    return " && ".join(parts + [cmd])


def prepend_codeflare_run_command_patches(
    run_command: str,
    *,
    dashboard_url: str = "",
    artifacts_dir: Path | None = None,
) -> str:
    cmd = prepend_codeflare_ephc_kubeconfig_auth(run_command)
    return prepend_codeflare_dashboard_patch(
        cmd,
        dashboard_url=dashboard_url,
        artifacts_dir=artifacts_dir,
    )
