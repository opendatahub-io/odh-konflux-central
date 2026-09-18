"""Validate and normalize the rhoai-e2e pipeline COMPONENTS comma list."""

from __future__ import annotations

from pathlib import Path

from suite.component_catalog import ComponentsSmokeCatalog, default_components_smoke_config_path, load_components_smoke_catalog
from suite.errors import AppError
from suite.tests_config import TestsCatalog


def components_csv_means_all(raw: str | None) -> bool:
    """Empty COMPONENTS / ``all`` = every enabled catalog id (CLI ``--components all``)."""
    s = (raw or "").strip().lower()
    return s in ("", "all")


def smoke_selected_component_ids(
    components_csv: str,
    catalog: ComponentsSmokeCatalog,
) -> frozenset[str]:
    """COMPONENTS param for a smoke run; empty or ``all`` means every enabled catalog id."""
    if components_csv_means_all(components_csv):
        return frozenset(cid for cid in catalog.component_ids if catalog.components[cid].enabled)
    return parse_components_selection(components_csv.strip(), catalog)


def catalog_disabled_component_ids(catalog: ComponentsSmokeCatalog) -> frozenset[str]:
    return frozenset(cid for cid in catalog.component_ids if not catalog.components[cid].enabled)


def parse_components_selection(raw: str, catalog: ComponentsSmokeCatalog) -> frozenset[str]:
    s = (raw or "").strip()
    if not s:
        return frozenset()
    tokens = [part.strip().lower() for part in s.split(",") if part.strip()]
    if tokens == ["all"]:
        return frozenset(cid for cid in catalog.component_ids if catalog.components[cid].enabled)
    if "all" in tokens:
        raise AppError("COMPONENTS token 'all' cannot be mixed with component ids.", 2)
    seen: set[str] = set()
    for tok in tokens:
        if tok not in catalog.component_ids:
            allowed = ", ".join(catalog.component_ids)
            raise AppError(f"Invalid COMPONENTS token {tok!r}. Allowed: {allowed}.", 2)
        if not catalog.components[tok].enabled:
            raise AppError(f"COMPONENTS token {tok!r} refers to a disabled component.", 2)
        seen.add(tok)
    if not seen:
        raise AppError("COMPONENTS selection is empty or normalizes to zero ids.", 2)
    return frozenset(seen)


def canonical_components_csv(selected: frozenset[str], catalog: ComponentsSmokeCatalog) -> str:
    parts = [c for c in catalog.component_ids if c in selected]
    return ",".join(parts)


def resolve_components_csv(
    raw: str | None,
    *,
    tests_catalog: TestsCatalog,
    tests_selected: frozenset[str],
    components_catalog: ComponentsSmokeCatalog | None = None,
) -> str:
    """Return canonical COMPONENTS when smoke or tier1 is selected; empty string otherwise."""
    if not tests_selected & {"smoke", "tier1"}:
        return ""
    cat = components_catalog if components_catalog is not None else load_components_smoke_catalog(
        default_components_smoke_config_path()
    )
    s = (raw or "").strip()
    if components_csv_means_all(s):
        return cat.enabled_components_csv
    selected = parse_components_selection(s, cat)
    return canonical_components_csv(selected, cat)


def validate_and_normalize_components_csv(
    raw: str | None,
    *,
    tests_csv: str,
    tests_catalog: TestsCatalog | None = None,
    components_catalog: ComponentsSmokeCatalog | None = None,
    components_config_path: Path | None = None,
) -> str:
    _ = tests_catalog
    tests_parts = {p.strip().lower() for p in (tests_csv or "").split(",") if p.strip()}
    if not tests_parts & {"smoke", "tier1"}:
        return ""
    path = components_config_path if components_config_path is not None else default_components_smoke_config_path()
    cat = components_catalog if components_catalog is not None else load_components_smoke_catalog(path)
    s = (raw or "").strip()
    if components_csv_means_all(s):
        return cat.enabled_components_csv
    selected = parse_components_selection(s, cat)
    return canonical_components_csv(selected, cat)
