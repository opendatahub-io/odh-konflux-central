"""Validate and normalize the olminstall pipeline ``TESTS`` comma list (CLI + Tekton)."""

from __future__ import annotations

from .constants import default_tests_config_path
from .errors import AppError
from .tests_config import TestsCatalog, load_tests_catalog


def parse_tests_selection(raw: str, catalog: TestsCatalog) -> frozenset[str]:
    """Split and normalize TESTS string; validate tokens and required phases (no canonical order yet)."""
    s = (raw or "").strip()
    if not s:
        raise AppError("TESTS selection is empty.", 2)
    seen: set[str] = set()
    for part in s.split(","):
        tok = part.strip().lower()
        if not tok:
            continue
        if tok not in catalog.phase_ids:
            allowed = ", ".join(catalog.phase_ids)
            raise AppError(f"Invalid TESTS token {tok!r}. Allowed: {allowed}.", 2)
        seen.add(tok)
    if not seen:
        raise AppError("TESTS selection is empty or normalizes to zero phases.", 2)
    missing = catalog.required_ids - seen
    if missing:
        need = ", ".join(sorted(missing))
        raise AppError(
            f"TESTS must include required phase(s): {need}. "
            f"Example default from config: {catalog.default_csv}.",
            2,
        )
    return frozenset(seen)


def canonical_tests_csv(selected: frozenset[str], catalog: TestsCatalog) -> str:
    """Stable ordering: follow phase order from olminstall-tests-config.yaml."""
    parts = [p for p in catalog.phase_ids if p in selected]
    return ",".join(parts)


def validate_and_normalize_tests_csv(raw: str | None, catalog: TestsCatalog | None = None) -> str:
    """
    Return a canonical comma-separated TESTS string for ITS / pipeline param.

    If ``raw`` is None or empty, use ``catalog.default_csv``.
    """
    cat = catalog if catalog is not None else load_tests_catalog(default_tests_config_path())
    s = (raw or "").strip()
    if not s:
        return cat.default_csv
    selected = parse_tests_selection(s, cat)
    return canonical_tests_csv(selected, cat)
