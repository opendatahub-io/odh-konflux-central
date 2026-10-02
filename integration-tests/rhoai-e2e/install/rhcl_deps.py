"""RHCL operator install/pin for MaaS (rhoai-e2e install-rhcl-operator.yaml parity)."""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path

from install.approve_transitive_installplans import approve_pending_installplans
from install.dsc_install import oc_run
from install.install_and_verify import (
    patch_manifest_install_plan_automatic,
    patch_manifest_starting_csv,
    pick_succeeded_csv_version,
)
from install.operator_install_scripts_checkout import resolve_olminstall_dir

_RHCL_NS = "kuadrant-system"
_RHCL_SUB = "rhcl-operator"
_RHCL_MANIFEST = "resources/install-rhcl-operator.yaml"
_FALLBACK_RHCL_CSV = "rhcl-operator.v1.3.4"
_DEFAULT_TIMEOUT_SEC = 600
_DEFAULT_RHCL_POST_INSTALL_RETRY_TIMEOUT_SEC = 900
_RECOVERY_POST_INSTALL_TIMEOUT_CAP_SEC = 600
_STARTING_CSV_RE = re.compile(r'^\s*startingCSV:\s*"?([^"\s]+)"?\s*$')
_KUADRANT_STACK_CSV_PREFIXES = (
    "rhcl-operator",
    "authorino-operator",
    "kuadrant-operator",
    "dns-operator",
    "limitador-operator",
    "cert-manager",
)
_STUCK_INSTALLPLAN_PHASES = frozenset({"Failed", "RequiresApproval"})
_DEFAULT_KUADRANT_NS_READY_TIMEOUT_SEC = 600
_GITOPS_OPERATORGROUP = "kuadrant"
_OLMINSTALL_OPERATORGROUP = "kuadrant-system"


def _operatorgroup_names() -> list[str]:
    r = oc_run(
        [
            "get",
            "operatorgroup",
            "-n",
            _RHCL_NS,
            "-o",
            'jsonpath={range .items[*]}{.metadata.name}{"\\n"}{end}',
        ],
        check=False,
        capture_output=True,
        timeout=30,
    )
    if r.returncode != 0:
        return []
    return [name for name in (r.stdout or "").splitlines() if name.strip()]


def _operatorgroup_multiple_flag(name: str) -> bool:
    r = oc_run(
        ["get", "operatorgroup", name, "-n", _RHCL_NS, "-o", "json"],
        check=False,
        capture_output=True,
        timeout=30,
    )
    if r.returncode != 0:
        return False
    try:
        doc = json.loads(r.stdout or "{}")
    except json.JSONDecodeError:
        return False
    for cond in (doc.get("status") or {}).get("conditions") or []:
        if not isinstance(cond, dict):
            continue
        if (
            str(cond.get("type") or "") == "MultipleOperatorGroup"
            and str(cond.get("status") or "").lower() == "true"
        ):
            return True
    return False


def _apply_gitops_operatorgroup() -> None:
    manifest = (
        "apiVersion: operators.coreos.com/v1\n"
        "kind: OperatorGroup\n"
        f"metadata:\n  name: {_GITOPS_OPERATORGROUP}\n  namespace: {_RHCL_NS}\n"
        "spec:\n  upgradeStrategy: Default\n"
    )
    apply = oc_run(
        ["apply", "-f", "-"],
        stdin_text=manifest,
        check=False,
        capture_output=True,
        timeout=60,
    )
    if apply.returncode != 0:
        err = (apply.stderr or apply.stdout or "").strip()
        raise RuntimeError(f"Could not apply {_GITOPS_OPERATORGROUP} OperatorGroup: {err or 'unknown'}")


