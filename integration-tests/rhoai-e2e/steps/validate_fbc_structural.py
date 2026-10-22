#!/usr/bin/env python3
"""Run Jenkins rhoai-structural-tests (rhoai-releases make update-release + make test).

Env: PRODUCT, FBCF_IMAGE, RHOAI_VERSION, SNAPSHOT, COMPONENT_NAME, QUAY_PULL_SECRET_PATH,
RHOAI_RELEASES_REPO_URL, RHOAI_RELEASES_REPO_REVISION, RHOAI_RELEASES_DIR,
TESTS_PAYLOAD_DIR, OLMINSTALL_ALLOW_GIT_SSLVERIFY_FALSE.
"""
from __future__ import annotations

import hashlib
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

from suite.constants import (
    DEFAULT_RHOAI_RELEASES_REPO_REVISION,
    DEFAULT_RHOAI_RELEASES_REPO_URL,
    is_test_only_product,
    normalize_product,
)
from suite.snapshot_catalog_line import (
    catalog_line_from_fbc_opm_render,
    catalog_line_from_image_tag,
    catalog_line_from_snapshot_json,
)

_VERSION_RE = re.compile(r"^(\d+\.\d+(?:-(?:ea|rc)\.\d+)?)")
_DEFAULT_SUFFIX = re.compile(r"\s*\(default\)\s*$", re.IGNORECASE)
_NIGHTLY_PREFIX = "releases/self-managed/nightly-"
_OPM_VERSION = "v1.47.0"
_YQ_VERSION = "v4.44.3"
_OPM_SHA256: dict[tuple[str, str], str] = {
    ("linux", "amd64"): "502cb3ce30c979e0a486ca306677c3251c8315e66abe0fde0c25c96eff229061",
    ("linux", "arm64"): "50198a678eadbad0ce8c7bb9e8e80468bffd90fc7195571ee9b2726e4d4110df",
}
_YQ_SHA256: dict[tuple[str, str], str] = {
    ("linux", "amd64"): "a2c097180dd884a8d50c956ee16a9cec070f30a7947cf4ebf87d5f36213e9ed7",
    ("linux", "arm64"): "0e7e1524f68d91b3ff9b089872d185940ab0fa020a5a9052046ef10547023156",
}


def _usable_catalog_line(raw: str) -> str:
    text = _DEFAULT_SUFFIX.sub("", (raw or "").strip())
    if not text or text.lower() in {"n/a", "unspecified"}:
        return ""
    match = _VERSION_RE.match(text)
    return match.group(1) if match else ""


def nightly_release_path(
    rhoai_version: str,
    snapshot_raw: str = "",
    component_name: str = "",
    *,
    fbcf_image: str = "",
) -> str:
    """Map a catalog line to Jenkins ``RELEASE_PATH`` (nightly self-managed)."""
    line = _usable_catalog_line(rhoai_version)
    if not line:
        line = catalog_line_from_snapshot_json(snapshot_raw, component_name)
    if not line:
        line = catalog_line_from_image_tag(fbcf_image)
    if not line and (fbcf_image or "").strip():
        line = catalog_line_from_fbc_opm_render(fbcf_image)
    line = _usable_catalog_line(line)
    if not line:
        return ""
    return f"{_NIGHTLY_PREFIX}{line}"


def should_skip_structural(*, product: str, fbcf_image: str) -> bool:
    if is_test_only_product(product) or normalize_product(product) != "rhoai":
        return True
    img = (fbcf_image or "").strip()
    return not img or img.lower() == "n/a"


def _stage_registry_auth(quay_path: str) -> str:
    src = Path(quay_path)
    if not src.is_file():
        print(f"WARN: quay dockerconfig missing at {quay_path} (skopeo may fail on private images)")
        return ""
    dest = Path("/tmp/rhoai-structural-auth.json")
    dest.write_bytes(src.read_bytes())
    os.environ["REGISTRY_AUTH_FILE"] = str(dest)
    print(f"REGISTRY_AUTH_FILE={dest}")
    return str(dest)


def _copy_artifacts(repo: Path, release_path: str, payload_dir: str) -> None:
    if not payload_dir:
        return
    dest = Path(payload_dir) / "results" / "structural"
    dest.mkdir(parents=True, exist_ok=True)
    results = repo / "tests" / "results"
    if results.is_dir():
        for item in results.iterdir():
            target = dest / item.name
            if item.is_file():
                shutil.copy2(item, target)
    overview = repo / release_path / "overview.html"
    if overview.is_file():
        shutil.copy2(overview, dest / "overview.html")
    print(f"Copied structural artifacts to {dest}")


def _uname_os_arch() -> tuple[str, str]:
    sysname = os.uname().sysname.lower()
    machine = os.uname().machine
    arch = {"x86_64": "amd64", "aarch64": "arm64", "arm64": "arm64"}.get(machine, machine)
    return sysname, arch


def _download_github_bin(name: str, url: str, dest: Path, *, sha256: str) -> Path:
    print(f"Downloading {name} from {url}")
    proc = subprocess.run(["curl", "-sSfLo", str(dest), url], check=False)
    if proc.returncode != 0:
        print(f"❌ failed to download {name} (exit {proc.returncode})", file=sys.stderr)
        raise SystemExit(proc.returncode)
    digest = hashlib.sha256(dest.read_bytes()).hexdigest()
    if digest != sha256:
        print(f"❌ {name} checksum mismatch: {digest} != {sha256}", file=sys.stderr)
        raise SystemExit(1)
    dest.chmod(0o755)
    os.environ["PATH"] = f"{dest.parent}:{os.environ.get('PATH', '')}"
    print(f"{name} installed at {dest}")
    return dest


