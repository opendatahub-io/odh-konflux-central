"""Helpers for rh-nightly catalog sync Snapshots when the OCP-matched catalog digest changes."""

from __future__ import annotations

import json
import re
import shutil
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from suite.component_version_gate import _compare_version_strings
from suite.errors import AppError

_RHOAI_FBC_PREFIX = "rhoai-fbc-fragment-"
_RHOAI_FBC_V_TAIL_RE = re.compile(r"^v(\d+)-(\d+)", re.IGNORECASE)
_DEFAULT_MIN_RHOAI = "3.5"
_DEFAULT_STATE_PATH = Path.home() / ".cache" / "rhoai-e2e" / "rh-nightly-last-triggered.json"
_LEGACY_STATE_PATH = Path.home() / ".cache" / "rhoai-e2e" / "rh-nightly-fan-in.json"
_OLMINSTALL_CACHE_DIR = Path.home() / ".cache" / "olminstall"
_OLMINSTALL_LAST_TRIGGERED = _OLMINSTALL_CACHE_DIR / "rh-nightly-last-triggered.json"
_OLMINSTALL_FAN_IN = _OLMINSTALL_CACHE_DIR / "rh-nightly-fan-in.json"


@dataclass(frozen=True)
class AutoTriggerDecision:
    action: str  # skip | trigger
    reason: str
    fbc_component: str = ""
    fbc_image: str = ""
    digest: str = ""


def image_digest(image: str) -> str:
    text = (image or "").strip()
    if "@sha256:" in text:
        return text.split("@sha256:", 1)[1].split()[0].lower()
    return text.lower()


def rhoai_fbc_component_meets_min_version(component_name: str, min_version: str = _DEFAULT_MIN_RHOAI) -> bool:
    """True when ``component_name`` is a RHOAI FBC fragment for ``min_version`` or newer."""
    name = (component_name or "").strip().lower()
    if not name.startswith(_RHOAI_FBC_PREFIX):
        return False
    tail = name[len(_RHOAI_FBC_PREFIX) :]
    if tail.startswith("ocp-"):
        return True
    match = _RHOAI_FBC_V_TAIL_RE.match(tail)
    if not match:
        return False
    ver = f"{match.group(1)}.{match.group(2)}"
    return _compare_version_strings(ver, min_version) >= 0


def last_triggered_state_key(cluster_id: str, fbc_component: str) -> str:
    return f"{cluster_id.strip()}:{fbc_component.strip()}"


def load_last_triggered_state(path: Path = _DEFAULT_STATE_PATH) -> dict[str, Any]:
    if not path.is_file():
        for legacy in (
            _LEGACY_STATE_PATH,
            _OLMINSTALL_LAST_TRIGGERED,
            _OLMINSTALL_FAN_IN,
        ):
            if legacy.is_file():
                path.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(legacy, path)
                break
    if not path.is_file():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def save_last_triggered_state(state: dict[str, Any], path: Path = _DEFAULT_STATE_PATH) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(state, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def decide_auto_trigger(
    *,
    cluster_id: str,
    fbc_component: str,
    fbc_image: str,
    min_rhoai: str = _DEFAULT_MIN_RHOAI,
    state: dict[str, Any] | None = None,
) -> AutoTriggerDecision:
    component = (fbc_component or "").strip()
    image = (fbc_image or "").strip()
    if not component or not image:
        return AutoTriggerDecision("skip", "no FBC image resolved for cluster OCP slice")
    if not rhoai_fbc_component_meets_min_version(component, min_rhoai):
        return AutoTriggerDecision(
            "skip",
            f"component {component!r} below MIN_RHOAI_VERSION {min_rhoai}",
            fbc_component=component,
            fbc_image=image,
        )
    digest = image_digest(image)
    key = last_triggered_state_key(cluster_id, component)
    prior = (state or {}).get(key, {})
    prior_digest = ""
    if isinstance(prior, dict):
        prior_digest = str(prior.get("digest", "")).strip().lower()
    if prior_digest and prior_digest == digest:
        return AutoTriggerDecision(
            "skip",
            f"catalog digest unchanged ({digest[:16]}…)",
            fbc_component=component,
            fbc_image=image,
            digest=digest,
        )
    return AutoTriggerDecision(
        "trigger",
        "new catalog digest",
        fbc_component=component,
        fbc_image=image,
        digest=digest,
    )


def record_auto_trigger_success(
    *,
    cluster_id: str,
    fbc_component: str,
    fbc_image: str,
    snapshot_name: str,
    state: dict[str, Any],
    v35_snapshot_ts: str = "",
) -> None:
    key = last_triggered_state_key(cluster_id, fbc_component)
    state[key] = {
        "digest": image_digest(fbc_image),
        "image": fbc_image,
        "snapshot": snapshot_name,
        "updated": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
    }
    if v35_snapshot_ts:
        state["last_v35_snapshot_ts"] = v35_snapshot_ts


def build_auto_trigger_snapshot_yaml(
    *,
    application: str,
    fbc_component: str,
    fbc_image: str,
    generate_name: str = "rh-nightly-snap-",
    git_url: str = "https://github.com/opendatahub-io/odh-konflux-central.git",
    git_revision: str = "main",
) -> str:
    if not application.strip():
        raise AppError("rh-nightly catalog sync snapshot application must be non-empty", 2)
    if not fbc_component.strip() or not fbc_image.strip():
        raise AppError("rh-nightly catalog sync snapshot requires FBC component and image", 2)
    return (
        "---\n"
        "apiVersion: appstudio.redhat.com/v1alpha1\n"
        "kind: Snapshot\n"
        "metadata:\n"
        f"  generateName: {generate_name}\n"
        "spec:\n"
        f"  application: {application}\n"
        "  components:\n"
        f"    - name: {fbc_component}\n"
        f"      containerImage: {fbc_image}\n"
        "      source:\n"
        "        git:\n"
        f"          url: {git_url}\n"
        f'          revision: "{git_revision}"\n'
    )