def _ensure_kuadrant_namespace_exists() -> None:
    """Create kuadrant-system when missing so OperatorGroup/Subscription can apply."""
    from install.dependency_operators import _namespace_phase

    phase = _namespace_phase(_RHCL_NS)
    if phase == "Active":
        return
    if phase == "Terminating":
        _ensure_kuadrant_namespace_ready()
        if _namespace_phase(_RHCL_NS) == "Active":
            return
    print(f"Creating namespace {_RHCL_NS} for RHCL OperatorGroup...", flush=True)
    create = oc_run(
        [
            "create",
            "namespace",
            _RHCL_NS,
            "--dry-run=client",
            "-o",
            "yaml",
        ],
        check=False,
        capture_output=True,
        timeout=30,
    )
    apply = oc_run(
        ["apply", "-f", "-"],
        stdin_text=create.stdout or f"apiVersion: v1\nkind: Namespace\nmetadata:\n  name: {_RHCL_NS}\n",
        check=False,
        capture_output=True,
        timeout=60,
    )
    if apply.returncode != 0 and _namespace_phase(_RHCL_NS) != "Active":
        err = (apply.stderr or apply.stdout or "").strip()
        raise RuntimeError(f"Could not create namespace {_RHCL_NS}: {err or 'unknown'}")


def reconcile_kuadrant_operator_groups() -> None:
    """Keep a single gitops-style OperatorGroup so RHCL subscriptions get a CSV.

    rhoai-e2e install-rhcl-operator.yaml creates OperatorGroup/kuadrant-system while
    setup-dependencies.sh (odh-gitops) creates OperatorGroup/kuadrant. Multiple groups
    in the same namespace block OLM InstallPlans on pooled QE clusters.

    On fresh EPHC the manifest apply strips OperatorGroup; without one OLM never
    creates an InstallPlan and currentCSV stays unset.
    """
    names = _operatorgroup_names()
    if not names:
        _ensure_kuadrant_namespace_exists()
        print(
            f"Applying OperatorGroup/{_GITOPS_OPERATORGROUP} in {_RHCL_NS} (none present)...",
            flush=True,
        )
        _apply_gitops_operatorgroup()
        return

    if len(names) > 1:
        keep = _GITOPS_OPERATORGROUP if _GITOPS_OPERATORGROUP in names else names[0]
        for name in names:
            if name == keep:
                continue
            print(
                f"Deleting duplicate OperatorGroup/{name} in {_RHCL_NS} (keep {keep})...",
                flush=True,
            )
            oc_run(
                ["delete", "operatorgroup", name, "-n", _RHCL_NS, "--wait=false"],
                check=False,
                capture_output=True,
                timeout=60,
            )
        names = [keep]

    if len(names) == 1 and _operatorgroup_multiple_flag(names[0]):
        print(
            f"Recreating OperatorGroup/{names[0]} in {_RHCL_NS} "
            "(clearing stale MultipleOperatorGroup)...",
            flush=True,
        )
        oc_run(
            ["delete", "operatorgroup", names[0], "-n", _RHCL_NS, "--wait=true"],
            check=False,
            capture_output=True,
            timeout=120,
        )
        _apply_gitops_operatorgroup()


def _rhcl_manifest_apply_text(manifest: Path, target_csv: str) -> str:
    """Manifest apply body without OperatorGroup (gitops/setup-dependencies owns it)."""
    parts: list[str] = []
    for chunk in manifest.read_text(encoding="utf-8").split("---"):
        body = chunk.strip()
        if not body:
            continue
        if re.search(r"^kind:\s*OperatorGroup\s*$", body, re.MULTILINE):
            continue
        parts.append(body)
    return "\n---\n".join(parts) + ("\n" if parts else "")


def rhcl_starting_csv(*, olm_dir: Path | None = None) -> str:
    """Target RHCL CSV from env, rhoai-e2e manifest, or fallback."""
    if override := os.environ.get("RHCL_OPERATOR_STARTING_CSV", "").strip():
        return override
    root = olm_dir
    if root is None and (existing := os.environ.get("OLMINSTALL_DIR", "").strip()):
        root = Path(existing)
    if root is not None and (parsed := _starting_csv_from_manifest(root)):
        return parsed
    return _FALLBACK_RHCL_CSV


def _starting_csv_from_manifest(olm_dir: Path) -> str:
    manifest = olm_dir / _RHCL_MANIFEST
    if not manifest.is_file():
        return ""
    for line in manifest.read_text(encoding="utf-8").splitlines():
        match = _STARTING_CSV_RE.match(line)
        if match:
            return match.group(1)
    return ""


