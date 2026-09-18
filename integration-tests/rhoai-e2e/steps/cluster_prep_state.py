"""Shared markers for cluster prep across Tekton tasks (prepare vs install-dep-operators)."""

from __future__ import annotations

import os
import time
from pathlib import Path

_CLUSTER_PREP_MARKER = ".cluster-prep-done"
_DEP_OPERATORS_MARKER = ".dep-operators-done"
_MAAS_SURFACE_MARKER = ".maas-smoke-surface-done"
_MAAS_PREP_ATTEMPTED_MARKER = ".maas-smoke-prep-attempted"
_MAAS_GATEWAY_MAS_MARKER = ".maas-gateway-mas-done"
_MAAS_GATEWAY_HTTPS_FAILED_MARKER = ".maas-gateway-https-failed"
_CLUSTER_API_UNREACHABLE_MARKER = ".cluster-api-unreachable"
_IDP_ATTEMPTED_MARKER = ".identity-providers-attempted"
_DEFAULT_MARKER_MAX_AGE_SEC = 48 * 3600


def _artifacts_dir(explicit: Path | None = None) -> Path | None:
    if explicit is not None:
        return explicit
    raw = os.environ.get("ARTIFACTS_DIR", "").strip()
    return Path(raw) if raw else None


def _artifacts_from_tests_shared() -> Path | None:
    raw = os.environ.get("TESTS_SHARED", "").strip()
    if not raw:
        return None
    return Path(raw) / "tests-payload" / "results"


def resolve_artifacts_dir(explicit: Path | None = None) -> Path | None:
    """Artifacts root for markers (ARTIFACTS_DIR preferred, else tests-shared layout)."""
    return _artifacts_dir(explicit) or _artifacts_from_tests_shared()


def _truthy_env(name: str) -> bool:
    return os.environ.get(name, "").strip().lower() in ("1", "true", "yes")


def _current_pipelinerun_id() -> str:
    for key in (
        "PIPELINE_RUN_UID",
        "PIPELINERUN_UID",
        "PIPELINE_RUN_NAME",
        "PIPELINERUN",
        "PIPELINE_RUN",
    ):
        value = os.environ.get(key, "").strip()
        if value:
            return value
    tekton_name = Path("/etc/tekton/pipelineRunName")
    if tekton_name.is_file():
        return tekton_name.read_text(encoding="utf-8").strip()
    return ""


def _marker_max_age_sec() -> int:
    raw = os.environ.get("MARKER_MAX_AGE_SEC", "").strip()
    if raw:
        try:
            return int(raw)
        except ValueError:
            return _DEFAULT_MARKER_MAX_AGE_SEC
    return _DEFAULT_MARKER_MAX_AGE_SEC


def _parse_marker(path: Path) -> tuple[str, float]:
    """Return (pipelinerun_id, unix_timestamp) from marker body."""
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return "", 0.0
    run_id = ""
    stamped = 0.0
    for line in text.splitlines():
        if line.startswith("pipelinerun="):
            run_id = line.split("=", 1)[1].strip()
        elif line.startswith("ts="):
            try:
                stamped = float(line.split("=", 1)[1].strip())
            except ValueError:
                stamped = 0.0
    if not stamped and path.is_file():
        try:
            stamped = path.stat().st_mtime
        except OSError:
            stamped = 0.0
    if not run_id and text.strip() == "ok":
        return "", stamped
    return run_id, stamped


def _marker_valid(path: Path) -> bool:
    if not path.is_file():
        return False
    if _truthy_env("CLUSTER_PREP_FORCE"):
        return False
    run_id, stamped = _parse_marker(path)
    if stamped and (time.time() - stamped) > _marker_max_age_sec():
        return False
    current = _current_pipelinerun_id()
    if current and run_id and run_id != current:
        return False
    return True


def _write_marker(path: Path) -> None:
    run_id = _current_pipelinerun_id()
    lines = [f"ts={time.time():.0f}"]
    if run_id:
        lines.append(f"pipelinerun={run_id}")
    path.write_text("\n".join(lines) + "\n", encoding="ascii")


