"""Delete/stop incomplete rhoai-e2e PipelineRuns (--delete-pending-pipelines)."""

from __future__ import annotations

import sys
from typing import Any
from urllib.parse import quote

from suite.errors import AppError
from k8s.oc_util import parse_json_output, run_cmd

from .runner_support import (
    _snapshot_param_is_resource_name,
    filter_pipelinerun_items,
    pipelinerun_delete_candidate,
    pipelinerun_has_started_tasks,
    pipelinerun_list_state,
    pipelinerun_snapshot_param,
    try_cancel_pipelinerun,
)


class RunnerDeleteMixin:
    def _rhoai_e2e_pipelinerun_items(self, *, source: str) -> list[dict[str, Any]]:
        """Live or archived PipelineRun objects for ``--app`` (rhoai-e2e name, not smoke-only)."""
        if source == "live":
            items = self.get_pipelineruns(self.args.namespace)
        elif self.ka_available():
            assert self.ka is not None
            path = (
                f"/apis/tekton.dev/v1/namespaces/{quote(self.args.namespace)}/pipelineruns"
                f"?labelSelector={quote(f'appstudio.openshift.io/application={self.args.app}')}&limit=50"
            )
            data = self._ka_get_json_warn_empty(path, ctx="archived PipelineRuns for delete scan")
            items = data.get("items", [])
        else:
            items = []
        return filter_pipelinerun_items(items, app=self.args.app, rhoai_e2e_only=True)

    def _delete_pending_targets_from_items(
        self,
        items: list[dict[str, Any]],
        *,
        stop_owned_running: bool = False,
        include_unowned_stuck: bool = False,
    ) -> list[tuple[str, str, dict[str, Any]]]:
        targets: list[tuple[str, str, dict[str, Any]]] = []
        for item in items:
            snap = pipelinerun_snapshot_param(item)
            snapshot_owner = ""
            if _snapshot_param_is_resource_name(snap):
                snapshot_owner = self.get_snapshot_owner(snap)
            delete, why = pipelinerun_delete_candidate(
                item,
                app=self.args.app,
                run_owner=self.run_owner,
                snapshot_owner=snapshot_owner,
                stop_owned_running=stop_owned_running,
                include_unowned_stuck=include_unowned_stuck,
            )
            if delete:
                name = item.get("metadata", {}).get("name", "")
                state = pipelinerun_list_state(item)
                targets.append((name, f"{why} ({state})", item))
        return targets

    def _fresh_pipelinerun_item(self, name: str) -> dict[str, Any] | None:
        try:
            data = parse_json_output(
                ["oc", "get", "pipelinerun", name, "-n", self.args.namespace, "-o", "json"],
            )
        except Exception:
            return None
        return data if data.get("metadata") else None

    def _stop_pipelinerun(self, name: str, why: str, item: dict[str, Any]) -> bool:
        """Cancel (when tasks started) then delete a live PipelineRun. Returns True if deleted."""
        live_item = self._fresh_pipelinerun_item(name) or item
        if pipelinerun_has_started_tasks(live_item) or pipelinerun_list_state(live_item) == "running":
            ok, detail = try_cancel_pipelinerun(name, self.args.namespace)
            if ok:
                print(f"  cancelled {name} ({why}): {detail}")
            elif detail != "tkn not in PATH":
                print(f"  WARN cancel {name} failed ({detail}); deleting anyway", file=sys.stderr)
            else:
                print(f"  WARN tkn not in PATH; hard-deleting {name}", file=sys.stderr)
        proc = run_cmd(
            ["oc", "delete", "pipelinerun", name, "-n", self.args.namespace, "--ignore-not-found"],
            capture=True,
            check=False,
        )
        if proc.returncode == 0 and "deleted" in (proc.stdout or "").lower():
            print(f"  deleted {name} ({why})")
            return True
        if proc.returncode == 0:
            print(f"  absent {name} ({why})")
            return True
        detail = (proc.stderr or proc.stdout or "").strip()
        print(f"  WARN failed to delete {name}: {detail or proc.returncode}", file=sys.stderr)
        return False

    def _print_delete_pending_empty_hint(self, live_items: list[dict[str, Any]]) -> None:
        print(f"No live incomplete rhoai-e2e PipelineRuns to stop for app '{self.args.app}'.")
        if not live_items:
            print(
                "No rhoai-e2e PipelineRuns remain on the cluster (they are pruned quickly after completion). "
                "``-l`` may still show archived history via KubeArchive."
            )
        else:
            print(f"Found {len(live_items)} live rhoai-e2e run(s), but none match pending/owned/stuck delete rules.")
            for item in sorted(
                live_items,
                key=lambda x: x.get("metadata", {}).get("creationTimestamp", ""),
                reverse=True,
            )[:5]:
                name = item.get("metadata", {}).get("name", "")
                state = pipelinerun_list_state(item)
                print(f"  {name}\t{state}")
            if not getattr(self.args, "stop_owned_running", False):
                owned_running = [
                    item
                    for item in live_items
                    if pipelinerun_delete_candidate(
                        item,
                        app=self.args.app,
                        run_owner=self.run_owner,
                        snapshot_owner="",
                        stop_owned_running=True,
                    )[0]
                    and pipelinerun_has_started_tasks(item)
                ]
                if owned_running:
                    print(
                        "Tip: pass --stop-owned-running to cancel+delete your owned runs that are "
                        "actively Running (same as Konflux UI Stop/Cancel), then remove the PipelineRun."
                    )
            if not getattr(self.args, "include_unowned_stuck", False):
                print(
                    "Tip: pass --include-unowned-stuck to stop rhoai-e2e runs stuck with no TaskRuns "
                    "that lack your rhoai-e2e.run-owner marker (shared tenant only)."
                )
        if not self.ka_available():
            return
        archived = self._rhoai_e2e_pipelinerun_items(source="archived")
        archived_targets = self._delete_pending_targets_from_items(
            archived,
            include_unowned_stuck=getattr(self.args, "include_unowned_stuck", False),
        )
        if not archived_targets:
            return
        print(
            f"KubeArchive still has {len(archived_targets)} incomplete rhoai-e2e record(s) for this app "
            "(already removed from the cluster; ``oc delete`` cannot update or remove them):"
        )
        for name, why, _item in sorted(archived_targets, key=lambda target: target[0], reverse=True)[:10]:
            print(f"  {name}\t{why}")
        print(
            "Konflux Activity may still list these as Pending/Running. That is a KubeArchive/UI display "
            "limitation for pruned runs without completionTime. Do not recreate PipelineRuns by name to "
            "fix them; that adds duplicate archive records. Ignore the rows or ask platform admin."
        )

    def delete_pending_pipelines(self) -> int:
        """Stop incomplete rhoai-e2e PipelineRuns (pending quota + owned + optional stuck without tasks)."""
        if not self.run_owner:
            raise AppError("Not logged in; run oc login before --delete-pending-pipelines.", 1)
        stop_owned_running = bool(getattr(self.args, "stop_owned_running", False))
        include_unowned_stuck = bool(getattr(self.args, "include_unowned_stuck", False))
        dry_run = bool(getattr(self.args, "dry_run", False))
        live_items = self._rhoai_e2e_pipelinerun_items(source="live")
        targets = self._delete_pending_targets_from_items(
            live_items,
            stop_owned_running=stop_owned_running,
            include_unowned_stuck=include_unowned_stuck,
        )
        if not targets:
            self._print_delete_pending_empty_hint(live_items)
            return 0
        label = "Would stop" if dry_run else "Stopping"
        print(f"{label} {len(targets)} PipelineRun(s) in {self.args.namespace}:")
        if dry_run:
            for name, why, _item in sorted(targets):
                print(f"  {name}\t{why}")
            print("Dry run only; no cancel or delete performed.")
            return 0
        deleted = 0
        for name, why, item in sorted(targets):
            if self._stop_pipelinerun(name, why, item):
                deleted += 1
        print(f"Done ({deleted} deleted).")
        return 0 if deleted == len(targets) else 1
