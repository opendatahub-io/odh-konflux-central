"""Generic catalog test-slice resolution for ``--tests`` extensions."""

from __future__ import annotations

from dataclasses import dataclass

from suite.component_catalog_models import (
    ComponentsSmokeCatalog,
    CypressParallelSet,
    CypressRunnerConfig,
    SmokeComponent,
)
from suite.errors import AppError


@dataclass(frozen=True)
class TestSlice:
    """Named sub-selection within a component phase (catalog-defined)."""

    component_id: str
    phase: str
    slice_id: str


def normalize_test_slice_token(raw: str) -> str:
    """Normalize CLI/Tekton token to canonical slice id (case-insensitive match)."""
    tok = (raw or "").strip()
    if tok.startswith("@"):
        tok = tok[1:]
    if not tok:
        raise AppError("Test slice token is empty.", 2)
    return tok


def _token_key(token: str) -> str:
    return normalize_test_slice_token(token).casefold()


def _cypress_parallel_set_to_slice(
    component_id: str,
    phase: str,
    parallel_set: CypressParallelSet,
) -> TestSlice:
    return TestSlice(
        component_id=component_id,
        phase=phase,
        slice_id=parallel_set.results_subdir,
    )


def _slice_matches_token(slice_id: str, grep_tag: str, token: str) -> bool:
    key = _token_key(token)
    return key in {slice_id.casefold(), grep_tag.lstrip("@").casefold()}


def iter_catalog_test_slices(catalog: ComponentsSmokeCatalog) -> tuple[TestSlice, ...]:
    """All declared test slices across the smoke catalog (extensible per runner type)."""
    out: list[TestSlice] = []
    seen: set[tuple[str, str, str]] = set()
    for component_id, comp in catalog.components.items():
        if comp.runner is None or comp.runner.cypress is None:
            continue
        for phase, sets in comp.runner.cypress.gates.items():
            for parallel_set in sets:
                key = (component_id, phase, parallel_set.results_subdir)
                if key in seen:
                    continue
                seen.add(key)
                out.append(_cypress_parallel_set_to_slice(component_id, phase, parallel_set))
    return tuple(out)


def match_test_slice_token(token: str, catalog: ComponentsSmokeCatalog) -> TestSlice | None:
    for component_id, comp in catalog.components.items():
        if comp.runner is None or comp.runner.cypress is None:
            continue
        for phase, sets in comp.runner.cypress.gates.items():
            for parallel_set in sets:
                if _slice_matches_token(parallel_set.results_subdir, parallel_set.grep_tag, token):
                    return _cypress_parallel_set_to_slice(component_id, phase, parallel_set)
    return None


def known_test_slice_ids(catalog: ComponentsSmokeCatalog) -> tuple[str, ...]:
    seen: set[str] = set()
    out: list[str] = []
    for item in iter_catalog_test_slices(catalog):
        if item.slice_id not in seen:
            seen.add(item.slice_id)
            out.append(item.slice_id)
    return tuple(out)


def slice_ids_for_component(test_slice_ids_csv: str, component_id: str) -> tuple[str, ...]:
    """Slice ids from *test_slice_ids_csv* (runners ignore ids they do not implement)."""
    return tuple(part.strip() for part in (test_slice_ids_csv or "").split(",") if part.strip())


def filter_cypress_config_by_slice_ids(
    config: CypressRunnerConfig,
    phases: tuple[str, ...],
    slice_ids_csv: str,
) -> CypressRunnerConfig:
    """Cypress runner: keep parallel sets matching *slice_ids_csv*."""
    raw_tokens = [part.strip() for part in (slice_ids_csv or "").split(",") if part.strip()]
    if not raw_tokens:
        return config
    token_keys = frozenset(_token_key(tok) for tok in raw_tokens)
    new_gates: dict[str, tuple[CypressParallelSet, ...]] = {}
    for phase in phases:
        sets = config.gates.get(phase, ())
        filtered = tuple(
            s
            for s in sets
            if s.results_subdir.casefold() in token_keys
            or s.grep_tag.lstrip("@").casefold() in token_keys
        )
        if filtered:
            new_gates[phase] = filtered
    if not new_gates:
        allowed = ", ".join(
            sorted(
                {
                    s.results_subdir
                    for sets in config.gates.values()
                    for s in sets
                }
            )
        )
        raise AppError(
            f"TEST_TAGS {slice_ids_csv!r} matched no Cypress sets for phases {list(phases)!r}. "
            f"Known sets: {allowed or '(none)'}.",
            2,
        )
    return CypressRunnerConfig(
        skip_tags=config.skip_tags,
        gates=new_gates,
        test_timeout_seconds=config.test_timeout_seconds,
        parallel_stagger_sec=config.parallel_stagger_sec,
        max_parallel=config.max_parallel,
        display_base=config.display_base,
        run_config=config.run_config,
    )


@dataclass(frozen=True)
class TestsSelectionWithSlices:
    phases: frozenset[str]
    test_tags: tuple[str, ...]
    scoped_component_ids: tuple[str, ...]
    auto_scope_components: bool


def parse_tests_selection_with_slice_extensions(
    raw: str,
    *,
    phase_ids: frozenset[str],
    required_phase_ids: frozenset[str],
    components_catalog: ComponentsSmokeCatalog,
) -> TestsSelectionWithSlices:
    """Split ``--tests`` into pipeline phases and optional catalog test-slice filters."""
    s = (raw or "").strip()
    if not s:
        raise AppError("TESTS selection is empty.", 2)

    phases: set[str] = set()
    slice_ids: list[str] = []
    scoped_components: set[str] = set()
    unknown: list[str] = []

    for part in s.split(","):
        tok = part.strip()
        if not tok:
            continue
        phase_key = tok.lower()
        if phase_key in phase_ids:
            phases.add(phase_key)
            continue
        match = match_test_slice_token(tok, components_catalog)
        if match is None:
            unknown.append(tok)
            continue
        phases.add(match.phase)
        scoped_components.add(match.component_id)
        if match.slice_id not in slice_ids:
            slice_ids.append(match.slice_id)

    if unknown:
        allowed_phases = ", ".join(sorted(phase_ids))
        allowed_slices = ", ".join(known_test_slice_ids(components_catalog))
        raise AppError(
            f"Invalid TESTS token(s): {', '.join(unknown)!r}. "
            f"Allowed phases: {allowed_phases}. "
            f"Catalog test slices: {allowed_slices}.",
            2,
        )
    if not phases:
        raise AppError("TESTS selection is empty or normalizes to zero phases.", 2)

    missing = required_phase_ids - phases
    if missing:
        need = ", ".join(sorted(missing))
        raise AppError(f"TESTS must include required phase(s): {need}.", 2)

    scoped = tuple(sorted(scoped_components))
    return TestsSelectionWithSlices(
        phases=frozenset(phases),
        test_tags=tuple(slice_ids),
        scoped_component_ids=scoped,
        auto_scope_components=bool(slice_ids),
    )