def clear_cluster_prep_markers(artifacts_dir: Path | None = None) -> None:
    """Remove cluster-prep, dep-operators, and MaaS-surface markers (CLUSTER_PREP_FORCE / parse step)."""
    root = resolve_artifacts_dir(artifacts_dir)
    if root is None:
        return
    for name in (
        _CLUSTER_PREP_MARKER,
        _DEP_OPERATORS_MARKER,
        _MAAS_SURFACE_MARKER,
        _MAAS_PREP_ATTEMPTED_MARKER,
        _MAAS_GATEWAY_MAS_MARKER,
        _MAAS_GATEWAY_HTTPS_FAILED_MARKER,
        _CLUSTER_API_UNREACHABLE_MARKER,
        _IDP_ATTEMPTED_MARKER,
    ):
        try:
            (root / name).unlink(missing_ok=True)
        except OSError:
            pass


def cluster_prep_already_done(artifacts_dir: Path | None = None) -> bool:
    """True after full component cluster prep finished for this PipelineRun."""
    root = resolve_artifacts_dir(artifacts_dir)
    return root is not None and _marker_valid(root / _CLUSTER_PREP_MARKER)


def mark_cluster_prep_done(artifacts_dir: Path | None = None) -> None:
    """Record that component cluster prep finished."""
    root = resolve_artifacts_dir(artifacts_dir)
    if root is None:
        return
    root.mkdir(parents=True, exist_ok=True)
    _write_marker(root / _CLUSTER_PREP_MARKER)


def dep_operators_already_done(artifacts_dir: Path | None = None) -> bool:
    """True after install-dep-operators ran RHCL/dependency-operator setup for this run."""
    root = resolve_artifacts_dir(artifacts_dir)
    return root is not None and _marker_valid(root / _DEP_OPERATORS_MARKER)


def mark_dep_operators_done(artifacts_dir: Path | None = None) -> None:
    """Record that dependency operators (RHCL/Authorino stack) are ready."""
    root = resolve_artifacts_dir(artifacts_dir)
    if root is None:
        return
    root.mkdir(parents=True, exist_ok=True)
    _write_marker(root / _DEP_OPERATORS_MARKER)


def maas_smoke_surface_already_done(artifacts_dir: Path | None = None) -> bool:
    """True after MaaS gateway/auth surface prep finished for this PipelineRun."""
    root = resolve_artifacts_dir(artifacts_dir)
    return root is not None and _marker_valid(root / _MAAS_SURFACE_MARKER)


def mark_maas_smoke_surface_done(artifacts_dir: Path | None = None) -> None:
    """Record that MaaS smoke surface prep (gateway, DB, auth) finished."""
    root = resolve_artifacts_dir(artifacts_dir)
    if root is None:
        return
    root.mkdir(parents=True, exist_ok=True)
    _write_marker(root / _MAAS_SURFACE_MARKER)


def maas_smoke_prep_attempted(artifacts_dir: Path | None = None) -> bool:
    """True after MaaS auth/readiness wait ran once this PipelineRun (success or timeout)."""
    root = resolve_artifacts_dir(artifacts_dir)
    return root is not None and _marker_valid(root / _MAAS_PREP_ATTEMPTED_MARKER)


def mark_maas_smoke_prep_attempted(artifacts_dir: Path | None = None) -> None:
    """Record that full MaaS auth/readiness prep was attempted (skip duplicate 600s waits)."""
    root = resolve_artifacts_dir(artifacts_dir)
    if root is None:
        return
    root.mkdir(parents=True, exist_ok=True)
    _write_marker(root / _MAAS_PREP_ATTEMPTED_MARKER)


def maas_gateway_mas_already_done(artifacts_dir: Path | None = None) -> bool:
    """True after gateway HTTPS wait + modelsAsService patch succeeded this PipelineRun."""
    root = resolve_artifacts_dir(artifacts_dir)
    return root is not None and _marker_valid(root / _MAAS_GATEWAY_MAS_MARKER)


def mark_maas_gateway_mas_done(artifacts_dir: Path | None = None) -> None:
    """Record that MaaS gateway HTTPS service was ready and modelsAsService was enabled."""
    root = resolve_artifacts_dir(artifacts_dir)
    if root is None:
        return
    root.mkdir(parents=True, exist_ok=True)
    _write_marker(root / _MAAS_GATEWAY_MAS_MARKER)


_MAAS_GATEWAY_HTTPS_NOT_READY = "MaaS gateway HTTPS service not ready"


