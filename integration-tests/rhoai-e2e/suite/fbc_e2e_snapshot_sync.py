"""Create override Snapshots on FBC fragment apps (digest dedup) for rhoai-e2e ITS."""

from __future__ import annotations

import json
from typing import Any, Protocol

from suite.constants import TRIGGER_TYPE_RH_NIGHTLY_AUTO
from suite.rh_nightly_auto_trigger import (
    build_auto_trigger_snapshot_doc,
    build_override_snapshot_labels,
    decide_auto_trigger,
    load_last_triggered_state,
    record_auto_trigger_success,
    save_last_triggered_state,
)

FBC_E2E_SYNC_CLUSTER_ID = "fbc-e2e-sync"

# OCP minor → (Konflux Application, FBC component name)
FBC_E2E_SYNC_SLICES: dict[str, tuple[str, str]] = {
    "420": ("rhoai-fbc-fragment-ocp-420", "rhoai-fbc-fragment-ocp-420"),
    "421": ("rhoai-fbc-fragment-ocp-421", "rhoai-fbc-fragment-ocp-421"),
    "422": ("rhoai-fbc-fragment-ocp-422", "rhoai-fbc-fragment-ocp-422"),
}

DEFAULT_FBC_SYNC_SLICES = ("420", "421", "422")


class FbcImageResolver(Protocol):
    def latest_named_component_image_on_application(
        self,
        namespace: str,
        app_name: str,
        component_name: str,
        image_pattern: str,
        *,
        required_catalog_line: str = "",
    ) -> tuple[str, str, dict[str, Any] | None]: ...


def parse_fbc_sync_slices(raw: str) -> list[str]:
    text = (raw or "").strip()
    if not text:
        return list(DEFAULT_FBC_SYNC_SLICES)
    out: list[str] = []
    for part in text.split(","):
        key = part.strip()
        if not key:
            continue
        if key not in FBC_E2E_SYNC_SLICES:
            known = ", ".join(sorted(FBC_E2E_SYNC_SLICES))
            raise ValueError(f"unknown --sync-fbc-slices entry {key!r} (allowed: {known})")
        if key not in out:
            out.append(key)
    if not out:
        raise ValueError("--sync-fbc-slices must list at least one of 420, 421, 422")
    return out


def plan_fbc_e2e_snapshot_sync(
    *,
    resolver: FbcImageResolver,
    namespace: str,
    slices: list[str],
    state: dict[str, Any],
    min_rhoai: str,
    force: bool,
) -> list[dict[str, Any]]:
    plans: list[dict[str, Any]] = []
    for slice_key in slices:
        app, component = FBC_E2E_SYNC_SLICES[slice_key]
        _ts, image, snap_meta = resolver.latest_named_component_image_on_application(
            namespace,
            app,
            component,
            r"@sha256:",
            required_catalog_line="",
        )
        if not image:
            plans.append(
                {
                    "slice": slice_key,
                    "action": "skip",
                    "reason": f"no catalog image on application {app}",
                    "application": app,
                    "component": component,
                }
            )
            continue
        decision = decide_auto_trigger(
            cluster_id=FBC_E2E_SYNC_CLUSTER_ID,
            fbc_component=component,
            fbc_image=image,
            min_rhoai=min_rhoai,
            state=state,
        )
        action = decision.action
        reason = decision.reason
        if force and action == "skip" and "catalog digest unchanged" in reason:
            action = "trigger"
            reason = "forced (--force-sync-fbc)"
        plans.append(
            {
                "slice": slice_key,
                "action": action,
                "reason": reason,
                "application": app,
                "component": component,
                "image": image,
                "digest": decision.digest,
                "fbc_snapshot_meta": snap_meta,
            }
        )
    return plans


def build_fbc_override_snapshot_json(
    *,
    namespace: str,
    application: str,
    fbc_component: str,
    fbc_image: str,
    labels: dict[str, str],
    annotations: dict[str, str],
    git_url: str,
    git_revision: str,
) -> str:
    base_labels = build_override_snapshot_labels(application=application, fbc_component=fbc_component)
    base_labels.update(labels)
    doc = build_auto_trigger_snapshot_doc(
        namespace=namespace,
        application=application,
        fbc_component=fbc_component,
        fbc_image=fbc_image,
        generate_name=f"rhoai-e2e-fbc-{fbc_component.rsplit('-', 1)[-1]}-",
        git_url=git_url,
        git_revision=git_revision,
        labels=base_labels,
        annotations=annotations,
    )
    return json.dumps(doc)


def apply_fbc_e2e_sync_plans(
    *,
    namespace: str,
    plans: list[dict[str, Any]],
    state: dict[str, Any],
    labels: dict[str, str],
    annotations: dict[str, str],
    git_url: str,
    git_revision: str,
    create_snapshot,
    dry_run: bool,
) -> tuple[int, int]:
    """``create_snapshot`` is ``(namespace, json_body) -> snapshot_name``."""
    triggered = 0
    skipped = 0
    for plan in plans:
        action = plan.get("action")
        slice_key = plan.get("slice", "")
        app = str(plan.get("application", ""))
        component = str(plan.get("component", ""))
        if action != "trigger":
            skipped += 1
            print(f"SKIP ocp-{slice_key} ({app}): {plan.get('reason', '')}")
            continue
        image = str(plan.get("image", ""))
        body = build_fbc_override_snapshot_json(
            namespace=namespace,
            application=app,
            fbc_component=component,
            fbc_image=image,
            labels=labels,
            annotations=annotations,
            git_url=git_url,
            git_revision=git_revision,
        )
        if dry_run:
            print(f"DRY-RUN would create override Snapshot on {app} ({image[:48]}…)")
            triggered += 1
            continue
        snap_name = create_snapshot(namespace, body)
        record_auto_trigger_success(
            cluster_id=FBC_E2E_SYNC_CLUSTER_ID,
            fbc_component=component,
            fbc_image=image,
            snapshot_name=snap_name,
            state=state,
        )
        triggered += 1
        print(f"Created Snapshot {snap_name} on {app} (ocp-{slice_key})")
    if not dry_run and triggered:
        save_last_triggered_state(state)
    return triggered, skipped


def default_fbc_sync_annotations(*, trigger_command: str) -> dict[str, str]:
    ann: dict[str, str] = {"rhoai-e2e.trigger-type": TRIGGER_TYPE_RH_NIGHTLY_AUTO}
    if trigger_command.strip():
        ann["rhoai-e2e.trigger-command"] = trigger_command.strip()
    return ann
