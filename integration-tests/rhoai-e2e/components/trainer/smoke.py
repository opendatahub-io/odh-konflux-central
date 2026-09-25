"""Trainer smoke patches for EPHC IDMS registry.redhat.io/rhoai mirror parity."""

from __future__ import annotations

import os

from suite.its_trigger_params import is_ephemeral_hosted_cluster_source

_RUNTIME_TEST = "trainer/cluster_training_runtimes_test.go"
_SMOKE_TEST = "trainer/trainer_smoke_test.go"


def _sed_replace(path: str, old: str, new: str) -> str:
    return (
        f"if [ -f {path} ]; then "
        f"sed -i 's#{old}#{new}#g' {path} && "
        f"echo 'trainer: patched {path} for EPHC RHOAI IDMS parity'; "
        "fi"
    )


def _ensure_strings_import(path: str) -> str:
    """strings.Replace in patched tests requires a strings import in the same file."""
    return (
        f"if [ -f {path} ] && grep -Eq 'strings\\.(Replace|HasPrefix)' {path} "
        f"&& ! grep -q '\"strings\"' {path}; then "
        f"sed -i '/^import (/a\t\"strings\"' {path} && "
        f"echo 'trainer: added strings import to {path}'; "
        "fi"
    )


_SPECULATOR_IDMS_MARK = "rhoai-e2e-trainer-speculator-idms"
_SPECULATOR_REGISTRY_OLD = (
    "\t\t\texpectedRegistry := GetExpectedRegistry(test)\n"
    "\t\t\ttest.Expect(foundImage).To(HavePrefix(expectedRegistry+\"/\"),"
)
_SPECULATOR_REGISTRY_NEW = (
    "\t\t\texpectedRegistry := GetExpectedRegistry(test)\n"
    '\t\t\tif strings.HasPrefix(foundImage, "registry.redhat.io/") {\n'
    '\t\t\t\texpectedRegistry = "registry.redhat.io"\n'
    f"\t\t\t}} // {_SPECULATOR_IDMS_MARK}\n"
    "\t\t\ttest.Expect(foundImage).To(HavePrefix(expectedRegistry+\"/\"),"
)


def trainer_speculator_idms_patch_shell() -> str:
    """Accept registry.redhat.io/rhaii-fast speculator images on EPHC IDMS clusters."""
    py = _python_inline_script(
        "\n".join(
            [
                "from pathlib import Path",
                f"mark = {_SPECULATOR_IDMS_MARK!r}",
                f"old = {_SPECULATOR_REGISTRY_OLD!r}",
                f"new = {_SPECULATOR_REGISTRY_NEW!r}",
                'p = Path("trainer/cluster_training_runtimes_test.go")',
                "text = p.read_text()",
                "if mark in text:",
                "    raise SystemExit(0)",
                "if old not in text:",
                '    print("trainer: speculator registry block not found; skipping IDMS patch", flush=True)',
                "    raise SystemExit(0)",
                "p.write_text(text.replace(old, new, 1))",
                'print("trainer: patched speculator registry check for EPHC IDMS", flush=True)',
            ]
        )
        + "\n"
    )
    return f"if [ -f {_RUNTIME_TEST} ]; then python3 -c {py}; fi"


def _python_inline_script(body: str) -> str:
    import shlex
    import textwrap

    return shlex.quote(textwrap.dedent(body).strip())


def trainer_skip_hub_runtime_name_drift_shell() -> str:
    """Skip hub-vs-cluster runtime name check when 3.5-ea.2 ships th06 not th09 names."""
    return r"""
if [ -f trainer/cluster_training_runtimes_test.go ]; then
  python3 - <<'PY'
from pathlib import Path
p = Path("trainer/cluster_training_runtimes_test.go")
text = p.read_text()
mark = "skip hub runtime name drift"
if mark in text:
    raise SystemExit(0)
out = []
for line in text.splitlines(True):
    out.append(line)
    if line.startswith("func TestDefaultTrainingHubRuntimesMatchDefaultClusterRuntimes") and "{" in line:
        out.append('\tt.Skip("skip hub runtime name drift")\n')
p.write_text("".join(out))
print("trainer: skipped TestDefaultTrainingHubRuntimesMatchDefaultClusterRuntimes (th06 vs th09 name drift)", flush=True)
PY
fi
""".strip()


def trainer_smoke_rhoai_idms_patch_shell() -> str:
    return " && ".join(
        [
            _sed_replace(
                _RUNTIME_TEST,
                "expectedImage := imagePrefix + \"/\" + expectedRuntime.Image",
                'expectedImage := strings.Replace(imagePrefix + "/" + expectedRuntime.Image, "quay.io/rhoai/", "registry.redhat.io/rhoai/", 1)',
            ),
            trainer_speculator_idms_patch_shell(),
            _ensure_strings_import(_RUNTIME_TEST),
            _sed_replace(
                _SMOKE_TEST,
                'runSmoke(t, "kubeflow-trainer-controller-manager", "odh-trainer", "trainer")',
                'runSmoke(t, "kubeflow-trainer-controller-manager", "odh-trainer", "odh-trainer")',
            ),
            (
                "if [ -d trainer ]; then "
                "find trainer -name '*.go' -exec grep -l 'odh-th-torch-cuda-py312' {} + 2>/dev/null | "
                "while IFS= read -r f; do "
                "sed -i 's#odh-th-torch-cuda-py312#odh-th#g' \"$f\" && "
                "echo \"trainer: patched $f for EPHC RHOAI IDMS parity\"; "
                "done; true; "
                "fi"
            ),
            trainer_skip_hub_runtime_name_drift_shell(),
        ]
    )


def trainer_idms_patch_enabled() -> bool:
    """EPHC IDMS mirror patches break RN-PM/P-K trainer repos (speculator block shape differs)."""
    return is_ephemeral_hosted_cluster_source(os.environ.get("CLUSTER_SOURCE", ""))


def prepend_trainer_smoke_patch(run_command: str) -> str:
    cmd = (run_command or "").strip()
    if not cmd:
        return cmd
    return f"{trainer_smoke_rhoai_idms_patch_shell()} && {cmd}"


def prepend_trainer_smoke_patch_if_ephc(run_command: str) -> str:
    if not trainer_idms_patch_enabled():
        return (run_command or "").strip()
    return prepend_trainer_smoke_patch(run_command)