def maas_gateway_https_failed_reason(artifacts_dir: Path | None = None) -> str:
    """Non-empty when a prior MaaS gateway HTTPS wait failed this PipelineRun."""
    root = resolve_artifacts_dir(artifacts_dir)
    if root is None:
        return ""
    path = root / _MAAS_GATEWAY_HTTPS_FAILED_MARKER
    if not _marker_valid(path):
        return ""
    try:
        text = path.read_text(encoding="utf-8").strip()
    except OSError:
        return ""
    for line in text.splitlines():
        if line.startswith("reason="):
            return line.split("=", 1)[1].strip()
    return text.splitlines()[0].strip() if text else ""


def mark_maas_gateway_https_failed(reason: str, artifacts_dir: Path | None = None) -> None:
    """Record that MaaS gateway HTTPS wait failed so later tasks fail fast."""
    root = resolve_artifacts_dir(artifacts_dir)
    if root is None:
        return
    root.mkdir(parents=True, exist_ok=True)
    path = root / _MAAS_GATEWAY_HTTPS_FAILED_MARKER
    run_id = _current_pipelinerun_id()
    snippet = (reason or "MaaS gateway HTTPS not ready").strip().replace("\n", " ")[:400]
    lines = [f"ts={time.time():.0f}", f"reason={snippet}"]
    if run_id:
        lines.append(f"pipelinerun={run_id}")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def cluster_api_unreachable_marker_reason(artifacts_dir: Path | None = None) -> str:
    """Non-empty when a prior smoke task recorded guest API/ELB DNS death this PipelineRun."""
    root = resolve_artifacts_dir(artifacts_dir)
    if root is None:
        return ""
    path = root / _CLUSTER_API_UNREACHABLE_MARKER
    if not _marker_valid(path):
        return ""
    try:
        text = path.read_text(encoding="utf-8").strip()
    except OSError:
        return ""
    for line in text.splitlines():
        if line.startswith("reason="):
            return line.split("=", 1)[1].strip()
    return text.splitlines()[0].strip() if text else ""


def mark_cluster_api_unreachable(reason: str, artifacts_dir: Path | None = None) -> None:
    """Record EPHC guest API loss so later component tasks skip reconcile/pytest quickly."""
    root = resolve_artifacts_dir(artifacts_dir)
    if root is None:
        return
    snippet = (reason or "cluster API unreachable").strip().replace("\n", " ")[:400]
    if not snippet.lower().startswith("cluster api unreachable"):
        snippet = f"cluster API unreachable: {snippet}"
    root.mkdir(parents=True, exist_ok=True)
    path = root / _CLUSTER_API_UNREACHABLE_MARKER
    run_id = _current_pipelinerun_id()
    lines = [f"ts={time.time():.0f}", f"reason={snippet}"]
    if run_id:
        lines.append(f"pipelinerun={run_id}")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def clear_cluster_api_unreachable_marker(artifacts_dir: Path | None = None) -> None:
    """Drop guest API-death marker after a successful re-probe (transient blip recovery)."""
    root = resolve_artifacts_dir(artifacts_dir)
    if root is None:
        return
    try:
        (root / _CLUSTER_API_UNREACHABLE_MARKER).unlink(missing_ok=True)
    except OSError:
        pass


def maas_gateway_https_blocked_reason() -> str:
    """Infra reason when Kuadrant stack or a prior HTTPS wait blocks MaaS prep."""
    from helpers.gateway_stack_marker import reconcile_gateway_stack_incomplete_marker

    # Clear stale incomplete marker when live Kuadrant/Authorino recovered after reinstall.
    if not reconcile_gateway_stack_incomplete_marker():
        return (
            "MaaS gateway HTTPS service not ready — Kuadrant auth stack incomplete "
            "(install-dep-operators)"
        )
    prior = maas_gateway_https_failed_reason()
    if prior:
        if prior.lower().startswith(_MAAS_GATEWAY_HTTPS_NOT_READY.lower()):
            return prior
        return f"{_MAAS_GATEWAY_HTTPS_NOT_READY} — {prior}"
    return ""


def identity_providers_already_attempted(artifacts_dir: Path | None = None) -> bool:
    """True after install_identity_providers ran once this PipelineRun."""
    root = resolve_artifacts_dir(artifacts_dir)
    return root is not None and _marker_valid(root / _IDP_ATTEMPTED_MARKER)


def mark_identity_providers_attempted(artifacts_dir: Path | None = None) -> None:
    """Record identity provider install attempt for this PipelineRun."""
    root = resolve_artifacts_dir(artifacts_dir)
    if root is None:
        return
    root.mkdir(parents=True, exist_ok=True)
    _write_marker(root / _IDP_ATTEMPTED_MARKER)