def _rhcl_ready_timeout_sec() -> int:
    raw = os.environ.get("RHCL_OPERATOR_READY_TIMEOUT_SEC", "").strip()
    if raw:
        try:
            return int(raw)
        except ValueError:
            print(
                f"WARN: invalid RHCL_OPERATOR_READY_TIMEOUT_SEC={raw!r}; "
                f"using default {_DEFAULT_TIMEOUT_SEC}s",
                file=sys.stderr,
                flush=True,
            )
    return _DEFAULT_TIMEOUT_SEC


def _oc_jsonpath(args: list[str], *, timeout: int = 30) -> str:
    r = oc_run(args, check=False, capture_output=True, timeout=timeout)
    if r.returncode != 0:
        return ""
    return (r.stdout or "").strip()


def _subscription_current_csv() -> str:
    return _oc_jsonpath(
        ["get", "subscription", _RHCL_SUB, "-n", _RHCL_NS, "-o", "jsonpath={.status.currentCSV}"]
    )


def _subscription_starting_csv() -> str:
    return _oc_jsonpath(
        ["get", "subscription", _RHCL_SUB, "-n", _RHCL_NS, "-o", "jsonpath={.spec.startingCSV}"]
    )


def _csv_phase(name: str) -> str:
    return _oc_jsonpath(["get", "csv", name, "-n", _RHCL_NS, "-o", "jsonpath={.status.phase}"])


def _csv_exists(name: str) -> bool:
    if not name:
        return False
    r = oc_run(
        ["get", "csv", name, "-n", _RHCL_NS],
        check=False,
        capture_output=True,
        timeout=30,
    )
    return r.returncode == 0


def _is_kuadrant_stack_csv(csv_name: str) -> bool:
    base = csv_name.split(".", 1)[0].lower()
    return any(base.startswith(prefix) or prefix in base for prefix in _KUADRANT_STACK_CSV_PREFIXES)


def _delete_stuck_csv(name: str, *, label: str = "CSV") -> None:
    if not name or not _csv_exists(name):
        return
    phase = _csv_phase(name)
    if phase not in ("Failed", "Replacing"):
        return
    print(f"Deleting stuck {label} {name} (phase={phase})...", flush=True)
    oc_run(["delete", "csv", name, "-n", _RHCL_NS], check=False, capture_output=True, timeout=60)


def _delete_stuck_rhcl_csv(name: str) -> None:
    """Remove a Failed RHCL CSV so OLM can recreate from the subscription pin."""
    if not name or not name.startswith("rhcl-operator."):
        return
    _delete_stuck_csv(name, label="RHCL CSV")


def _delete_stuck_kuadrant_stack_csvs(*, keep_csv: str = "") -> None:
    """Remove Failed/Replacing Kuadrant stack CSVs (authorino, dns, limitador, …)."""
    r = oc_run(
        ["get", "csv", "-n", _RHCL_NS, "-o", "json"],
        check=False,
        capture_output=True,
        timeout=60,
    )
    if r.returncode != 0:
        return
    try:
        doc = json.loads(r.stdout or "{}")
    except json.JSONDecodeError:
        return
    for item in doc.get("items") or []:
        if not isinstance(item, dict):
            continue
        meta = item.get("metadata") or {}
        name = str(meta.get("name") or "").strip()
        if not name or name == keep_csv or not _is_kuadrant_stack_csv(name):
            continue
        _delete_stuck_csv(name, label="Kuadrant stack CSV")


def _rhcl_subscription_exists() -> bool:
    return (
        oc_run(
            ["get", "subscription", _RHCL_SUB, "-n", _RHCL_NS],
            check=False,
            capture_output=True,
            timeout=30,
        ).returncode
        == 0
    )


def _delete_rhcl_subscription() -> None:
    if not _rhcl_subscription_exists():
        return
    print(f"Deleting {_RHCL_NS}/{_RHCL_SUB} subscription to clear OLM drift...", flush=True)
    oc_run(
        ["delete", "subscription", _RHCL_SUB, "-n", _RHCL_NS, "--wait=false"],
        check=False,
        capture_output=True,
        timeout=60,
    )


