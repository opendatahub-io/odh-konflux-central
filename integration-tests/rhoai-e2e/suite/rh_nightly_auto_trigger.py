"""Helpers for rh-nightly catalog sync Snapshots when the OCP-matched catalog digest changes."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from suite.component_version_gate import _compare_version_strings
from suite.constants import (
    DEFAULT_UPSTREAM_KONFLUX_GIT,
    LABEL_KONFLUX_APPLICATION,
    LABEL_KONFLUX_COMPONENT,
    LABEL_SNAPSHOT_TYPE,
    SNAPSHOT_TYPE_OVERRIDE,
)
from suite.errors import AppError

_RHOAI_FBC_PREFIX = "rhoai-fbc-fragment-"
_RHOAI_FBC_V_TAIL_RE = re.compile(r"^v(\d+)-(\d+)", re.IGNORECASE)
_DEFAULT_MIN_RHOAI = "3.5"
_DEFAULT_STATE_PATH = Path.home() / ".cache" / "rhoai-e2e" / "rh-nightly-last-triggered.json"


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


def build_override_snapshot_labels(
    *,
    application: str,
    fbc_component: str,
    extra_labels: dict[str, str] | None = None,
) -> dict[str, str]:
    """Konflux override Snapshot labels so ITS ``override`` contexts match."""
    app = application.strip()
    comp = fbc_component.strip()
    labels = {
        LABEL_SNAPSHOT_TYPE: SNAPSHOT_TYPE_OVERRIDE,
        LABEL_KONFLUX_APPLICATION: app,
        LABEL_KONFLUX_COMPONENT: comp,
    }
    if extra_labels:
        labels.update({k: v for k, v in extra_labels.items() if (k or "").strip() and (v or "").strip()})
    return labels


def build_auto_trigger_snapshot_doc(
    *,
    namespace: str,
    application: str,
    fbc_component: str,
    fbc_image: str,
    generate_name: str = "rh-nightly-snap-",
    git_url: str = DEFAULT_UPSTREAM_KONFLUX_GIT,
    git_revision: str = "main",
    labels: dict[str, str] | None = None,
    annotations: dict[str, str] | None = None,
) -> dict[str, Any]:
    if not (namespace or "").strip():
        raise AppError("snapshot namespace must be non-empty", 2)
    if not application.strip():
        raise AppError("rh-nightly catalog sync snapshot application must be non-empty", 2)
    if not fbc_component.strip() or not fbc_image.strip():
        raise AppError("rh-nightly catalog sync snapshot requires FBC component and image", 2)
    meta: dict[str, Any] = {
        "generateName": generate_name,
        "namespace": namespace.strip(),
    }
    if labels:
        meta["labels"] = {k: str(v) for k, v in labels.items() if (k or "").strip()}
    if annotations:
        meta["annotations"] = {k: str(v) for k, v in annotations.items() if (k or "").strip() and (v or "").strip()}
    return {
        "apiVersion": "appstudio.redhat.com/v1alpha1",
        "kind": "Snapshot",
        "metadata": meta,
        "spec": {
            "application": application.strip(),
            "components": [
                {
                    "name": fbc_component.strip(),
                    "containerImage": fbc_image.strip(),
                    "source": {
                        "git": {
                            "url": git_url,
                            "revision": git_revision,
                        }
                    },
                }
            ],
        },
    }


def build_auto_trigger_snapshot_yaml(
    *,
    application: str,
    fbc_component: str,
    fbc_image: str,
    generate_name: str = "rh-nightly-snap-",
    git_url: str = DEFAULT_UPSTREAM_KONFLUX_GIT,
    git_revision: str = "main",
) -> str:
    doc = build_auto_trigger_snapshot_doc(
        namespace="rhoai-tenant",
        application=application,
        fbc_component=fbc_component,
        fbc_image=fbc_image,
        generate_name=generate_name,
        git_url=git_url,
        git_revision=git_revision,
    )
    meta = doc["metadata"]
    comp = doc["spec"]["components"][0]
    comp_name = comp["name"]
    comp_image = comp["containerImage"]
    git = comp["source"]["git"]
    return (
        "---\n"
        "apiVersion: appstudio.redhat.com/v1alpha1\n"
        "kind: Snapshot\n"
        "metadata:\n"
        f"  generateName: {meta['generateName']}\n"
        "spec:\n"
        f"  application: {doc['spec']['application']}\n"
        "  components:\n"
        f"    - name: {comp_name}\n"
        f"      containerImage: {comp_image}\n"
        "      source:\n"
        "        git:\n"
        f"          url: {git['url']}\n"
        f'          revision: "{git["revision"]}"\n'
    )
