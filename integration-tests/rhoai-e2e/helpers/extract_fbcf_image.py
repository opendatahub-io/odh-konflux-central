#!/usr/bin/env python3
"""Extract the FBCF container image from a Konflux ApplicationSnapshot JSON.

Env: SNAPSHOT (JSON string), COMPONENT_NAME, RESULT_PATH (Tekton result file).
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

_TEKTON_RESULTS = Path("/tekton/results").resolve()


def _validated_result_path(raw: str) -> Path:
    p = Path(raw).expanduser()
    rp = p.resolve()
    if rp != _TEKTON_RESULTS and _TEKTON_RESULTS not in rp.parents:
        raise ValueError(f"RESULT_PATH must be under {_TEKTON_RESULTS}, got {raw!r}")
    return rp


def main() -> int:
    snapshot_raw = os.environ.get("SNAPSHOT", "").strip()
    component = os.environ.get("COMPONENT_NAME", "").strip()
    result_path = os.environ.get("RESULT_PATH", "").strip()

    if not snapshot_raw:
        print("❌ SNAPSHOT env var is empty", file=sys.stderr)
        return 1
    if not component:
        print("❌ COMPONENT_NAME env var is empty", file=sys.stderr)
        return 1
    if not result_path:
        print("❌ RESULT_PATH env var is empty", file=sys.stderr)
        return 1

    print("Parsing Konflux snapshot...")
    try:
        snap = json.loads(snapshot_raw)
    except json.JSONDecodeError as exc:
        print(f"❌ Invalid SNAPSHOT JSON: {exc}", file=sys.stderr)
        return 1

    if not isinstance(snap, dict):
        print("❌ SNAPSHOT JSON root must be an object", file=sys.stderr)
        return 1
    components = snap.get("components")
    if components is None:
        print("❌ SNAPSHOT missing 'components' array", file=sys.stderr)
        return 1
    if not isinstance(components, list):
        print(
            f"❌ SNAPSHOT 'components' must be a list, got {type(components).__name__}",
            file=sys.stderr,
        )
        return 1

    fbcf_image = None
    found_component = False
    for i, comp in enumerate(components):
        if not isinstance(comp, dict):
            print(
                f"❌ snapshot.components[{i}] must be an object, got {type(comp).__name__}",
                file=sys.stderr,
            )
            return 1
        if comp.get("name") == component:
            found_component = True
            fbcf_image = comp.get("containerImage")
            break

    if not found_component:
        print(
            f"❌ SNAPSHOT has no component named {component!r}",
            file=sys.stderr,
        )
        return 1
    if fbcf_image is None:
        print(
            f"❌ Component {component!r} has no containerImage field (or it is null)",
            file=sys.stderr,
        )
        return 1
    if not isinstance(fbcf_image, str):
        print(
            f"❌ Component {component!r} containerImage must be a string, "
            f"got {type(fbcf_image).__name__}",
            file=sys.stderr,
        )
        return 1
    if not fbcf_image.strip():
        print(
            f"❌ Component {component!r} has empty or whitespace-only containerImage",
            file=sys.stderr,
        )
        return 1

    print(f"✓ Extracted FBCF Image: {fbcf_image}")
    try:
        out = _validated_result_path(result_path)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(fbcf_image, encoding="utf-8")
    except ValueError as exc:
        print(f"❌ Invalid RESULT_PATH: {exc}", file=sys.stderr)
        return 1
    except (FileNotFoundError, PermissionError, OSError) as exc:
        print(f"❌ Failed to write RESULT_PATH {result_path!r}: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