def _purge_blocking_rhcl_installplans(target_csv: str) -> None:
    """Remove InstallPlans that pin or upgrade RHCL away from the manifest target."""
    r = oc_run(
        ["get", "installplan", "-n", _RHCL_NS, "-o", "json"],
        check=False,
        capture_output=True,
        timeout=60,
    )
    if r.returncode != 0:
        return
    try:
        doc = json.loads(r.stdout or "{}")
    except json.JSONDecodeError:
        return

    for item in doc.get("items") or []:
        if not isinstance(item, dict):
            continue
        spec = item.get("spec") or {}
        csvs = [str(c) for c in (spec.get("clusterServiceVersionNames") or []) if c]
        if not _installplan_blocks_rhcl_pin(csvs, target_csv):
            continue
        ip_name = str((item.get("metadata") or {}).get("name") or "").strip()
        if not ip_name:
            continue
        phase = str((item.get("status") or {}).get("phase") or "").strip()
        print(
            f"Deleting RHCL-blocking InstallPlan/{ip_name} in {_RHCL_NS} "
            f"(phase={phase or '?'}, CSVs={csvs})...",
            flush=True,
        )
        oc_run(
            ["delete", "installplan", ip_name, "-n", _RHCL_NS, "--wait=false"],
            check=False,
            capture_output=True,
            timeout=60,
        )


def _installplan_blocks_rhcl_pin(csv_names: list[object], target_csv: str) -> bool:
    """True when an InstallPlan would upgrade RHCL away from the manifest pin."""
    for raw in csv_names:
        csv = str(raw or "").strip()
        if not csv.startswith("rhcl-operator."):
            continue
        if csv != target_csv:
            return True
    return False


def _delete_stuck_kuadrant_installplans(
    target_csv: str,
    *,
    orphan_csvs: set[str] | None = None,
) -> None:
    """Remove InstallPlans stuck on phantom upgrades or failed Kuadrant CSVs."""
    r = oc_run(
        ["get", "installplan", "-n", _RHCL_NS, "-o", "json"],
        check=False,
        capture_output=True,
        timeout=60,
    )
    if r.returncode != 0:
        return
    try:
        doc = json.loads(r.stdout or "{}")
    except json.JSONDecodeError:
        return

    orphans = orphan_csvs or set()
    for item in doc.get("items") or []:
        if not isinstance(item, dict):
            continue
        spec = item.get("spec") or {}
        if spec.get("approved") is True:
            continue
        status = item.get("status") or {}
        phase = str(status.get("phase") or "").strip()
        if phase not in _STUCK_INSTALLPLAN_PHASES:
            continue
        csvs = {str(c) for c in (spec.get("clusterServiceVersionNames") or []) if c}
        if not csvs:
            continue
        blocked = bool(csvs & orphans) or _installplan_blocks_rhcl_pin(list(csvs), target_csv)
        if not blocked:
            continue
        ip_name = str((item.get("metadata") or {}).get("name") or "").strip()
        if not ip_name:
            continue
        print(
            f"Deleting stuck InstallPlan/{ip_name} in {_RHCL_NS} "
            f"(phase={phase}, CSVs={sorted(csvs)})...",
            flush=True,
        )
        oc_run(
            ["delete", "installplan", ip_name, "-n", _RHCL_NS, "--wait=false"],
            check=False,
            capture_output=True,
            timeout=60,
        )


def _succeeded_rhcl_csv_name() -> str | None:
    """Return the newest Succeeded rhcl-operator CSV name in kuadrant-system."""
    version = pick_succeeded_csv_version(_RHCL_NS, "rhcl-operator", timeout=15)
    if not version:
        return None
    name = version if version.startswith("rhcl-operator.") else f"rhcl-operator.v{version}"
    return name if _csv_phase(name) == "Succeeded" else None


def rhcl_stack_functional(*, csv_name: str | None = None) -> bool:
    """True when an RHCL CSV is Succeeded and authorino-operator is installed."""
    name = (csv_name or _subscription_current_csv()).strip()
    if not name:
        return False
    if _csv_phase(name) != "Succeeded":
        return False
    return pick_succeeded_csv_version(_RHCL_NS, "authorino-operator", timeout=30) is not None


