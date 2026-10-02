"""Shared utilities for Tekton pipeline step scripts.

Provides reusable functions that replace per-script boilerplate:
env-var reading, Tekton result writing, git cloning (with optional
Red Hat internal TLS workaround), subprocess execution, and JUnit
XML summary parsing.
"""

from __future__ import annotations

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
        if pip.returncode != 0:
            tail = ((pip.stdout or "") + (pip.stderr or "")).strip()[:2000]
            print(
                f"defusedxml is required for JUnit parsing; pip install failed: {tail or pip.returncode}",
                file=sys.stderr,
            )
            sys.exit(1)
        try:
            _import_defused()
        except ImportError as exc:
            print(f"defusedxml still unavailable after pip install: {exc}", file=sys.stderr)
            sys.exit(1)


def require_env(name: str, default: str | None = None) -> str:
    """Return env var *name* (stripped). Exits non-zero when missing and no *default*."""
    v = os.environ.get(name, "").strip()
    if v:
        return v
    if default is not None:
        return default
    print(f"Required environment variable is missing: {name}", file=sys.stderr)
    sys.exit(1)


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
    target.write_text(value, encoding="utf-8")


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
        run(["git", *git_prefix, *args], cwd=dest)

    print(f"Cloning {url}@{rev} -> {dest} ...")
    _git(["init", "-q"])
    _git(["remote", "add", "origin", url])
    _git(["fetch", "--depth=1", "origin", rev])
    _git(["checkout", "-q", "FETCH_HEAD"])
    print(f"Cloned {url}@{rev}")


def _rh_git_invocation_prefix(url: str) -> list[str]:
    """Return per-invocation ``git -c …`` args for RH internal TLS; never mutates global git config."""
    if Path("/etc/pki/ca-trust/source/anchors").is_dir():
        run(["update-ca-trust"], check=False, capture=True)

    probe = run(
        ["git", "ls-remote", "--exit-code", url, "HEAD"],
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


def parse_junit_summary(artifacts_dir: str | Path) -> dict[str, int]:
    """Parse JUnit XML files and return aggregate test counts.

    Returns dict with keys: ``total``, ``passed``, ``failures``, ``errors``, ``skipped``.
    """
    _ensure_defusedxml()
    total = failures = errors = skipped = 0
    for xml_path in sorted(Path(artifacts_dir).glob("*.xml")):
        try:
            tree = _parse_junit_xml(xml_path)
        except (ElementTree.ParseError, DefusedXmlException):
            continue
        root = tree.getroot()
        suites = [root] if root.tag == "testsuite" else root.findall("testsuite")
        for ts in suites:
            total += _safe_junit_int(ts.get("tests"))
            failures += _safe_junit_int(ts.get("failures"))
            errors += _safe_junit_int(ts.get("errors"))
            skipped += _safe_junit_int(ts.get("skipped"))
    passed = total - failures - errors - skipped
    return {
        "total": total,
        "passed": max(passed, 0),
        "failures": failures,
        "errors": errors,
        "skipped": skipped,
    }
