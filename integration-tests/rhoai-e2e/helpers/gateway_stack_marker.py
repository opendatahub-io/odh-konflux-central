"""Persist gateway auth stack readiness across rhoai-e2e install and Cypress steps."""

from __future__ import annotations

import os
from pathlib import Path

_MARKER_NAME = ".gateway-auth-stack-incomplete"


def _marker_base_dirs() -> list[Path]:
    """Directories that may carry cross-step state (setup-dependencies vs Cypress)."""
    bases: list[Path] = []
    tests_shared = os.environ.get("TESTS_SHARED", "").strip()
    if tests_shared:
        from steps.tests_payload import resolve_tests_payload_root

        payload = resolve_tests_payload_root(Path(tests_shared))
        bases.extend((payload / "results", payload))
    artifacts = os.environ.get("ARTIFACTS_DIR", "").strip()
    if artifacts:
        bases.append(Path(artifacts))
    if not bases:
        bases.append(Path("/artifacts"))
    seen: set[str] = set()
    ordered: list[Path] = []
    for base in bases:
        key = str(base)
        if key in seen:
            continue
        seen.add(key)
        ordered.append(base)
    return ordered


def gateway_stack_marker_paths() -> list[Path]:
    return [base / _MARKER_NAME for base in _marker_base_dirs()]


def write_gateway_stack_incomplete_marker() -> None:
    wrote = False
    for path in gateway_stack_marker_paths():
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("rhcl post-install retry failed\n", encoding="utf-8")
            wrote = True
        except OSError as exc:
            print(
                f"WARN: could not write gateway stack marker at {path} ({exc})",
                flush=True,
            )
    if not wrote:
        print("WARN: gateway stack incomplete marker not written to any base dir", flush=True)


def clear_gateway_stack_incomplete_marker() -> None:
    for path in gateway_stack_marker_paths():
        if path.is_file():
            path.unlink()


def gateway_stack_incomplete() -> bool:
    return any(path.is_file() for path in gateway_stack_marker_paths())


def reconcile_gateway_stack_incomplete_marker() -> bool:
    """Clear a stale incomplete marker when live Kuadrant/Authorino is Ready.

    install-dep-operators may write ``.gateway-auth-stack-incomplete`` before
    install-rhoai finishes reconciling Authorino. After reinstall, re-probe the
    live stack and clear the marker so MaaS components are not false-blocked.

    When the marker remains and a GatewayClass is present, restart Kuadrant to
    clear Ready=False/MissingDependency (common after cleanup+reinstall).

    Returns True when the stack is not blocking (no marker, or marker cleared).
    """
    if not gateway_stack_incomplete():
        return True
    try:
        from components.maas_billing.auth import (
            maas_gateway_auth_stack_live_ready,
            recover_kuadrant_after_gateway_api_provider,
        )
    except Exception as exc:  # pragma: no cover - import/cluster edge
        print(
            f"WARN: could not import live gateway-stack probe ({exc}); "
            "keeping incomplete marker",
            flush=True,
        )
        return False
    try:
        live_ready = maas_gateway_auth_stack_live_ready()
    except Exception as exc:
        print(
            f"WARN: live Kuadrant/Authorino probe failed ({exc}); "
            "keeping incomplete marker",
            flush=True,
        )
        return False
    if not live_ready:
        try:
            live_ready = bool(recover_kuadrant_after_gateway_api_provider())
        except Exception as exc:
            print(
                f"WARN: Kuadrant Gateway API provider recovery failed ({exc}); "
                "keeping incomplete marker",
                flush=True,
            )
            return False
        if live_ready:
            # recover clears the marker when Kuadrant becomes Ready; re-check TLS.
            try:
                live_ready = maas_gateway_auth_stack_live_ready() or not gateway_stack_incomplete()
            except Exception:
                live_ready = not gateway_stack_incomplete()
    if not live_ready:
        return False
    clear_gateway_stack_incomplete_marker()
    print(
        "✓ Cleared stale gateway-auth-stack-incomplete marker "
        "(Kuadrant Ready + Authorino TLS live-ready after reinstall)",
        flush=True,
    )
    return True
