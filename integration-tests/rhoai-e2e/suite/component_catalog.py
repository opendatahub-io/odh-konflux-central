"""Load rhoai-e2e-components-smoke.yaml (component smoke catalog)."""

from __future__ import annotations

from pathlib import Path

from .errors import AppError
from .tests_config import load_yaml_document

from .component_catalog_models import (
    ComponentsSmokeCatalog,
    SmokeComponent,
    default_components_smoke_config_path,
)
from .component_catalog_parse import load_v2_component, parse_catalog_konflux, parse_catalog_konflux_timeout_defaults

__all__ = [
    "ComponentsSmokeCatalog",
    "SmokeComponent",
    "default_components_smoke_config_path",
    "load_components_smoke_catalog",
    "merged_setup_dependencies_args",
    "resolve_shift_left_env_secret",
    "resolve_shift_left_env_secret_for_prepare",
    "selected_components_need_minimal_deps",
    "selected_components_need_shift_left_env",
    "shift_left_env_secret_for_component",
]


def load_components_smoke_catalog(path: Path) -> ComponentsSmokeCatalog:
    doc = load_yaml_document(path)
    ver = doc.get("schemaVersion")
    if ver != 2:
        raise AppError(f"Unsupported components smoke schemaVersion {ver!r} in {path} (expected 2).", 2)

    raw = doc.get("components")
    if not isinstance(raw, list) or not raw:
        raise AppError(f"components must be a non-empty list in {path}", 2)

    ids: list[str] = []
    components: dict[str, SmokeComponent] = {}
    for i, item in enumerate(raw):
        if not isinstance(item, dict):
            raise AppError(f"components[{i}] must be a mapping in {path}", 2)
        comp = load_v2_component(item, path, i)
        if comp.id in ids:
            raise AppError(f"Duplicate component id {comp.id!r} in {path}", 2)
        ids.append(comp.id)
        components[comp.id] = comp

    shift_left_secret = parse_catalog_konflux(doc, path)
    gate_timeout_defaults = parse_catalog_konflux_timeout_defaults(doc, path)

    return ComponentsSmokeCatalog(
        schema_version=int(ver),
        component_ids=tuple(ids),
        components=components,
        shift_left_env_secret=shift_left_secret,
        default_component_test_timeout_by_gate=gate_timeout_defaults,
    )


def selected_components_need_minimal_deps(
    selected_ids: frozenset[str], catalog: ComponentsSmokeCatalog
) -> bool:
    return any(catalog.components[c].requires_minimal_deps for c in selected_ids if c in catalog.components)


def selected_components_need_shift_left_env(
    selected_ids: frozenset[str], catalog: ComponentsSmokeCatalog
) -> bool:
    return any(
        catalog.components[c].requires_shift_left_env
        for c in selected_ids
        if c in catalog.components
    )


def shift_left_env_secret_for_component(
    catalog: ComponentsSmokeCatalog,
    component_id: str,
) -> str:
    """Tenant Secret for one catalog component (per-task pytest mount)."""
    comp = catalog.components.get(component_id)
    if comp is None or not comp.requires_shift_left_env:
        return ""
    override = comp.shift_left_env_secret.strip()
    if override:
        return override
    return (catalog.shift_left_env_secret or "").strip()


def resolve_shift_left_env_secret_for_prepare(
    catalog: ComponentsSmokeCatalog,
    *,
    selected_ids: frozenset[str],
) -> str:
    """Pipeline-wide secret for opendatahub-tests-prepare / cluster prep (MaaS, model stack)."""
    if not selected_components_need_shift_left_env(selected_ids, catalog):
        return ""
    catalog_default = (catalog.shift_left_env_secret or "").strip()
    for cid in selected_ids:
        if cid not in catalog.components:
            continue
        comp = catalog.components[cid]
        if not comp.requires_shift_left_env:
            continue
        override = comp.shift_left_env_secret.strip()
        if not override or override == catalog_default:
            if catalog_default:
                return catalog_default
    return ""


def resolve_shift_left_env_secret(
    catalog: ComponentsSmokeCatalog,
    *,
    selected_ids: frozenset[str],
    explicit: str = "",
) -> str:
    """Tenant Secret for prepare steps; per-component pytest tasks use shift_left_env_secret_for_component."""
    if (explicit or "").strip():
        return explicit.strip()
    return resolve_shift_left_env_secret_for_prepare(catalog, selected_ids=selected_ids)


def merged_setup_dependencies_args(
    selected_ids: frozenset[str], catalog: ComponentsSmokeCatalog
) -> str:
    """Args for setup-dependencies.sh from selected components (empty = full; -M = minimal)."""
    args: list[str] = []
    for cid in selected_ids:
        comp = catalog.components.get(cid)
        if comp is None or not comp.requires_minimal_deps:
            continue
        args.append(comp.setup_dependencies_args)
    if not args:
        return ""
    if any(a == "" for a in args) or len(set(args)) > 1:
        return ""
    return args[0]