def rhcl_operators_ready(target_csv: str) -> bool:
    current = _subscription_current_csv()
    if rhcl_stack_functional(csv_name=current):
        return True
    if not current or current == target_csv:
        if rhcl_stack_functional(csv_name=target_csv):
            return True
    if not current:
        installed = _succeeded_rhcl_csv_name()
        if installed and rhcl_stack_functional(csv_name=installed):
            return True
    return False


def _kuadrant_namespace_ready_timeout_sec() -> int:
    raw = os.environ.get("KUADRANT_NAMESPACE_READY_TIMEOUT_SEC", "").strip()
    if raw:
        try:
            return int(raw)
        except ValueError:
            print(
                f"WARN: invalid KUADRANT_NAMESPACE_READY_TIMEOUT_SEC={raw!r}; "
                f"using default {_DEFAULT_KUADRANT_NS_READY_TIMEOUT_SEC}s",
                file=sys.stderr,
                flush=True,
            )
    return _DEFAULT_KUADRANT_NS_READY_TIMEOUT_SEC


def _ensure_kuadrant_namespace_ready() -> None:
    """Wait for kuadrant-system after cleanup uninstalls (namespace often stuck Terminating)."""
    from install.dependency_operators import _namespace_phase, unblock_terminating_namespace

    timeout_sec = _kuadrant_namespace_ready_timeout_sec()
    deadline = time.time() + timeout_sec
    while time.time() < deadline:
        phase = _namespace_phase(_RHCL_NS)
        if not phase or phase == "Active":
            return
        if phase == "Terminating":
            unblock_terminating_namespace(_RHCL_NS)
        time.sleep(5)
    raise RuntimeError(
        f"Namespace {_RHCL_NS} not ready after {timeout_sec}s "
        f"(phase={_namespace_phase(_RHCL_NS) or 'unknown'})"
    )


def _apply_rhcl_manifest(target_csv: str, *, olm_dir: Path | None = None) -> None:
    olm_dir = olm_dir or resolve_olminstall_dir(require_marker=False)
    _ensure_kuadrant_namespace_ready()
    _ensure_kuadrant_namespace_exists()
    reconcile_kuadrant_operator_groups()
    manifest = olm_dir / _RHCL_MANIFEST
    if not manifest.is_file():
        raise FileNotFoundError(f"Missing {manifest} for RHCL operator install")

    staging = Path(f"/tmp/{manifest.name}")
    staging.write_text(_rhcl_manifest_apply_text(manifest, target_csv), encoding="utf-8")
    patch_manifest_install_plan_automatic(staging)
    if not patch_manifest_starting_csv(staging, target_csv):
        raise RuntimeError(f"Could not patch startingCSV={target_csv} into RHCL manifest {staging}")

    print(f"Applying RHCL operator manifest ({target_csv})...", flush=True)
    apply = oc_run(
        ["apply", "-f", str(staging)],
        check=False,
        capture_output=True,
        timeout=120,
    )
    if apply.returncode != 0:
        err = (apply.stderr or apply.stdout or "").strip()
        raise RuntimeError(f"Could not apply RHCL operator manifest: {err or 'unknown error'}")


def _patch_rhcl_subscription(target_csv: str, *, force: bool = False) -> None:
    starting = _subscription_starting_csv()
    if not force and starting == target_csv:
        return
    print(
        f"Patching {_RHCL_NS}/{_RHCL_SUB} startingCSV "
        f"({starting or 'unset'} -> {target_csv})...",
        flush=True,
    )
    patch = oc_run(
        [
            "patch",
            "subscription",
            _RHCL_SUB,
            "-n",
            _RHCL_NS,
            "--type=merge",
            "-p",
            f'{{"spec":{{"startingCSV":"{target_csv}","installPlanApproval":"Automatic"}}}}',
        ],
        check=False,
        capture_output=True,
        timeout=60,
    )
    if patch.returncode != 0:
        err = (patch.stderr or patch.stdout or "").strip()
        raise RuntimeError(f"Could not patch RHCL subscription: {err or 'unknown error'}")


