"""KubeRay RHOAI image test patches for EPHC IDMS registry.redhat.io mirror parity."""

from __future__ import annotations

import re
import shlex
import textwrap

_IMAGES_TEST = "test/e2e/raycluster_rhoai_images_test.go"
_IDMS_MARK = "olminstall-kuberay-idms"
_KUBE_RBAC_MSG = (
    "injected kube-rbac-proxy sidecar should use RELATED_IMAGE_ODH_KUBE_RBAC_PROXY_IMAGE"
)


def _python_inline_script(body: str) -> str:
    return shlex.quote(textwrap.dedent(body).strip())


def _rhoai_idms_patch_python_body() -> str:
    return "\n".join(
        [
            "import re",
            "from pathlib import Path",
            f"mark = {_IDMS_MARK!r}",
            f"needle = {_KUBE_RBAC_MSG!r}",
            f"p = Path({_IMAGES_TEST!r})",
            "text = p.read_text()",
            "if mark in text:",
            "    raise SystemExit(0)",
            "if needle not in text:",
            '    print("kuberay: skip IDMS patch (kube-rbac-proxy assertion not found)", flush=True)',
            "    raise SystemExit(0)",
            "lines = text.splitlines(True)",
            "out = []",
            "patched = False",
            "for line in lines:",
            "    if needle in line and not patched:",
            "        m = re.search(r'Equal\\(([^)]+)\\)', line)",
            "        if m:",
            "            var = m.group(1).strip()",
            "            indent = line[: len(line) - len(line.lstrip())]",
            "            out.append(",
            '                f\'{indent}{var} = strings.Replace({var}, "registry.redhat.io/", "quay.io/", 1) // {mark}\\n\'',
            "            )",
            "            patched = True",
            "    out.append(line)",
            'text = "".join(out)',
            'if patched and \'"strings"\' not in text and "strings.Replace" in text:',
            '    text = text.replace("import (\\n", \'import (\\n\\t"strings"\\n\', 1)',
            "p.write_text(text)",
            'print("kuberay: patched TestRayClusterRHOAIImages for EPHC IDMS mirror", flush=True)',
        ]
    ) + "\n"


def kuberay_rhoai_idms_patch_shell() -> str:
    """Normalize registry.redhat.io sidecar images before RELATED_IMAGE comparison on EPHC."""
    py = _python_inline_script(_rhoai_idms_patch_python_body())
    return f"if [ -f {_IMAGES_TEST} ]; then python3 -c {py}; fi"


def prepend_kuberay_smoke_patch(run_command: str) -> str:
    cmd = (run_command or "").strip()
    if not cmd:
        return cmd
    from components.kuberay.auth_options import kuberay_skip_auth_options_if_crd_missing_shell

    patches = " && ".join([
        kuberay_skip_auth_options_if_crd_missing_shell(),
        kuberay_rhoai_idms_patch_shell(),
    ])
    return f"{patches} && {cmd}"