def ensure_opm(*, dest: Path | None = None) -> Path:
    """Install operator-registry ``opm`` when the task image does not ship it."""
    existing = shutil.which("opm")
    if existing:
        print(f"opm already on PATH: {existing}")
        return Path(existing)
    sysname, arch = _uname_os_arch()
    url = (
        "https://github.com/operator-framework/operator-registry/releases/download/"
        f"{_OPM_VERSION}/{sysname}-{arch}-opm"
    )
    expected = _OPM_SHA256.get((sysname, arch))
    if not expected:
        print(f"❌ no pinned opm checksum for {sysname}-{arch}", file=sys.stderr)
        raise SystemExit(1)
    return _download_github_bin("opm", url, dest or Path("/tmp/opm"), sha256=expected)


def ensure_yq(*, dest: Path | None = None) -> Path:
    """Install mikefarah ``yq`` (rhoai-releases bats lib requires it)."""
    existing = shutil.which("yq")
    if existing:
        print(f"yq already on PATH: {existing}")
        return Path(existing)
    sysname, arch = _uname_os_arch()
    url = (
        "https://github.com/mikefarah/yq/releases/download/"
        f"{_YQ_VERSION}/yq_{sysname}_{arch}"
    )
    expected = _YQ_SHA256.get((sysname, arch))
    if not expected:
        print(f"❌ no pinned yq checksum for {sysname}-{arch}", file=sys.stderr)
        raise SystemExit(1)
    return _download_github_bin("yq", url, dest or Path("/tmp/yq"), sha256=expected)


def ensure_findutils() -> None:
    """Install GNU ``find`` when the task image is missing findutils."""
    if shutil.which("find"):
        print("find already on PATH")
        return
    installer = shutil.which("microdnf") or shutil.which("dnf") or shutil.which("yum")
    if not installer:
        print("❌ find not on PATH and no microdnf/dnf/yum to install findutils", file=sys.stderr)
        raise SystemExit(1)
    print(f"+ {installer} install -y findutils")
    proc = subprocess.run([installer, "install", "-y", "findutils"], check=False)
    if proc.returncode != 0 or not shutil.which("find"):
        print(f"❌ failed to install findutils (exit {proc.returncode})", file=sys.stderr)
        raise SystemExit(proc.returncode or 1)


def _run_make(repo: Path, target: str, env: dict[str, str]) -> int:
    print(f"+ make {target}")
    proc = subprocess.run(
        ["make", target],
        cwd=repo,
        env=env,
        check=False,
    )
    return int(proc.returncode)


def main() -> int:
    product = os.environ.get("PRODUCT", "").strip()
    fbcf = os.environ.get("FBCF_IMAGE", "").strip()
    if should_skip_structural(product=product, fbcf_image=fbcf):
        print(f"✓ FBC structural skipped (PRODUCT={product or 'empty'} FBCF_IMAGE={fbcf or 'empty'})")
        return 0

    ensure_opm()
    _stage_registry_auth(os.environ.get("QUAY_PULL_SECRET_PATH", "/var/secret/quay/.dockerconfigjson"))
    release_path = nightly_release_path(
        os.environ.get("RHOAI_VERSION", ""),
        snapshot_raw=os.environ.get("SNAPSHOT", ""),
        component_name=os.environ.get("COMPONENT_NAME", "") or os.environ.get("RHOAI_FBC_NAME", ""),
        fbcf_image=fbcf,
    )
    if not release_path:
        print(
            "❌ Cannot map nightly RELEASE_PATH: set RHOAI_VERSION (e.g. 3.6-ea.1) "
            "or provide Snapshot PAC catalog line / FBC tag",
            file=sys.stderr,
        )
        return 1

    from steps.tekton_util import git_clone

    url = os.environ.get("RHOAI_RELEASES_REPO_URL", "").strip() or DEFAULT_RHOAI_RELEASES_REPO_URL
    rev = os.environ.get("RHOAI_RELEASES_REPO_REVISION", "").strip() or DEFAULT_RHOAI_RELEASES_REPO_REVISION
    dest = Path(os.environ.get("RHOAI_RELEASES_DIR", "").strip() or "/tmp/rhoai-releases")
    tls = os.environ.get("OLMINSTALL_ALLOW_GIT_SSLVERIFY_FALSE", "").strip().lower() in (
        "1",
        "true",
        "yes",
    )
    git_clone(url, rev, dest, tls_workaround=True if tls or "gitlab.cee.redhat.com" in url else False)

    ensure_yq()
    ensure_findutils()

    env = os.environ.copy()
    env["RELEASE_PATH"] = release_path
    env["INDEX_IMAGE"] = fbcf
    env["IIB"] = fbcf
    print(f"RELEASE_PATH={release_path}")
    print(f"INDEX_IMAGE={fbcf}")

    rc = _run_make(dest, "update-release", env)
    if rc != 0:
        print(f"❌ make update-release failed (exit {rc})", file=sys.stderr)
        return rc
    rc = _run_make(dest, "test", env)
    _copy_artifacts(dest, release_path, os.environ.get("TESTS_PAYLOAD_DIR", "").strip())
    if rc != 0:
        print(f"❌ make test failed (exit {rc})", file=sys.stderr)
        return rc
    print("✓ FBC structural tests passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