def _reconcile_stuck_rhcl_subscription(target_csv: str, *, olm_dir: Path | None = None) -> None:
    """Recover pooled-cluster drift: phantom currentCSV or Failed RHCL CSV."""
    current = _subscription_current_csv()
    orphan_csvs: set[str] = set()
    if current and not _csv_exists(current):
        orphan_csvs.add(current)

    _delete_stuck_kuadrant_stack_csvs(keep_csv=target_csv if _csv_phase(target_csv) == "Succeeded" else "")
    for csv_name in {current, target_csv}:
        if csv_name:
            _delete_stuck_rhcl_csv(csv_name)
    _purge_blocking_rhcl_installplans(target_csv)
    _delete_stuck_kuadrant_installplans(target_csv, orphan_csvs=orphan_csvs)

    current_missing = bool(current) and not _csv_exists(current)
    current_bad = bool(current) and _csv_exists(current) and _csv_phase(current) != "Succeeded"
    needs_reset = current_missing or current_bad or (bool(current) and current != target_csv)
    if needs_reset:
        print(
            f"Reconciling RHCL subscription "
            f"(currentCSV={current or '?'}, target={target_csv})...",
            flush=True,
        )
        _delete_rhcl_subscription()
        _apply_rhcl_manifest(target_csv, olm_dir=olm_dir)
    else:
        _patch_rhcl_subscription(target_csv, force=False)


def _wait_rhcl_operators_ready(target_csv: str, *, timeout_sec: int) -> None:
    deadline = time.time() + timeout_sec
    while time.time() < deadline:
        current = _subscription_current_csv()
        if rhcl_stack_functional(csv_name=current):
            label = current if current and current != target_csv else target_csv
            print(f"✓ RHCL operator ready ({label} + authorino-operator)", flush=True)
            return

        if not current:
            installed = _succeeded_rhcl_csv_name()
            if installed and rhcl_stack_functional(csv_name=installed):
                print(
                    f"✓ RHCL operator ready ({installed} + authorino-operator; "
                    f"subscription currentCSV unset)",
                    flush=True,
                )
                return

        rhcl_phase = _csv_phase(current) if current else ""
        authorino_ver = pick_succeeded_csv_version(_RHCL_NS, "authorino-operator", timeout=15)
        if int(time.time()) % 30 < 12:
            print(
                f"Waiting for RHCL operators "
                f"(currentCSV={current or '?'} phase={rhcl_phase or '?'} "
                f"authorino={authorino_ver or 'pending'})...",
                flush=True,
            )
        approve_pending_installplans(_RHCL_NS)
        time.sleep(15)

    raise RuntimeError(
        f"RHCL operator not ready after {timeout_sec}s "
        f"(expected {_RHCL_NS}/{_RHCL_SUB} currentCSV={target_csv}, "
        f"actual currentCSV={_subscription_current_csv() or 'unset'})"
    )


def ensure_rhcl_operator_for_maas(*, olm_dir: Path | None = None) -> None:
    """Ensure RHCL/Authorino operators match rhoai-e2e MaaS pin."""
    _ensure_kuadrant_namespace_exists()
    reconcile_kuadrant_operator_groups()
    target_csv = rhcl_starting_csv(olm_dir=olm_dir)
    timeout_sec = _rhcl_ready_timeout_sec()

    if rhcl_operators_ready(target_csv):
        current = _subscription_current_csv()
        if current and current != target_csv:
            print(
                f"✓ RHCL operator ready at {current} (gitops; manifest pin {target_csv})",
                flush=True,
            )
        else:
            print(f"✓ RHCL operator already at {target_csv}", flush=True)
        return

    sub = oc_run(
        ["get", "subscription", _RHCL_SUB, "-n", _RHCL_NS],
        check=False,
        capture_output=True,
        timeout=30,
    )
    if sub.returncode != 0:
        if olm_dir is not None:
            _apply_rhcl_manifest(target_csv, olm_dir=olm_dir)
        else:
            _apply_rhcl_manifest(target_csv)
    else:
        root = olm_dir or resolve_olminstall_dir(require_marker=False)
        _reconcile_stuck_rhcl_subscription(target_csv, olm_dir=root)

    approved = approve_pending_installplans(_RHCL_NS)
    if approved:
        print(f"✓ Approved {approved} RHCL InstallPlan(s) in {_RHCL_NS}", flush=True)

    _wait_rhcl_operators_ready(target_csv, timeout_sec=timeout_sec)


