"""Resolve versioned Quay image tags from installed operator CSV version."""

from __future__ import annotations

import re
import shutil

from steps.tekton_util import run

_EA_RE = re.compile(r"^(\d+)\.(\d+)\.\d+-ea\.(\d+)$")
_MAJOR_MINOR_RE = re.compile(r"^(\d+)\.(\d+)\.")


def _tag_exists(repo: str, tag: str) -> bool:
    skopeo = shutil.which("skopeo")
    if not skopeo:
        return False
    candidate = f"{repo}:{tag}"
    probe = run(
        [skopeo, "inspect", f"docker://{candidate}", "--no-tags"],
        check=False,
        capture=True,
    )
    return probe.returncode == 0


def prior_minor_ga_tags(csv_version: str) -> list[str]:
    """Older GA minor tags when the installed CSV minor has no published test image."""
    m = _EA_RE.match(csv_version)
    if m:
        major, minor = int(m.group(1)), int(m.group(2))
    else:
        m = _MAJOR_MINOR_RE.match(csv_version)
        if not m:
            return []
        major, minor = int(m.group(1)), int(m.group(2))
    return [f"{major}.{prior}" for prior in range(minor - 1, 0, -1)]


def ea_fallback_tags(csv_version: str) -> list[str]:
    """Candidate tags for RHOAI EA CSV versions (newest EA first)."""
    m = _EA_RE.match(csv_version)
    if not m:
        return []
    major, minor, ea = m.group(1), m.group(2), int(m.group(3))
    tags: list[str] = []
    for n in range(ea, 0, -1):
        tags.append(f"{major}.{minor}-ea.{n}")
        tags.append(f"{major}.{minor}ea{n}")
    tags.append(f"{major}.{minor}")
    return tags


def resolve_versioned_image(repo: str, csv_version: str) -> str:
    """Map operator CSV version to a versioned container tag, else ``:latest``."""
    latest_img = f"{repo}:latest"
    if not csv_version or csv_version == "latest":
        return latest_img

    m = _EA_RE.match(csv_version)
    if m:
        tags = ea_fallback_tags(csv_version)
    else:
        m = _MAJOR_MINOR_RE.match(csv_version)
        if m:
            tags = [f"{m.group(1)}.{m.group(2)}"]
        else:
            print(f"Unrecognized CSV version format: {csv_version} -- using {latest_img}")
            return latest_img

    skopeo = shutil.which("skopeo")
    if not skopeo:
        print(f"skopeo not found in PATH -- using {latest_img}")
        return latest_img

    if _EA_RE.match(csv_version):
        hyphen_ea_tags = [tag for tag in tags if "-ea." in tag]
        shorthand_ea_tags = [tag for tag in tags if tag not in hyphen_ea_tags]
        for tag in hyphen_ea_tags:
            candidate = f"{repo}:{tag}"
            if _tag_exists(repo, tag):
                print(f"Versioned image tag exists: {candidate}")
                return candidate
            print(f"Tag not found: {candidate}")
        # Prior GA minors (e.g. 3.5 on 3.6 EA) carry full pytest trees Jenkins uses; :latest
        # and shorthand EA tags (3.6ea1) often omit paths like tests/model_serving/maas_billing.
        for tag in prior_minor_ga_tags(csv_version):
            candidate = f"{repo}:{tag}"
            if _tag_exists(repo, tag):
                print(f"EA CSV {csv_version}: using prior GA image {candidate}")
                return candidate
            print(f"Prior minor GA tag not found: {candidate}")
        if _tag_exists(repo, "latest"):
            print(
                f"EA CSV {csv_version}: no exact EA or prior GA tag; using {latest_img}"
            )
            return latest_img
        for tag in shorthand_ea_tags:
            candidate = f"{repo}:{tag}"
            if _tag_exists(repo, tag):
                print(f"Versioned image tag exists: {candidate}")
                return candidate
            print(f"Tag not found: {candidate}")
    else:
        for tag in tags:
            candidate = f"{repo}:{tag}"
            if _tag_exists(repo, tag):
                print(f"Versioned image tag exists: {candidate}")
                return candidate
            print(f"Tag not found: {candidate}")

    for tag in prior_minor_ga_tags(csv_version):
        candidate = f"{repo}:{tag}"
        if _tag_exists(repo, tag):
            print(f"Prior minor GA image tag exists: {candidate}")
            return candidate
        print(f"Prior minor GA tag not found: {candidate}")

    print(f"No matching tag for CSV {csv_version} -- falling back to {latest_img}")
    return latest_img
