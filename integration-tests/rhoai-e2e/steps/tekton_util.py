"""Shared utilities for Tekton pipeline step scripts.

Provides reusable functions that replace per-script boilerplate:
env-var reading, Tekton result writing, git cloning (with optional
Red Hat internal TLS workaround), subprocess execution, and JUnit
XML summary parsing.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any
from urllib.parse import urlparse
from xml.etree import ElementTree

_parse_junit_xml: Any = None


class DefusedXmlException(Exception):  # noqa: N818 — fallback when defusedxml absent
    """Raised for unsafe XML; replaced when defusedxml imports successfully."""


def _ensure_defusedxml() -> None:
    """Load defusedxml only for JUnit parsing (other helpers import tekton_util without it)."""
    global _parse_junit_xml, DefusedXmlException
    if _parse_junit_xml is not None:
        return

    def _import_defused() -> None:
        global _parse_junit_xml, DefusedXmlException
        from defusedxml.ElementTree import parse as defused_parse  # type: ignore[import-not-found]
        from defusedxml.common import DefusedXmlException as _DefusedXmlException  # type: ignore[import-not-found]

        _parse_junit_xml = defused_parse
        DefusedXmlException = _DefusedXmlException

    try:
        _import_defused()
    except ImportError:
        print("defusedxml not found; installing for JUnit parsing...", file=sys.stderr)
        pip = subprocess.run(
            [sys.executable, "-m", "pip", "install", "--no-cache-dir", "-q", "defusedxml>=0.7.1"],
            capture_output=True,
            text=True,
            check=False,
        )
        if pip.returncode == 0:
            try:
                _import_defused()
                return
            except ImportError as exc:
                print(f"defusedxml still unavailable after pip install: {exc}", file=sys.stderr)
        else:
            tail = ((pip.stdout or "") + (pip.stderr or "")).strip()[:2000]
            print(
                f"defusedxml install failed; falling back to stdlib XML parser: {tail or pip.returncode}",
                file=sys.stderr,
            )
        print("WARN: using xml.etree.ElementTree fallback for JUnit parsing", file=sys.stderr)
        _parse_junit_xml = ElementTree.parse
        DefusedXmlException = ElementTree.ParseError


def require_env(name: str, default: str | None = None) -> str:
    """Return env var *name* (stripped). Exits non-zero when missing and no *default*."""
    v = resolved_tekton_env_value(os.environ.get(name, ""))
    if v:
        return v
    if default is not None:
        return default
    print(f"Required environment variable is missing: {name}", file=sys.stderr)
    sys.exit(1)


def resolved_tekton_env_value(value: str) -> str:
    """Return empty when *value* is an unsubstituted Tekton ``$(...)`` placeholder."""
    v = (value or "").strip()
    if v.startswith("$(") and v.endswith(")"):
        return ""
    return v


_TEKTON_RESULTS_ROOT = Path("/tekton/results")


def _allowed_tekton_result_roots() -> list[Path]:
    roots = [_TEKTON_RESULTS_ROOT.resolve()]
    extra = os.environ.get("TEKTON_RESULTS_DIR", "").strip()
    if extra:
        roots.append(Path(extra).resolve())
    return roots


def _is_allowed_tekton_result_path(target: Path) -> bool:
    """True if *target* is a Tekton step result file path (not arbitrary host paths)."""
    for root in _allowed_tekton_result_roots():
        try:
            target.relative_to(root)
            return True
        except ValueError:
            continue
    parts = target.parts
    try:
        tekton_idx = parts.index("tekton")
    except ValueError:
        return False
    rest = parts[tekton_idx + 1 :]
    # Tekton v1 step results: /tekton/run/<id>/status/results/<name>
    return len(rest) >= 4 and rest[0] == "run" and rest[2] == "status" and rest[3] == "results"


# Tekton step / task results are capped at 4096 bytes (termination message includes them).
_TEKTON_RESULT_MAX_BYTES = 3800
# Entire Task termination message includes every result key on the task (not per-result).
_TEKTON_TASK_RESULTS_BUDGET_BYTES = 4096
# Per-step termination JSON (all result files present after the step) is ~2048 bytes on Konflux.
_TEKTON_STEP_TERMINATION_BUDGET_BYTES = 2048


def clamp_tekton_result(value: str, max_bytes: int = _TEKTON_RESULT_MAX_BYTES) -> str:
    """Truncate *value* so UTF-8 encoded size is at most *max_bytes*."""
    raw = (value or "").encode("utf-8", errors="replace")
    if len(raw) <= max_bytes:
        return value
    return raw[: max_bytes - 1].decode("utf-8", errors="ignore") + "…"


def write_result_or_path(path: str | Path, value: str) -> None:
    """Write Tekton results to allowed paths; otherwise write a regular workspace file."""
    target = Path(path).resolve()
    if _is_allowed_tekton_result_path(target):
        write_result(path, value)
        return
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(clamp_tekton_result(value), encoding="utf-8")


def write_result(path: str | Path, value: str) -> None:
    """Write *value* to a Tekton result file (no trailing newline).

    Refuses paths outside Tekton result directories (``/tekton/results``, ``TEKTON_RESULTS_DIR``,
    or ``/tekton/run/*/status/results/*``).
    """
    target = Path(path).resolve()
    if not _is_allowed_tekton_result_path(target):
        print(
            f"Refusing to write Tekton result outside allowed directories: {target}",
            file=sys.stderr,
        )
        sys.exit(1)
    target.write_text(clamp_tekton_result(value), encoding="utf-8")


# publish-results declares many results; Tekton packs them into one ~4096 B termination JSON.
_PUBLISH_RESULTS_RESULT_PRIORITY: tuple[str, ...] = (
    "TEST_OUTPUT",
    "TASK_MESSAGE",
    "TESTS_SUMMARY",
    "BVT_GATE",
    "SMOKE_GATE",
    "CLUSTER",
    "OPERATOR_VERSION",
    "FBCF_IMAGE",
    "TIER1_GATE",
    "ARTIFACTS_URL",
)

PUBLISH_GATE_SUMMARY_PATH_ENVS: tuple[tuple[str, str], ...] = (
    ("TESTS_SUMMARY", "TESTS_SUMMARY_PATH"),
    ("BVT_GATE", "BVT_GATE_PATH"),
    ("SMOKE_GATE", "SMOKE_GATE_PATH"),
)


def tekton_result_paths_from_env(
    name_env_pairs: tuple[tuple[str, str], ...],
) -> dict[str, str]:
    """Map Tekton result names to ``$(results.*.path)`` files from step env vars."""
    out: dict[str, str] = {}
    for name, env_name in name_env_pairs:
        raw = os.environ.get(env_name, "").strip()
        if raw and "$(" not in raw:
            out[name] = raw
    return out


def tekton_task_results_payload_size(results: dict[str, str]) -> int:
    """UTF-8 size of the JSON object Tekton stores for task results."""
    return len(json.dumps(results, separators=(",", ":")).encode("utf-8"))


def tekton_step_termination_payload_size(results: dict[str, str]) -> int:
    """UTF-8 size of Tekton's per-step ``[{key,value,type}, ...]`` termination JSON."""
    payload = [{"key": key, "value": value, "type": 1} for key, value in results.items()]
    return len(json.dumps(payload, separators=(",", ":")).encode("utf-8"))


def tekton_results_termination_payload_size(results: dict[str, str]) -> int:
    """Size Tekton uses when rejecting oversized task/step termination messages."""
    return max(
        tekton_task_results_payload_size(results),
        tekton_step_termination_payload_size(results),
    )


def slim_test_output_for_tekton(raw: str) -> str:
    """Drop suites[] from TEST_OUTPUT JSON so task termination stays under budget."""
    text = (raw or "").strip()
    if not text.lstrip().startswith("{"):
        return text
    try:
        obj = json.loads(text)
    except json.JSONDecodeError:
        return text
    if not isinstance(obj, dict) or "suites" not in obj:
        return text
    slim = {key: value for key, value in obj.items() if key != "suites"}
    return json.dumps(slim, separators=(",", ":"))


def tekton_results_root(directory: str | Path | None = None) -> Path:
    """Directory holding Tekton task result files for the current pod."""
    if directory is not None:
        return Path(directory)
    env_dir = os.environ.get("TEKTON_RESULTS_DIR", "").strip()
    if env_dir:
        return Path(env_dir)
    return Path("/tekton/results")


def read_tekton_task_result_files(
    directory: str | Path | None = None,
) -> dict[str, str]:
    """Read current task-level Tekton results from ``/tekton/results`` (or *directory*)."""
    root = tekton_results_root(directory)
    if not root.is_dir():
        return {}
    out: dict[str, str] = {}
    for path in sorted(root.iterdir()):
        if not path.is_file():
            continue
        try:
            val = path.read_text(encoding="utf-8").strip()
        except OSError:
            continue
        if val:
            out[path.name] = val
    return out


def read_tekton_results_at_paths(paths: dict[str, str]) -> dict[str, str]:
    """Read Tekton results using explicit ``$(results.*.path)`` file paths."""
    out: dict[str, str] = {}
    for name, raw in paths.items():
        path = (raw or "").strip()
        if not path or "$(" in path:
            continue
        target = Path(path)
        if not target.is_file():
            continue
        try:
            val = target.read_text(encoding="utf-8").strip()
        except OSError:
            continue
        if val:
            out[name] = val
    return out


def write_tekton_task_result_files(
    results: dict[str, str],
    *,
    directory: str | Path | None = None,
) -> None:
    """Rewrite task-level Tekton result files under the Tekton results directory."""
    root = tekton_results_root(directory)
    for name, value in results.items():
        write_result(root / name, value)


def sync_tekton_task_result_files(
    results: dict[str, str],
    *,
    directory: str | Path | None = None,
) -> None:
    """Rewrite *results* and remove stale Tekton result files not in *results*."""
    root = tekton_results_root(directory)
    keep = set(results)
    if root.is_dir():
        for path in root.iterdir():
            if path.is_file() and path.name not in keep:
                try:
                    path.unlink()
                except OSError as exc:
                    print(
                        f"WARN: could not remove stale Tekton result {path.name}: {exc}",
                        file=sys.stderr,
                    )
    write_tekton_task_result_files(results, directory=directory)


def write_tekton_results_at_paths(results: dict[str, str], paths: dict[str, str]) -> None:
    """Rewrite Tekton results at their declared ``$(results.*.path)`` locations."""
    for name, value in results.items():
        path = (paths.get(name) or "").strip()
        if not path or "$(" in path:
            continue
        write_result(path, value)


def fit_tekton_task_results(
    results: dict[str, str],
    *,
    priority: tuple[str, ...] = _PUBLISH_RESULTS_RESULT_PRIORITY,
    budget: int = _TEKTON_TASK_RESULTS_BUDGET_BYTES,
) -> dict[str, str]:
    """Return *results* trimmed so the Tekton termination JSON fits *budget*."""
    out = {k: clamp_tekton_result(v) for k, v in results.items() if (v or "").strip()}
    if "TEST_OUTPUT" in out:
        out["TEST_OUTPUT"] = slim_test_output_for_tekton(out["TEST_OUTPUT"])
    if tekton_results_termination_payload_size(out) <= budget:
        return out

    ordered = list(priority) + [k for k in out if k not in priority]
    protected = frozenset(priority[:4])
    shrink_limits = (1200, 800, 500, 300, 150, 80)

    for key in reversed(ordered):
        if tekton_results_termination_payload_size(out) <= budget:
            break
        if key not in out:
            continue
        if key in protected:
            for limit in shrink_limits:
                out[key] = clamp_tekton_result(out[key], max_bytes=limit)
                if key == "TEST_OUTPUT":
                    out[key] = slim_test_output_for_tekton(out[key])
                if tekton_results_termination_payload_size(out) <= budget:
                    break
            continue
        out.pop(key, None)

    if tekton_results_termination_payload_size(out) > budget:
        for key in ("FBCF_IMAGE", "ARTIFACTS_URL", "BVT_GATE", "SMOKE_GATE", "TIER1_GATE"):
            out.pop(key, None)
            if tekton_results_termination_payload_size(out) <= budget:
                break
    return out


def read_tekton_result_env(*names: str) -> None:
    """If env *names* point at Tekton result files, replace with file contents."""
    for name in names:
        raw = os.environ.get(name, "").strip()
        if not raw:
            continue
        path = Path(raw)
        if path.is_file():
            os.environ[name] = path.read_text(encoding="utf-8").strip()


def run(
    cmd: list[str],
    *,
    check: bool = True,
    capture: bool = False,
    input_text: str | None = None,
    timeout: float | None = 300,
    env: dict[str, str] | None = None,
    cwd: str | Path | None = None,
) -> subprocess.CompletedProcess[str]:
    """Thin wrapper around :func:`subprocess.run` with sensible defaults (always text mode)."""
    kw: dict[str, Any] = {
        "check": check,
        "text": True,
        "capture_output": capture,
        "timeout": timeout,
    }
    if input_text is not None:
        kw["input"] = input_text
    if env is not None:
        kw["env"] = env
    if cwd is not None:
        kw["cwd"] = str(cwd)
    return subprocess.run(cmd, **kw)


_RH_INTERNAL_HOSTS_RE = re.compile(r"^(?:gitlab\.cee\.redhat\.com|git\.corp\.redhat\.com)$")

_ALLOW_GIT_SSLVERIFY_FALSE_ENV = "OLMINSTALL_ALLOW_GIT_SSLVERIFY_FALSE"


def _safe_junit_int(raw: object, default: int = 0) -> int:
    try:
        return int(raw) if raw is not None else default
    except (TypeError, ValueError):
        return default


def _env_truthy(name: str) -> bool:
    return os.environ.get(name, "").strip().lower() in ("1", "true", "yes")


def _git_clone_dest_allowed(dest: Path) -> bool:
    """True if *dest* is a non-root path strictly under an allowed clone base (Tekton ``/workspace``, ``/tmp``, …)."""
    rp = dest.resolve()
    if rp == Path("/"):
        return False
    bases: list[Path] = [Path("/workspace").resolve(), Path("/tmp").resolve()]
    extra = os.environ.get("TEST_WORKSPACE", "").strip()
    if extra:
        bases.append(Path(extra).resolve())
    for base in bases:
        if rp == base:
            return False
        try:
            rp.relative_to(base)
            return True
        except ValueError:
            continue
    return False


def _staged_git_has_https_helper(tools_git: Path) -> bool:
    exec_path = os.environ.get("GIT_EXEC_PATH", "").strip()
    if exec_path:
        return (Path(exec_path) / "git-remote-https").is_file()
    bindir = tools_git.parent
    return (bindir / "git-core" / "git-remote-https").is_file()


def _git_executable() -> str:
    """Resolve ``git`` for Tekton steps; opendatahub-tests image often lacks git."""
    artifacts = Path(os.environ.get("ARTIFACTS_DIR", "/artifacts"))
    from steps.tests_payload import resolve_tests_payload_root, tests_payload_tools_bin_dir

    tools_git = tests_payload_tools_bin_dir(resolve_tests_payload_root(artifacts)) / "git"
    if tools_git.is_file() and _staged_git_has_https_helper(tools_git):
        return str(tools_git)
    path = shutil.which("git")
    if path:
        return path
    for candidate in ("/usr/bin/git", "/usr/local/bin/git"):
        if Path(candidate).is_file():
            return candidate
    print(
        "git not found in PATH or staged tools bin; clone-dependent prereqs must run in "
        "opendatahub-tests-prepare (konflux-test image) or stage git via runners.orchestrator",
        file=sys.stderr,
    )
    sys.exit(1)


def git_clone(
    url: str,
    rev: str,
    dest: str | Path,
    *,
    tls_workaround: bool = False,
) -> None:
    """Shallow-clone *url* at *rev* into *dest*.

    *dest* must resolve to a directory strictly under ``/workspace``, ``/tmp``,
    or ``TEST_WORKSPACE`` (when set); otherwise the clone is refused (whether or
    not *dest* already exists) to avoid writing outside allowed bases.

    When *tls_workaround* is ``True`` the Red Hat internal CA trust bundle
    is updated. Host-scoped ``sslVerify=false`` is applied only for known
    internal hosts and only when ``OLMINSTALL_ALLOW_GIT_SSLVERIFY_FALSE`` is
    set to a truthy value (audit log on stderr before mutating git config).
    """
    dest = Path(dest).resolve()
    if not _git_clone_dest_allowed(dest):
        print(
            f"Refusing clone destination outside allowed clone bases: {dest}",
            file=sys.stderr,
        )
        sys.exit(1)
    if dest.exists():
        print(f"Removing existing clone directory {dest}", file=sys.stderr)
        shutil.rmtree(dest)
    dest.mkdir(parents=True, exist_ok=True)

    git_prefix: list[str] = []
    if tls_workaround:
        git_prefix = _rh_git_invocation_prefix(url)

    def _git(args: list[str]) -> None:
        run([_git_executable(), *git_prefix, *args], cwd=dest)

    print(f"Cloning {url}@{rev} -> {dest} ...")
    _git(["init", "-q"])
    _git(["remote", "add", "origin", url])
    _git(["fetch", "--depth=1", "origin", rev])
    _git(["checkout", "-q", "FETCH_HEAD"])
    print(f"Cloned {url}@{rev}")


_WRITABLE_KUBECONFIG = Path("/tmp/rhoai-e2e-writable-kubeconfig")
_ADMIN_KUBECONFIG_BACKUP = Path("/tmp/rhoai-e2e-admin-kubeconfig")
_CLUSTER_ADMIN_SA_NS = "kube-system"
_CLUSTER_ADMIN_SA_NAME = "rhoai-e2e-cluster-admin"
OLMINSTALL_HTPASSWD_KUBECONFIG_ENV = "OLMINSTALL_HTPASSWD_KUBECONFIG"


def _preserve_htpasswd_pytest_kubeconfig(env: dict[str, str]) -> bool:
    """True when kubeconfig was logged in as htpasswd for unprivileged pytest."""
    return env.get(OLMINSTALL_HTPASSWD_KUBECONFIG_ENV, "").strip().lower() in (
        "1",
        "true",
        "yes",
    )


def ensure_writable_kubeconfig(environ: dict[str, str] | None = None) -> None:
    """Copy read-only Tekton Secret kubeconfig so ``oc login`` can update credentials."""
    env = os.environ if environ is None else environ
    kc = env.get("KUBECONFIG", "").strip()
    if not kc:
        return
    src = Path(kc)
    if not src.is_file():
        return
    try:
        if src.resolve() == _WRITABLE_KUBECONFIG.resolve():
            return
    except OSError:
        pass
    shutil.copy2(src, _WRITABLE_KUBECONFIG)
    _WRITABLE_KUBECONFIG.chmod(0o600)
    if _kubeconfig_user_uses_client_cert(src):
        shutil.copy2(src, _ADMIN_KUBECONFIG_BACKUP)
        _ADMIN_KUBECONFIG_BACKUP.chmod(0o600)
    env["KUBECONFIG"] = str(_WRITABLE_KUBECONFIG)


def _kubeconfig_bearer_token(path: Path) -> str:
    try:
        import yaml
    except ImportError:
        return ""
    try:
        doc = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError):
        return ""
    if not isinstance(doc, dict):
        return ""
    contexts = doc.get("contexts") if isinstance(doc.get("contexts"), list) else []
    users = doc.get("users") if isinstance(doc.get("users"), list) else []
    current = str(doc.get("current-context") or "").strip()
    ctx = next((c for c in contexts if isinstance(c, dict) and c.get("name") == current), None)
    if not isinstance(ctx, dict):
        return ""
    context = ctx.get("context") if isinstance(ctx.get("context"), dict) else {}
    user_name = str(context.get("user") or "").strip()
    user_entry = next((u for u in users if isinstance(u, dict) and u.get("name") == user_name), None)
    if not isinstance(user_entry, dict):
        return ""
    user = user_entry.get("user") if isinstance(user_entry.get("user"), dict) else {}
    return str(user.get("token") or "").strip()


def _resolve_oc_binary(env: dict[str, str]) -> str:
    staged = Path(env.get("ARTIFACTS_DIR", "/artifacts").strip() or "/artifacts")
    from steps.tests_payload import resolve_tests_payload_root, tests_payload_tools_bin_dir

    tools_oc = tests_payload_tools_bin_dir(resolve_tests_payload_root(staged)) / "oc"
    if tools_oc.is_file():
        return str(tools_oc)
    path = shutil.which("oc", path=env.get("PATH"))
    if path:
        return path
    for candidate in ("/usr/bin/oc", "/usr/local/bin/oc"):
        if Path(candidate).is_file():
            return candidate
    return ""


def _kubeconfig_current_user(path: Path) -> dict:
    try:
        import yaml
    except ImportError:
        return {}
    try:
        doc = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError):
        return {}
    if not isinstance(doc, dict):
        return {}
    contexts = doc.get("contexts") if isinstance(doc.get("contexts"), list) else []
    users = doc.get("users") if isinstance(doc.get("users"), list) else []
    current = str(doc.get("current-context") or "").strip()
    ctx = next((c for c in contexts if isinstance(c, dict) and c.get("name") == current), None)
    if not isinstance(ctx, dict):
        return {}
    context = ctx.get("context") if isinstance(ctx.get("context"), dict) else {}
    user_name = str(context.get("user") or "").strip()
    user_entry = next((u for u in users if isinstance(u, dict) and u.get("name") == user_name), None)
    if not isinstance(user_entry, dict):
        return {}
    user = user_entry.get("user")
    return user if isinstance(user, dict) else {}


def _kubeconfig_user_uses_exec(path: Path) -> bool:
    return isinstance(_kubeconfig_current_user(path).get("exec"), dict)


def _kubeconfig_user_uses_client_cert(path: Path) -> bool:
    user = _kubeconfig_current_user(path)
    if not user:
        return False
    return bool(
        user.get("client-certificate")
        or user.get("client-certificate-data")
        or user.get("client-key")
        or user.get("client-key-data")
    )


def _kubeconfig_api_server(path: Path) -> str:
    try:
        import yaml
    except ImportError:
        return ""
    try:
        doc = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError):
        return ""
    if not isinstance(doc, dict):
        return ""
    contexts = doc.get("contexts") if isinstance(doc.get("contexts"), list) else []
    clusters = doc.get("clusters") if isinstance(doc.get("clusters"), list) else []
    current = str(doc.get("current-context") or "").strip()
    ctx = next((c for c in contexts if isinstance(c, dict) and c.get("name") == current), None)
    if not isinstance(ctx, dict):
        return ""
    context = ctx.get("context") if isinstance(ctx.get("context"), dict) else {}
    cluster_name = str(context.get("cluster") or "").strip()
    cluster_entry = next(
        (c for c in clusters if isinstance(c, dict) and c.get("name") == cluster_name),
        None,
    )
    if not isinstance(cluster_entry, dict):
        return ""
    cluster = cluster_entry.get("cluster") if isinstance(cluster_entry.get("cluster"), dict) else {}
    return str(cluster.get("server") or "").strip()


def _kubeconfig_cluster_ca_data(path: Path) -> str:
    try:
        import yaml
    except ImportError:
        return ""
    try:
        doc = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError):
        return ""
    if not isinstance(doc, dict):
        return ""
    contexts = doc.get("contexts") if isinstance(doc.get("contexts"), list) else []
    clusters = doc.get("clusters") if isinstance(doc.get("clusters"), list) else []
    current = str(doc.get("current-context") or "").strip()
    ctx = next((c for c in contexts if isinstance(c, dict) and c.get("name") == current), None)
    if not isinstance(ctx, dict):
        return ""
    context = ctx.get("context") if isinstance(ctx.get("context"), dict) else {}
    cluster_name = str(context.get("cluster") or "").strip()
    cluster_entry = next(
        (c for c in clusters if isinstance(c, dict) and c.get("name") == cluster_name),
        None,
    )
    if not isinstance(cluster_entry, dict):
        return ""
    cluster = cluster_entry.get("cluster") if isinstance(cluster_entry.get("cluster"), dict) else {}
    return str(cluster.get("certificate-authority-data") or "").strip()


def _admin_kubeconfig_path(env: dict[str, str]) -> Path | None:
    explicit = env.get("OLMINSTALL_ADMIN_KUBECONFIG", "").strip()
    if explicit:
        path = Path(explicit)
        if path.is_file():
            return path
    backup = _ADMIN_KUBECONFIG_BACKUP
    if backup.is_file():
        if _kubeconfig_user_uses_client_cert(backup) or _kubeconfig_bearer_token(backup):
            return backup
    kc = env.get("KUBECONFIG", "").strip()
    if kc:
        path = Path(kc)
        if path.is_file() and _kubeconfig_user_uses_client_cert(path):
            return path
    return None


def backup_kubeconfig_for_admin_restore(environ: dict[str, str] | None = None) -> None:
    """Preserve cluster-admin kubeconfig before ``oc login`` as an htpasswd pytest user."""
    env = os.environ if environ is None else environ
    ensure_writable_kubeconfig(env)
    kc = env.get("KUBECONFIG", "").strip()
    if not kc:
        return
    src = Path(kc)
    if not src.is_file():
        return
    try:
        if src.resolve() == _ADMIN_KUBECONFIG_BACKUP.resolve():
            return
    except OSError:
        pass
    if not _ADMIN_KUBECONFIG_BACKUP.is_file():
        shutil.copy2(src, _ADMIN_KUBECONFIG_BACKUP)
        _ADMIN_KUBECONFIG_BACKUP.chmod(0o600)
    env["OLMINSTALL_ADMIN_KUBECONFIG"] = str(_ADMIN_KUBECONFIG_BACKUP)


def _oc_login_supports_password_stdin(oc: str, env: dict[str, str]) -> bool:
    """True when bundled ``oc login`` accepts ``--password-stdin`` (missing on some image CLIs)."""
    help_proc = run([oc, "login", "--help"], check=False, capture=True, env=env, timeout=20)
    text = f"{help_proc.stdout or ''}\n{help_proc.stderr or ''}"
    return "--password-stdin" in text


def _run_htpasswd_oc_login(
    oc: str,
    *,
    server: str,
    user: str,
    password: str,
    env: dict[str, str],
    ca_file: Path | None,
) -> subprocess.CompletedProcess[str]:
    use_stdin = _oc_login_supports_password_stdin(oc, env)
    cmd = [oc, "login", f"--server={server}", f"-u={user}"]
    if use_stdin:
        cmd.append("--password-stdin")
        input_text: str | None = password
    else:
        cmd.extend(["-p", password])
        input_text = None
    if ca_file is not None:
        cmd.append(f"--certificate-authority={ca_file}")
    else:
        cmd.append("--insecure-skip-tls-verify=true")
    return run(cmd, check=False, capture=True, env=env, timeout=60, input_text=input_text)


def materialize_htpasswd_kubeconfig_login(
    username: str,
    password: str,
    environ: dict[str, str] | None = None,
) -> bool:
    """Log the writable kubeconfig in as htpasswd so ``unprivileged_client`` avoids SA token user."""
    user = (username or "").strip()
    pwd = (password or "").strip()
    if not user or not pwd:
        return False
    env = os.environ if environ is None else environ
    ensure_writable_kubeconfig(env)
    backup_kubeconfig_for_admin_restore(env)
    kc = env.get("KUBECONFIG", "").strip()
    if not kc:
        return False
    path = Path(kc)
    server = _kubeconfig_api_server(path)
    if not server:
        print(
            "WARN: could not resolve API server for htpasswd oc login",
            file=sys.stderr,
            flush=True,
        )
        return False
    oc = _resolve_oc_binary(env)
    if not oc:
        print("WARN: oc not found for htpasswd kubeconfig login", file=sys.stderr, flush=True)
        return False
    ca_data = _kubeconfig_cluster_ca_data(path)
    if ca_data:
        import base64
        import tempfile

        with tempfile.TemporaryDirectory() as tmpdir:
            ca_file = Path(tmpdir) / "ca.crt"
            ca_file.write_bytes(base64.b64decode(ca_data))
            ca_file.chmod(0o600)
            proc = _run_htpasswd_oc_login(
                oc,
                server=server,
                user=user,
                password=pwd,
                env=env,
                ca_file=ca_file,
            )
    else:
        proc = _run_htpasswd_oc_login(
            oc,
            server=server,
            user=user,
            password=pwd,
            env=env,
            ca_file=None,
        )
    if proc.returncode != 0:
        print(
            f"WARN: htpasswd oc login failed for {user}: "
            f"{(proc.stderr or proc.stdout or '').strip()}",
            file=sys.stderr,
            flush=True,
        )
        return False
    whoami = run([oc, "whoami"], check=False, capture=True, env=env, timeout=20)
    who = (whoami.stdout or "").strip()
    if whoami.returncode != 0 or who != user:
        print(
            f"WARN: htpasswd oc login context is {who!r}, expected {user!r}",
            file=sys.stderr,
            flush=True,
        )
        return False
    env[OLMINSTALL_HTPASSWD_KUBECONFIG_ENV] = "1"
    print(f"✓ Kubeconfig logged in as htpasswd user {user} for pytest", flush=True)
    return True


def _oc_env_for_kubeconfig(env: dict[str, str], kubeconfig: Path) -> dict[str, str]:
    merged = dict(env)
    merged["KUBECONFIG"] = str(kubeconfig)
    return merged


def _oc_has_cluster_admin(oc: str, env: dict[str, str]) -> bool:
    proc = run(
        [oc, "auth", "can-i", "*", "*", "--all-namespaces"],
        capture=True,
        check=False,
        env=env,
        timeout=30,
    )
    return proc.returncode == 0 and (proc.stdout or "").strip().lower() == "yes"


def _token_authenticated(oc: str, token: str, kubeconfig_path: Path, env: dict[str, str]) -> bool:
    """Return True when *token* can reach the API (not 401)."""
    server = _kubeconfig_api_server(kubeconfig_path)
    ca_data = _kubeconfig_cluster_ca_data(kubeconfig_path)
    if not token or not server or not ca_data:
        return False
    yaml = _ensure_yaml_for_kubeconfig()
    if yaml is None:
        return False
    import tempfile

    with tempfile.TemporaryDirectory(prefix="rhoai-e2e-token-auth-") as tmp:
        tmp_kc = Path(tmp) / "kubeconfig"
        doc = {
            "apiVersion": "v1",
            "kind": "Config",
            "clusters": [
                {
                    "name": "c",
                    "cluster": {
                        "server": server,
                        "certificate-authority-data": ca_data,
                    },
                }
            ],
            "contexts": [{"name": "ctx", "context": {"cluster": "c", "user": "u"}}],
            "current-context": "ctx",
            "users": [{"name": "u", "user": {"token": token}}],
        }
        try:
            tmp_kc.write_text(yaml.safe_dump(doc, default_flow_style=False), encoding="utf-8")
            tmp_kc.chmod(0o600)
        except OSError:
            return False
        check_env = {key: val for key, val in env.items() if key != "KUBECONFIG"}
        check_env["KUBECONFIG"] = str(tmp_kc)
        proc = run(
            [oc, "auth", "can-i", "get", "namespaces"],
            capture=True,
            check=False,
            env=check_env,
            timeout=30,
        )
        return proc.returncode == 0 and (proc.stdout or "").strip().lower() in ("yes", "no")


def _token_has_cluster_admin(oc: str, token: str, kubeconfig_path: Path, env: dict[str, str]) -> bool:
    server = _kubeconfig_api_server(kubeconfig_path)
    ca_data = _kubeconfig_cluster_ca_data(kubeconfig_path)
    if not token or not server or not ca_data:
        return False
    yaml = _ensure_yaml_for_kubeconfig()
    if yaml is None:
        return False
    import tempfile

    with tempfile.TemporaryDirectory(prefix="rhoai-e2e-token-check-") as tmp:
        tmp_kc = Path(tmp) / "kubeconfig"
        doc = {
            "apiVersion": "v1",
            "kind": "Config",
            "clusters": [
                {
                    "name": "c",
                    "cluster": {
                        "server": server,
                        "certificate-authority-data": ca_data,
                    },
                }
            ],
            "contexts": [{"name": "ctx", "context": {"cluster": "c", "user": "u"}}],
            "current-context": "ctx",
            "users": [{"name": "u", "user": {"token": token}}],
        }
        try:
            tmp_kc.write_text(yaml.safe_dump(doc, default_flow_style=False), encoding="utf-8")
            tmp_kc.chmod(0o600)
        except OSError:
            return False
        check_env = {key: val for key, val in env.items() if key != "KUBECONFIG"}
        check_env["KUBECONFIG"] = str(tmp_kc)
        proc = run(
            [oc, "auth", "can-i", "*", "*", "--all-namespaces"],
            capture=True,
            check=False,
            env=check_env,
            timeout=30,
        )
        return proc.returncode == 0 and (proc.stdout or "").strip().lower() == "yes"


def _rhoai_e2e_cluster_admin_sa_identity() -> str:
    return f"system:serviceaccount:{_CLUSTER_ADMIN_SA_NS}:{_CLUSTER_ADMIN_SA_NAME}"


def _rhoai_e2e_cluster_admin_sa_ready(oc: str, oc_env: dict[str, str], *, timeout: int = 120) -> bool:
    """True when the rhoai-e2e cluster-admin SA already has cluster-admin."""
    proc = run(
        [
            oc,
            "auth",
            "can-i",
            "*",
            "*",
            "--as",
            _rhoai_e2e_cluster_admin_sa_identity(),
        ],
        capture=True,
        check=False,
        env=oc_env,
        timeout=timeout,
    )
    return proc.returncode == 0 and (proc.stdout or "").strip().lower() == "yes"


def _ensure_rhoai_e2e_cluster_admin_sa(oc: str, oc_env: dict[str, str]) -> bool:
    """Ensure a cluster-admin SA exists for minting pytest/golang bearer tokens on EPHC."""
    slow_timeout = int(os.environ.get("OLMINSTALL_OC_SLOW_TIMEOUT_SEC", "120"))
    get = run(
        [oc, "get", "sa", _CLUSTER_ADMIN_SA_NAME, "-n", _CLUSTER_ADMIN_SA_NS],
        capture=True,
        check=False,
        env=oc_env,
        timeout=slow_timeout,
    )
    if get.returncode != 0:
        create = run(
            [oc, "create", "sa", _CLUSTER_ADMIN_SA_NAME, "-n", _CLUSTER_ADMIN_SA_NS],
            capture=True,
            check=False,
            env=oc_env,
            timeout=slow_timeout,
        )
        if create.returncode != 0:
            err = ((create.stderr or "") + (create.stdout or "")).strip()[:200]
            print(
                f"WARN: could not create {_CLUSTER_ADMIN_SA_NS}/{_CLUSTER_ADMIN_SA_NAME}: {err}",
                file=sys.stderr,
                flush=True,
            )
            return False
    if _rhoai_e2e_cluster_admin_sa_ready(oc, oc_env, timeout=slow_timeout):
        return True
    bind = run(
        [
            oc,
            "adm",
            "policy",
            "add-cluster-role-to-user",
            "cluster-admin",
            _rhoai_e2e_cluster_admin_sa_identity(),
        ],
        capture=True,
        check=False,
        env=oc_env,
        timeout=slow_timeout,
    )
    bind_text = ((bind.stderr or "") + (bind.stdout or "")).lower()
    if bind.returncode != 0 and "already" not in bind_text:
        err = bind_text.strip()[:200]
        print(f"WARN: cluster-admin bind for rhoai-e2e SA failed: {err}", file=sys.stderr, flush=True)
        return False
    return True


def _ensure_yaml_for_kubeconfig() -> Any:
    try:
        import yaml

        return yaml
    except ImportError:
        pip = run(
            [sys.executable, "-m", "pip", "install", "--no-cache-dir", "-q", "pyyaml"],
            capture=True,
            check=False,
        )
        if pip.returncode != 0:
            print(
                "WARN: PyYAML unavailable; cannot materialize bearer token into kubeconfig",
                file=sys.stderr,
                flush=True,
            )
            return None
        try:
            import yaml

            return yaml
        except ImportError:
            print(
                "WARN: PyYAML still unavailable after pip install; cannot materialize bearer token",
                file=sys.stderr,
                flush=True,
            )
            return None


def _oc_authenticated(oc: str, env: dict[str, str]) -> bool:
    proc = run([oc, "whoami"], capture=True, check=False, env=env, timeout=30)
    return proc.returncode == 0 and bool((proc.stdout or "").strip())


def _oc_whoami_t(env: dict[str, str]) -> str:
    oc = _resolve_oc_binary(env)
    if not oc:
        return ""
    proc = run([oc, "whoami", "-t"], capture=True, check=False, env=env, timeout=30)
    return (proc.stdout or "").strip() if proc.returncode == 0 else ""


def _ephc_cluster_source(env: dict[str, str]) -> bool:
    return env.get("CLUSTER_SOURCE", "").strip() in ("", "EPHC")


def _token_from_oc_create_token(path: Path, env: dict[str, str]) -> str:
    """Mint a cluster-admin bearer token using client-cert admin kubeconfig on EPHC."""
    oc = _resolve_oc_binary(env)
    if not oc:
        return ""
    admin_kc = _admin_kubeconfig_path(env)
    oc_env = _oc_env_for_kubeconfig(env, admin_kc) if admin_kc else env
    if not _oc_authenticated(oc, oc_env):
        oc_env = env
        if not _oc_authenticated(oc, oc_env):
            return ""
    if not _oc_has_cluster_admin(oc, oc_env):
        print(
            "WARN: cannot mint cluster-admin bearer token without cluster-admin kubeconfig identity",
            file=sys.stderr,
            flush=True,
        )
        return ""
    if not _ensure_rhoai_e2e_cluster_admin_sa(oc, oc_env):
        return ""
    proc = run(
        [
            oc,
            "create",
            "token",
            _CLUSTER_ADMIN_SA_NAME,
            "-n",
            _CLUSTER_ADMIN_SA_NS,
            "--duration=24h",
        ],
        capture=True,
        check=False,
        env=oc_env,
        timeout=60,
    )
    token = (proc.stdout or "").strip()
    if proc.returncode != 0 or not token:
        err = ((proc.stderr or "") + (proc.stdout or "")).strip()[:200]
        if err:
            print(
                f"WARN: oc create token {_CLUSTER_ADMIN_SA_NAME} -n {_CLUSTER_ADMIN_SA_NS} failed: {err}",
                file=sys.stderr,
                flush=True,
            )
        return ""
    if _oc_has_cluster_admin(oc, oc_env):
        return token
    if _token_has_cluster_admin(oc, token, path, env):
        return token
    print(
        f"WARN: oc create token {_CLUSTER_ADMIN_SA_NAME} minted token without cluster-admin; skipping",
        file=sys.stderr,
        flush=True,
    )
    return ""


def _token_from_exec_user_block(path: Path, user: dict) -> str:
    from k8s.external_kubeconfig import _token_from_exec_user, _user_uses_exec_auth

    if not _user_uses_exec_auth(user):
        return ""
    try:
        return _token_from_exec_user(path, user)
    except Exception as exc:
        print(
            f"WARN: exec credential plugin token resolution failed: {exc}",
            file=sys.stderr,
            flush=True,
        )
        return ""


def _resolve_bearer_token_from_kubeconfig(path: Path, env: dict[str, str]) -> str:
    oc = _resolve_oc_binary(env)
    user = _kubeconfig_current_user(path)

    def _accept(token: str) -> str:
        if not token:
            return ""
        if oc and not _token_authenticated(oc, token, path, env):
            return ""
        return token

    if _ephc_cluster_source(env):
        if oc and _oc_authenticated(oc, env):
            minted = _token_from_oc_create_token(path, env)
            if minted:
                return minted
        whoami_t = _oc_whoami_t(env)
        if whoami_t and oc and _token_has_cluster_admin(oc, whoami_t, path, env):
            return whoami_t
        return ""

    # External pooled clusters: prefer 24h rhoai-e2e SA token over short OIDC exec/whoami
    # tokens so long full-matrix prep+test waves do not hit 401 mid-run (psi-23 2w6sl).
    if oc and _oc_authenticated(oc, env):
        minted = _token_from_oc_create_token(path, env)
        if minted:
            return minted

    if user:
        exec_token = _accept(_token_from_exec_user_block(path, user))
        if exec_token:
            return exec_token

    whoami_t = _accept(_oc_whoami_t(env))
    if whoami_t:
        return whoami_t

    embedded = _kubeconfig_bearer_token(path)
    return _accept(embedded) if embedded else ""


def ensure_kubeconfig_bearer_token(environ: dict[str, str] | None = None) -> None:
    """Embed a bearer token in the writable kubeconfig for pytest ``current_client_token``.

    EPHC kubeconfigs often ship with client-cert auth or a stale ``user.token`` that satisfies
    ``oc`` but not opendatahub-tests ``get_openshift_token(client=admin_client)``; refresh via
    ``oc whoami -t`` or ``oc create token`` when ``oc`` is available (tr274 / zq8p8).
    """
    env = os.environ if environ is None else environ
    if _preserve_htpasswd_pytest_kubeconfig(env):
        print(
            "✓ Skipping bearer token materialization (htpasswd pytest kubeconfig login)",
            flush=True,
        )
        return
    ensure_writable_kubeconfig(env)
    kc = env.get("KUBECONFIG", "").strip()
    if not kc:
        return
    path = Path(kc)
    if not path.is_file():
        return
    existing = _kubeconfig_bearer_token(path)
    whoami_t = _oc_whoami_t(env)
    token = _resolve_bearer_token_from_kubeconfig(path, env)
    if not token:
        if not existing:
            print(
                "WARN: could not resolve bearer token for kubeconfig; pytest current_client_token may fail",
                file=sys.stderr,
                flush=True,
            )
        return
    oc = _resolve_oc_binary(env)
    if _ephc_cluster_source(env) and oc:
        admin_kc = _admin_kubeconfig_path(env)
        admin_env = _oc_env_for_kubeconfig(env, admin_kc) if admin_kc else env
        if not _token_has_cluster_admin(oc, token, path, env) and not (
            admin_kc and _oc_has_cluster_admin(oc, admin_env)
        ) and not _oc_has_cluster_admin(oc, env):
            print(
                "WARN: refusing to materialize non-cluster-admin bearer token on EPHC",
                file=sys.stderr,
                flush=True,
            )
            return
    existing_authenticated = bool(existing and oc and _token_authenticated(oc, existing, path, env))
    must_rewrite = (
        not whoami_t
        or _kubeconfig_user_uses_exec(path)
        or _kubeconfig_user_uses_client_cert(path)
        or token != existing
        or not existing_authenticated
        or (_ephc_cluster_source(env) and bool(token))
    )
    env["OPENSHIFT_TOKEN"] = token
    env.setdefault("OC_TOKEN", token)
    if token == existing and not must_rewrite:
        return
    yaml = _ensure_yaml_for_kubeconfig()
    if yaml is None:
        return
    try:
        doc = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError):
        return
    if not isinstance(doc, dict):
        return
    contexts = doc.get("contexts") if isinstance(doc.get("contexts"), list) else []
    users = doc.get("users") if isinstance(doc.get("users"), list) else []
    current = str(doc.get("current-context") or "").strip()
    ctx = next((c for c in contexts if isinstance(c, dict) and c.get("name") == current), None)
    if not isinstance(ctx, dict):
        return
    context = ctx.get("context") if isinstance(ctx.get("context"), dict) else {}
    user_name = str(context.get("user") or "").strip()
    if not user_name:
        return
    doc["users"] = [{"name": user_name, "user": {"token": token}}]
    try:
        path.write_text(yaml.safe_dump(doc, default_flow_style=False), encoding="utf-8")
        path.chmod(0o600)
        print("✓ Materialized bearer token into kubeconfig for pytest", flush=True)
    except OSError:
        pass


def prepare_kubeconfig_auth_for_tests(
    environ: dict[str, str] | None = None,
    *,
    tekton_kubeconfig_path: str = "",
) -> None:
    """Stage ``oc``, refresh bearer token, and sync back to the Tekton kubeconfig mount.

    Component tasks run long after orchestrate materialized tokens; pytest reads
    ``user.token`` from kubeconfig and fails with 401 when the embedded bearer expired.
    """
    env = os.environ if environ is None else environ
    artifacts = env.get("ARTIFACTS_DIR", "/artifacts").strip() or "/artifacts"
    env.setdefault("ARTIFACTS_DIR", artifacts)
    from runners.orchestrator import stage_oc_for_pytest
    from steps.tests_payload import resolve_tests_payload_root, tests_payload_tools_bin_dir

    stage_oc_for_pytest()
    tools_bin = tests_payload_tools_bin_dir(resolve_tests_payload_root(Path(artifacts)))
    if tools_bin.is_dir():
        env["PATH"] = f"{tools_bin}:{env.get('PATH', '')}"
    ensure_writable_kubeconfig(env)
    ensure_kubeconfig_bearer_token(env)
    dest = tekton_kubeconfig_path.strip() or env.get("KUBECONFIG", "").strip()
    if dest:
        sync_materialized_kubeconfig_to(dest, env)


def sync_materialized_kubeconfig_to(
    tekton_kubeconfig_path: str,
    environ: dict[str, str] | None = None,
) -> None:
    """Copy bearer-materialized writable kubeconfig back to the Tekton-mounted path.

    Mode ``0644`` so a later Tekton step (component image, often non-root) can read
    the shared ``tests-shared`` kubeconfig after orchestrate materializes tokens.
    """
    dest = tekton_kubeconfig_path.strip()
    if not dest:
        return
    env = os.environ if environ is None else environ
    src_path = env.get("KUBECONFIG", "").strip()
    if not src_path or src_path == dest:
        return
    src, dst = Path(src_path), Path(dest)
    if not src.is_file() or not dst.parent.is_dir():
        return
    try:
        shutil.copyfile(src, dst)
        try:
            dst.chmod(0o644)
        except OSError:
            pass
        print(f"✓ Synced kubeconfig to {dst}", flush=True)
    except OSError as exc:
        print(
            f"WARN: could not sync kubeconfig to {dst} ({exc}); pytest uses {src}",
            file=sys.stderr,
            flush=True,
        )


def _rh_git_invocation_prefix(url: str) -> list[str]:
    """Return per-invocation ``git -c …`` args for RH internal TLS; never mutates global git config."""
    if Path("/etc/pki/ca-trust/source/anchors").is_dir():
        run(["update-ca-trust"], check=False, capture=True)

    probe = run(
        [_git_executable(), "ls-remote", "--exit-code", url, "HEAD"],
        check=False,
        capture=True,
    )
    if probe.returncode == 0:
        return []

    parsed = urlparse(url)
    host = (parsed.hostname or "").strip()
    if not host and parsed.netloc:
        host = parsed.netloc.split("@")[-1].split(":")[0]
    if not _RH_INTERNAL_HOSTS_RE.fullmatch(host):
        print(f"TLS verification failed for {url}.")
        print("Mount the internal CA bundle into the container so git ls-remote succeeds.")
        sys.exit(1)

    if not _env_truthy(_ALLOW_GIT_SSLVERIFY_FALSE_ENV):
        print(
            f"TLS verification failed for internal host {url}; refusing host-scoped sslVerify=false. "
            f"Set {_ALLOW_GIT_SSLVERIFY_FALSE_ENV}=1 after security review, or bake the RH IT CA into the image.",
            file=sys.stderr,
        )
        sys.exit(1)

    host_origin = f"{parsed.scheme}://{host}"
    # TODO(PRIORITY): bake the RH IT root CA into quay.io/rhoai/rhoai-task-toolset:its
    #       or mount it as a ConfigMap to remove this exception entirely.
    print(
        f"AUDIT: applying git http.{host_origin}.sslVerify=false url={url!r} host={host!r}",
        file=sys.stderr,
    )
    print(
        "  Escalation: replace sslVerify=false by shipping trusted CAs in the task image (see TODO above).",
        file=sys.stderr,
    )
    return ["-c", f"http.{host_origin}.sslVerify=false"]


def parse_junit_summary(artifacts_dir: str | Path, *, recursive: bool = False) -> dict[str, int]:
    """Parse JUnit XML files and return aggregate test counts.

    Returns dict with keys: ``total``, ``passed``, ``failures``, ``errors``, ``skipped``.
    """
    from suite.component_junit import is_intermediate_cypress_junit, junit_counts

    total = failures = errors = skipped = 0
    root = Path(artifacts_dir)
    xml_iter = sorted(root.rglob("*.xml")) if recursive else sorted(root.glob("*.xml"))
    for xml_path in xml_iter:
        if recursive and is_intermediate_cypress_junit(xml_path, root):
            continue
        counts = junit_counts(xml_path)
        if counts is None:
            continue
        total += counts["total"]
        failures += counts["failures"]
        errors += counts["errors"]
        skipped += counts["skipped"]
    passed = total - failures - errors - skipped
    return {
        "total": total,
        "passed": max(passed, 0),
        "failures": failures,
        "errors": errors,
        "skipped": skipped,
    }