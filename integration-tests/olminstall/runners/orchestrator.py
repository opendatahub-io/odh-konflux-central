"""Orchestrate cluster prep before component pytest runs."""

from __future__ import annotations

import os
import platform
import shutil
import subprocess
import sys
from pathlib import Path

from _bootstrap import ensure_olminstall_path

ensure_olminstall_path()

from install.gateway_config import wait_gateway_config_ready
from k8s.vault_runtime import ensure_runtime_vault_env
from k8s.shift_left_env import load_shift_left_env_from_mount
from runners.component_prereqs import prepare_components_for_smoke
from runners.selection import selected_component_ids
from steps.cluster_prep_state import mark_cluster_prep_done

_GIT_CORE_HELPERS = (
    "git",
    "git-remote",
    "git-remote-http",
    "git-remote-https",
)


def _stage_git_core_helpers(source_git: str, bindir: Path) -> Path | None:
    proc = subprocess.run(
        [source_git, "--exec-path"],
        check=False,
        capture_output=True,
        text=True,
    )
    if proc.returncode != 0:
        return None
    exec_src = Path((proc.stdout or "").strip())
    if not exec_src.is_dir():
        return None
    exec_dest = bindir / "git-core"
    exec_dest.mkdir(parents=True, exist_ok=True)
    staged = 0
    for name in _GIT_CORE_HELPERS:
        helper_src = exec_src / name
        if not helper_src.is_file():
            continue
        helper_dest = exec_dest / name
        shutil.copy2(helper_src, helper_dest)
        helper_dest.chmod(0o755)
        staged += 1
    if staged == 0:
        return None
    return exec_dest


def stage_git_for_prereqs() -> None:
    """Stage ``git`` under tests-payload/.tools/bin for opendatahub-tests pytest steps."""
    artifacts = Path(os.environ.get("ARTIFACTS_DIR", "/artifacts").strip() or "/artifacts")
    from steps.tests_payload import (resolve_tests_payload_root,
                                     tests_payload_tools_bin_dir)

    bindir = tests_payload_tools_bin_dir(resolve_tests_payload_root(artifacts))
    bindir.mkdir(parents=True, exist_ok=True)
    dest = bindir / "git"
    git_core = bindir / "git-core"
    if dest.is_file() and (git_core / "git-remote-https").is_file():
        os.environ.setdefault("GIT_EXEC_PATH", str(git_core))
        return
    source = shutil.which("git") or "/usr/bin/git"
    if not Path(source).is_file():
        print(f"WARN: cannot stage git for pytest — {source} missing", file=sys.stderr, flush=True)
        return
    shutil.copy2(source, dest)
    dest.chmod(0o755)
    exec_dest = _stage_git_core_helpers(source, bindir)
    if exec_dest is not None:
        os.environ["GIT_EXEC_PATH"] = str(exec_dest)
        print(f"✓ Staged git core helpers at {exec_dest}", flush=True)
    print(f"✓ Staged git for pytest at {dest}", flush=True)


def stage_jq_for_prereqs() -> None:
    """Stage ``jq`` for ods-install identity-provider scripts in component pytest images."""
    if shutil.which("jq"):
        return
    artifacts = Path(os.environ.get("ARTIFACTS_DIR", "/artifacts").strip() or "/artifacts")
    from steps.tests_payload import (resolve_tests_payload_root,
                                     tests_payload_tools_bin_dir)

    bindir = tests_payload_tools_bin_dir(resolve_tests_payload_root(artifacts))
    bindir.mkdir(parents=True, exist_ok=True)
    _stage_tool_binary(bindir, "jq", sources=("/usr/bin/jq", "/usr/local/bin/jq"))
    if shutil.which("jq"):
        return
    staged = bindir / "jq"
    if staged.is_file():
        os.environ["PATH"] = f"{bindir}:{os.environ.get('PATH', '')}"


def stage_oc_for_pytest() -> None:
    """Stage ``oc`` under tests-payload/.tools/bin (not uploaded with JUnit/logs)."""
    artifacts = Path(os.environ.get("ARTIFACTS_DIR", "/artifacts").strip() or "/artifacts")
    from steps.tests_payload import (resolve_tests_payload_root,
                                     tests_payload_tools_bin_dir)

    bindir = tests_payload_tools_bin_dir(resolve_tests_payload_root(artifacts))
    bindir.mkdir(parents=True, exist_ok=True)
    dest = bindir / "oc"
    if dest.is_file():
        return
    source = shutil.which("oc") or "/usr/bin/oc"
    if not Path(source).is_file():
        print(f"WARN: cannot stage oc for pytest — {source} missing", file=sys.stderr, flush=True)
        return
    shutil.copy2(source, dest)
    dest.chmod(0o755)
    print(f"✓ Staged oc for pytest at {dest}", flush=True)