def post_install_rhcl_needed() -> bool:
    """False when Authorino workload and TLS are already configured on cluster."""
    from components.maas_billing.auth import authorino_workload_tls_ready

    return not authorino_workload_tls_ready()


def run_post_install_rhcl_operator(
    *,
    fatal: bool = False,
    olm_dir: Path | None = None,
    timeout_sec: int | None = None,
) -> bool:
    """Run rhoai-e2e post-install-rhcl-operator.sh (Kuadrant wait + Authorino TLS)."""
    try:
        root = olm_dir or resolve_olminstall_dir()
    except (FileNotFoundError, RuntimeError) as exc:
        msg = str(exc)
        if fatal:
            raise RuntimeError(msg) from exc
        print(f"WARN: {msg}; skipping post-install-rhcl", file=sys.stderr, flush=True)
        return False
    except SystemExit as exc:
        code = getattr(exc, "code", 1)
        msg = f"rhoai-e2e checkout failed (exit {code})"
        if fatal:
            raise RuntimeError(msg) from exc
        print(f"WARN: {msg}; skipping post-install-rhcl", file=sys.stderr, flush=True)
        return False
    script = root / "resources" / "post-install-rhcl-operator.sh"
    print(f"Running {script.name} for Kuadrant/Authorino readiness...", flush=True)
    if timeout_sec is not None:
        timeout = timeout_sec
    else:
        raw_timeout = os.environ.get("RHCL_POST_INSTALL_TIMEOUT_SEC", "1800").strip()
        try:
            timeout = int(raw_timeout)
        except ValueError:
            timeout = 1800
            print(
                f"WARN: invalid RHCL_POST_INSTALL_TIMEOUT_SEC={raw_timeout!r}; using default {timeout}s",
                file=sys.stderr,
                flush=True,
            )
    try:
        proc = subprocess.run(
            ["bash", str(script)],
            env=os.environ.copy(),
            check=False,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        msg = f"{script.name} timed out after {timeout}s"
        if fatal:
            raise RuntimeError(msg) from None
        print(f"WARN: {msg}; continuing with Kuadrant/AuthPolicy recovery", file=sys.stderr, flush=True)
        return False
    if proc.returncode != 0:
        msg = f"{script.name} exited {proc.returncode}"
        if fatal:
            raise RuntimeError(msg)
        print(f"WARN: {msg}; continuing with Kuadrant/AuthPolicy recovery", file=sys.stderr, flush=True)
        return False
    return True


def reconcile_rhcl_after_gitops_apply(*, olm_dir: Path | None = None) -> None:
    """Re-pin RHCL after odh-gitops apply inside setup-dependencies.sh.

    Konflux install-dep-operators mirrors Jenkins: setup-dependencies.sh first, then
    RHCL CSV pin from the rhoai-e2e manifest. GitOps apply can upgrade RHCL (e.g.
    v1.4.0) during setup-dependencies; reconcile immediately after that apply so
    pooled clusters are not left on a failed upgrade before finalize recovery.
    """
    print("Reconciling RHCL CSV after gitops dependency apply...", flush=True)
    ensure_rhcl_operator_for_maas(olm_dir=olm_dir)


def _parse_rhcl_post_install_retry_timeout_sec() -> int:
    raw = os.environ.get(
        "RHCL_POST_INSTALL_RETRY_TIMEOUT_SEC",
        str(_DEFAULT_RHCL_POST_INSTALL_RETRY_TIMEOUT_SEC),
    ).strip()
    try:
        return int(raw)
    except ValueError:
        print(
            f"WARN: invalid RHCL_POST_INSTALL_RETRY_TIMEOUT_SEC={raw!r}; "
            f"using {_DEFAULT_RHCL_POST_INSTALL_RETRY_TIMEOUT_SEC}s",
            file=sys.stderr,
            flush=True,
        )
        return _DEFAULT_RHCL_POST_INSTALL_RETRY_TIMEOUT_SEC


def _clear_maas_gateway_stack_marker() -> None:
    from helpers.gateway_stack_marker import clear_gateway_stack_incomplete_marker

    clear_gateway_stack_incomplete_marker()


def ensure_maas_rhcl_dependency_stack(*, olm_dir: Path | None = None) -> None:
    """Pin RHCL CSV and run post-install script (install-dep-operators happy path)."""
    from components.maas_billing.gateway import wait_openshift_default_gateway_class_accepted
    from install.dependency_operators import authorino_deferred_to_component_prep

    ensure_rhcl_operator_for_maas(olm_dir=olm_dir)
    # EPHC/HyperShift: GatewayClass may not exist until ingress gateway controller starts.
    # post-install-rhcl needs Accepted=True to avoid Kuadrant MissingDependency stuck state.
    if not wait_openshift_default_gateway_class_accepted():
        print(
            "WARN: openshift-default GatewayClass not Accepted before post-install-rhcl; "
            "Kuadrant may stay MissingDependency",
            file=sys.stderr,
            flush=True,
        )
    defer_authorino = authorino_deferred_to_component_prep()
    post_kwargs: dict[str, object] = {"fatal": not defer_authorino}
    if olm_dir is not None:
        post_kwargs["olm_dir"] = olm_dir
    if run_post_install_rhcl_operator(**post_kwargs):
        _clear_maas_gateway_stack_marker()
        return
    if defer_authorino:
        print(
            "WARN: post-install-rhcl-operator.sh incomplete; "
            "retrying Kuadrant/Authorino readiness before component tests",
            file=sys.stderr,
            flush=True,
        )
        retry_timeout = _parse_rhcl_post_install_retry_timeout_sec()
        retry_kwargs = dict(post_kwargs)
        retry_kwargs["fatal"] = False
        retry_kwargs["timeout_sec"] = retry_timeout
        if run_post_install_rhcl_operator(**retry_kwargs):
            _clear_maas_gateway_stack_marker()
            print("✓ post-install-rhcl-operator.sh succeeded on retry", flush=True)
            return
        olm = olm_dir or resolve_olminstall_dir()
        try:
            from install.dependency_operators import _ensure_authorino_operators_after_setup

            print(
                "Retrying Kuadrant/Authorino via odh-gitops recovery after post-install failure...",
                flush=True,
            )
            _ensure_authorino_operators_after_setup(olm, setup_rc=1)
        except Exception as exc:
            print(
                f"WARN: Authorino odh-gitops recovery failed ({exc}); continuing",
                file=sys.stderr,
                flush=True,
            )
        recovery_kwargs = dict(retry_kwargs)
        recovery_kwargs["timeout_sec"] = min(
            retry_timeout,
            _RECOVERY_POST_INSTALL_TIMEOUT_CAP_SEC,
        )
        if run_post_install_rhcl_operator(**recovery_kwargs):
            _clear_maas_gateway_stack_marker()
            print("✓ post-install-rhcl-operator.sh succeeded after odh-gitops recovery", flush=True)
            return
        # post-install often races GatewayClass; restart Kuadrant once provider exists.
        try:
            from components.maas_billing.auth import recover_kuadrant_after_gateway_api_provider
            from components.maas_billing.gateway import ensure_openshift_default_gateway_class

            ensure_openshift_default_gateway_class()
            if recover_kuadrant_after_gateway_api_provider():
                _clear_maas_gateway_stack_marker()
                print(
                    "✓ Kuadrant Ready after Gateway API provider recovery "
                    "(install-dep-operators)",
                    flush=True,
                )
                return
        except Exception as exc:
            print(
                f"WARN: Kuadrant Gateway API provider recovery failed ({exc})",
                file=sys.stderr,
                flush=True,
            )
        from helpers.gateway_stack_marker import write_gateway_stack_incomplete_marker

        write_gateway_stack_incomplete_marker()
        print(
            "ERROR: Kuadrant auth stack not ready after post-install retry; "
            "gateway 503 risk for dashboard_cypress",
            file=sys.stderr,
            flush=True,
        )
        return
    raise RuntimeError("post-install-rhcl-operator.sh failed")
