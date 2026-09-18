"""KFTO smoke patches for ROSA HCP / Kyverno quay.io/rhoai mirror (Jenkins registry parity)."""

from __future__ import annotations

import shlex
import textwrap

_KFTO_DIR = "kfto"
_COMMON_TAG = "common/test_tag.go"
_OLD_PREFIX_CHECK = 'strings.HasPrefix(imagePrefix, "quay.io")'
_NEW_PREFIX_CHECK = 'strings.HasPrefix(imagePrefix, "quay.io/opendatahub")'
_TIER_SENTINEL = "// olminstall-kfto-smoke-tier-parity"
_PYTORCH_TIMEOUT_SENTINEL = "// olminstall-kfto-pytorch-timeout-patch"

_MANDATORY_TIER_OLD = """
func mandatoryTestTier(test Test, expectedTestTier string) (runTest bool, skipReason string) {
\tactualTestTier, found := GetTestTier(test)
\tif found && actualTestTier == expectedTestTier {
\t\treturn true, ""
\t}
\treturn false, fmt.Sprintf("Test tier '%s' doesn't match expected tier '%s'", actualTestTier, expectedTestTier)
}
""".strip("\n")

_MANDATORY_TIER_NEW = f"""
func mandatoryTestTier(test Test, expectedTestTier string) (runTest bool, skipReason string) {{
\tactualTestTier, found := GetTestTier(test)
\tif found && actualTestTier == expectedTestTier {{
\t\treturn true, ""
\t}}
\t{_TIER_SENTINEL}
\tif found && actualTestTier == tierSmoke && (expectedTestTier == preUpgrade || expectedTestTier == postUpgrade) {{
\t\treturn true, ""
\t}}
\treturn false, fmt.Sprintf("Test tier '%s' doesn't match expected tier '%s'", actualTestTier, expectedTestTier)
}}
""".strip("\n")

_PYTORCH_TIMEOUT_OLD = "300 * time.Second"
_PYTORCH_TIMEOUT_NEW = "600 * time.Second  // olminstall-kfto-pytorch-timeout-patch (extended for cluster load)"


def _python_inline_script(body: str) -> str:
    """Shell-quoted multiline script for ``python3 -c`` inside Tekton ``bash -c "${RUN_COMMAND}"``."""
    return shlex.quote(textwrap.dedent(body).strip())


def kfto_smoke_rhoai_quay_patch_shell() -> str:
    """Return a shell fragment that patches bundled KFTO smoke sources before ``go test``."""
    quay = (
        f'if [ -f {_KFTO_DIR}/kfto_smoke_test.go ] && grep -Fq {_OLD_PREFIX_CHECK!r} {_KFTO_DIR}/kfto_smoke_test.go; then '
        f"sed -i 's#{_OLD_PREFIX_CHECK}#{_NEW_PREFIX_CHECK}#' {_KFTO_DIR}/kfto_smoke_test.go && "
        'echo "distributed_workloads: patched kfto_smoke_test.go for quay.io/rhoai RHOAI HCP mirror"; '
        "fi"
    )
    tier = kfto_smoke_tier_remap_shell()
    return f"{quay} && {tier}"


def _kfto_tier_patch_python_body() -> str:
    return "\n".join(
        [
            "from pathlib import Path",
            f"sentinel = {_TIER_SENTINEL!r}",
            f"old = {_MANDATORY_TIER_OLD!r}",
            f"new = {_MANDATORY_TIER_NEW!r}",
            f"p = Path({_COMMON_TAG!r})",
            "text = p.read_text()",
            "if sentinel in text:",
            "    raise SystemExit(0)",
            "if old not in text:",
            "    raise SystemExit('mandatoryTestTier block not found in common/test_tag.go')",
            "p.write_text(text.replace(old, new, 1))",
            "print('distributed_workloads: patched common/test_tag.go for Smoke tier KFTO parity', flush=True)",
        ]
    ) + "\n"


def _kfto_pytorch_timeout_patch_python_body() -> str:
    """Extend KFTO job condition timeout for cluster load (600s vs 300s)."""
    return "\n".join(
        [
            "from pathlib import Path",
            f"sentinel = {_PYTORCH_TIMEOUT_SENTINEL!r}",
            f"old = {_PYTORCH_TIMEOUT_OLD!r}",
            f"new = {_PYTORCH_TIMEOUT_NEW!r}",
            "for p in Path('.').rglob('*.go'):",
            "    if 'kfto' not in p.parts:",
            "        continue",
            "    text = p.read_text()",
            "    if sentinel in text:",
            "        continue",
            "    if old in text:",
            "        text = text.replace(old, new, 1)",
            "        p.write_text(text)",
            "        print(f'distributed_workloads: patched {p} for pytorch job timeout', flush=True)",
        ]
    ) + "\n"


def kfto_smoke_tier_remap_shell() -> str:
    """Let -testTier=Smoke run KFTO Pre/Post-Upgrade mandatory tier tests (Jenkins parity)."""
    py = _python_inline_script(_kfto_tier_patch_python_body())
    return f"if [ -f {_COMMON_TAG} ]; then python3 -c {py}; fi"


def kfto_smoke_pytorch_timeout_shell() -> str:
    """Extend job condition timeout from 300s to 600s for cluster load on rh-nightly-pm."""
    py = _python_inline_script(_kfto_pytorch_timeout_patch_python_body())
    return f"python3 -c {py}"


def prepend_kfto_smoke_patch(run_command: str) -> str:
    cmd = (run_command or "").strip()
    if not cmd:
        return cmd
    patches = " && ".join([
        kfto_smoke_rhoai_quay_patch_shell(),
        kfto_smoke_pytorch_timeout_shell(),
    ])
    return f"{patches} && {cmd}"