def prepare_oc_binary_path_for_pytest() -> None:
    """Stage oc and set OC_BINARY_PATH so opendatahub-tests skips console CLI download.

    Tekton pods often cannot resolve downloads-openshift-console.apps.* on OCI ephemeral
    guests (*.konflux-ocp-ci.dev). BVT already used this; component smoke must too.
    """
    artifacts = Path(os.environ.get("ARTIFACTS_DIR", "/artifacts").strip() or "/artifacts")
    os.environ.setdefault("ARTIFACTS_DIR", str(artifacts))
    from steps.tests_payload import (resolve_tests_payload_root,
                                     tests_payload_tools_bin_dir)

    stage_oc_for_pytest()
    tools_bin = tests_payload_tools_bin_dir(resolve_tests_payload_root(artifacts))
    if tools_bin.is_dir():
        os.environ["PATH"] = f"{tools_bin}:{os.environ.get('PATH', '')}"
    staged_oc = tools_bin / "oc"
    if staged_oc.is_file():
        os.environ["OC_BINARY_PATH"] = str(staged_oc)
    else:
        bundled = shutil.which("oc")
        if bundled:
            os.environ.setdefault("OC_BINARY_PATH", bundled)


def _tool_binary_runs(path: Path) -> bool:
    if not path.is_file():
        return False
    try:
        proc = subprocess.run(
            [str(path), "--version"],
            check=False,
            capture_output=True,
            text=True,
            timeout=15,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    combined = (proc.stdout or "") + (proc.stderr or "")
    return proc.returncode == 0 and "error while loading shared libraries" not in combined


_JQ_RELEASES: dict[str, tuple[str, str]] = {
    "x86_64": (
        "jq-linux-amd64",
        "5942c9b0934e510ee61eb3e30273f1b3fe2590df93933a93d7c58b81d19c8ff5",
    ),
    "amd64": (
        "jq-linux-amd64",
        "5942c9b0934e510ee61eb3e30273f1b3fe2590df93933a93d7c58b81d19c8ff5",
    ),
    "aarch64": (
        "jq-linux-arm64",
        "4dd2d8a0661df0b22f1bb9a1f9830f06b6f3b8f7d91211a1ef5d7c4f06a8b4a5",
    ),
    "arm64": (
        "jq-linux-arm64",
        "4dd2d8a0661df0b22f1bb9a1f9830f06b6f3b8f7d91211a1ef5d7c4f06a8b4a5",
    ),
}


def _stage_static_jq(bindir: Path) -> bool:
    """Fetch a portable jq when the staged copy from another image cannot execute."""
    import hashlib
    import urllib.request

    release = _JQ_RELEASES.get(platform.machine().lower())
    if not release:
        print(
            f"WARN: no static jq release for arch {platform.machine()}",
            file=sys.stderr,
            flush=True,
        )
        return False

    binary, expected_sha = release
    dest = bindir / "jq"
    url = f"https://github.com/jqlang/jq/releases/download/jq-1.7.1/{binary}"
    try:
        with urllib.request.urlopen(url, timeout=120) as resp:
            payload = resp.read()
        digest = hashlib.sha256(payload).hexdigest()
        if digest != expected_sha:
            print(
                f"WARN: downloaded jq checksum mismatch (expected {expected_sha}, got {digest})",
                file=sys.stderr,
                flush=True,
            )
            return False
        dest.write_bytes(payload)
        dest.chmod(0o755)
    except OSError as exc:
        print(f"WARN: could not download static jq: {exc}", file=sys.stderr, flush=True)
        return False
    return _tool_binary_runs(dest)


def _stage_tool_binary(bindir: Path, name: str, *, sources: tuple[str, ...]) -> None:
    dest = bindir / name
    if _tool_binary_runs(dest):
        return
    if dest.is_file():
        dest.unlink()
    candidates = tuple(dict.fromkeys(p for p in (shutil.which(name), *sources) if p))
    for candidate in candidates:
        path = Path(candidate)
        if path.is_file():
            shutil.copy2(path, dest)
            dest.chmod(0o755)
            if _tool_binary_runs(dest):
                print(f"✓ Staged {name} for component tests at {dest}", flush=True)
                return
            dest.unlink()
    if name == "jq" and _stage_static_jq(bindir):
        print(f"✓ Staged static jq for component tests at {dest}", flush=True)
        return
    print(f"WARN: cannot stage {name} — not found in {sources!r}", file=sys.stderr, flush=True)


def stage_which_shim() -> None:
    """Stage a minimal ``which`` for Cypress cy.exec (tenant images often omit it)."""
    artifacts = Path(os.environ.get("ARTIFACTS_DIR", "/artifacts").strip() or "/artifacts")
    from steps.tests_payload import (resolve_tests_payload_root,
                                     tests_payload_tools_bin_dir)

    bindir = tests_payload_tools_bin_dir(resolve_tests_payload_root(artifacts))
    bindir.mkdir(parents=True, exist_ok=True)
    dest = bindir / "which"
    if dest.is_file():
        return
    dest.write_text(
        "#!/bin/sh\n"
        'if [ "$#" -lt 1 ]; then exit 1; fi\n'
        'command -v "$1"\n',
        encoding="utf-8",
    )
    dest.chmod(0o755)
    print(f"✓ Staged which shim for component tests at {dest}", flush=True)


def _pyyaml_staged(target: Path) -> bool:
    return (target / "yaml" / "__init__.py").is_file()


def _remove_staged_pyyaml_binaries(target: Path) -> None:
    """Drop stale _yaml artifacts (pip --target may leave a directory, not only .so)."""
    for entry in target.glob("_yaml*"):
        if entry.is_dir():
            shutil.rmtree(entry)
        else:
            entry.unlink(missing_ok=True)


def stage_cypress_python_tools() -> None:
    """Stage PyYAML under tests-payload for the Cypress image (no pip / dnf as non-root)."""
    artifacts = Path(os.environ.get("ARTIFACTS_DIR", "/artifacts").strip() or "/artifacts")
    from steps.tests_payload import (resolve_tests_payload_root,
                                     tests_payload_tools_python_dir)

    target = tests_payload_tools_python_dir(resolve_tests_payload_root(artifacts))
    if _pyyaml_staged(target):
        _remove_staged_pyyaml_binaries(target)
        return
    try:
        import yaml  # noqa: F401
    except ImportError:
        print(
            "WARN: PyYAML not available to stage for Cypress; "
            "run-cypress may fail in images without pip",
            file=sys.stderr,
            flush=True,
        )
        return
    import yaml as yaml_mod

    target.mkdir(parents=True, exist_ok=True)
    yaml_pkg = Path(yaml_mod.__file__).resolve().parent
    shutil.copytree(yaml_pkg, target / "yaml", dirs_exist_ok=True)
    # Do not copy _yaml.so: orchestrate and Cypress images often differ in Python ABI.
    _remove_staged_pyyaml_binaries(target)
    print(f"✓ Staged PyYAML for Cypress at {target}", flush=True)


def stage_cypress_cli_tools() -> None:
    """Stage oc/jq/which for dashboard Cypress (runtime image lacks preinstalled CLI tools)."""
    artifacts = Path(os.environ.get("ARTIFACTS_DIR", "/artifacts").strip() or "/artifacts")
    from steps.tests_payload import (resolve_tests_payload_root,
                                     tests_payload_tools_bin_dir)

    bindir = tests_payload_tools_bin_dir(resolve_tests_payload_root(artifacts))
    bindir.mkdir(parents=True, exist_ok=True)
    stage_oc_for_pytest()
    _stage_tool_binary(bindir, "jq", sources=("/usr/bin/jq", "/usr/local/bin/jq"))
    stage_which_shim()
    stage_cypress_python_tools()


def prepare_cluster_for_components(*, collect_only: bool = False) -> None:
    """Shared prepare work that must not block component tasks on per-component failures."""
    if collect_only:
        return
    ensure_runtime_vault_env()
    load_shift_left_env_from_mount()
    # Ensure Kuadrant/RHCL gateway is ready before component tests (especially dashboard_cypress, ogx).
    # This prevents 503 errors from /v1/health endpoint during Cypress auth checks.
    if not wait_gateway_config_ready(timeout_sec=300):
        print(
            "WARN: GatewayConfig not ready within timeout; component tests may encounter auth failures",
            file=sys.stderr,
            flush=True,
        )
    artifacts = Path(os.environ.get("ARTIFACTS_DIR", "/artifacts").strip() or "/artifacts")
    ids = selected_component_ids()
    if ids:
        prep_ok = prepare_components_for_smoke(ids)
        if prep_ok:
            mark_cluster_prep_done(artifacts)
        else:
            print(
                "WARN: cluster prep had failures; skipping .cluster-prep-done marker so retries can run",
                file=sys.stderr,
                flush=True,
            )
    stage_git_for_prereqs()
    stage_oc_for_pytest()


def main() -> int:
    from steps.cluster_prep_state import clear_cluster_prep_markers
    from steps.component_prep_track import (component_prep_log_prefix,
                                            record_component_prep_track)

    prefix = component_prep_log_prefix()
    track = record_component_prep_track()
    print(f"{prefix} component cluster prep starting (track={track})", flush=True)

    if os.environ.get("CLUSTER_PREP_FORCE", "").strip().lower() in ("1", "true", "yes"):
        clear_cluster_prep_markers()
    collect_only = os.environ.get("COMPONENT_TEST_COLLECT_ONLY", "").strip().lower() in (
        "1",
        "true",
        "yes",
    ) or os.environ.get("COMPONENT_SMOKE_COLLECT_ONLY", "").strip().lower() in (
        "1",
        "true",
        "yes",
    )
    try:
        prepare_cluster_for_components(collect_only=collect_only)
    except Exception as exc:
        print(
            f"WARN: component cluster prepare encountered an error ({exc}); "
            "component tasks will verify readiness individually",
            file=sys.stderr,
            flush=True,
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
