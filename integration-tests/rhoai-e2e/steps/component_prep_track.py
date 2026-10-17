"""Record which component-prep Tekton branch ran (ephc vs external) for task summaries."""

from __future__ import annotations

import os
from pathlib import Path


def _run_config_dir() -> Path | None:
    tests_shared = os.environ.get("TESTS_SHARED", "").strip()
    if tests_shared:
        return Path(tests_shared) / "run-config"
    artifacts = os.environ.get("ARTIFACTS_DIR", "").strip()
    if artifacts:
        return Path(artifacts).parent.parent / "run-config"
    return None


def resolve_component_prep_track() -> str:
    """Return prep track label: ephc, external, skipped-dep-operators, or unknown."""
    if os.environ.get("RUN_COMPONENT_CLUSTER_PREP_IN_DEP_OPERATORS", "").strip().lower() in (
        "1",
        "true",
        "yes",
    ):
        return "skipped-dep-operators"
    from install.gateway_config import cluster_source_is_ephc

    product = os.environ.get("PRODUCT", "").strip().lower()
    from suite.constants import product_installs_operator

    if cluster_source_is_ephc() and product_installs_operator(product):
        return "ephc"
    source = os.environ.get("CLUSTER_SOURCE", "").strip()
    if source and source != "EPHC":
        return "external"
    from suite.constants import is_test_only_product

    if is_test_only_product(product) and not source:
        return "unknown"
    return "unknown"


def component_prep_log_prefix() -> str:
    track = resolve_component_prep_track()
    if track == "skipped-dep-operators":
        return "[prep-skipped-dep-operators]"
    if track == "ephc":
        return "[prep-ephc]"
    if track == "external":
        return "[prep-external]"
    return "[prep]"


def record_component_prep_track(track: str | None = None) -> str:
    """Persist track for write_task_message; return the track id."""
    label = (track or resolve_component_prep_track()).strip() or "unknown"
    cfg = _run_config_dir()
    if cfg is not None:
        cfg.mkdir(parents=True, exist_ok=True)
        (cfg / "component_prep_track.txt").write_text(label + "\n", encoding="ascii")
    return label


def read_component_prep_track_note() -> str:
    """Human-readable line for opendatahub-tests-prepare TASK_MESSAGE."""
    cfg = _run_config_dir()
    if cfg is None:
        return ""
    path = cfg / "component_prep_track.txt"
    if not path.is_file():
        return ""
    track = path.read_text(encoding="utf-8").strip()
    if track == "ephc":
        return "Component prep: EPHC (prepare-components-prerequisites-ephc)"
    if track == "external":
        return "Component prep: external pooled (prepare-components-prerequisites-external)"
    if track == "skipped-dep-operators":
        return "Component prep: skipped (install-dep-operators)"
    if track:
        return f"Component prep: {track}"
    return ""
