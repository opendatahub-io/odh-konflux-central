"""Tekton RUN_SMOKE_<component_id> result names for per-component task when: clauses.

``parse-pipeline-tests`` writes initial selection; ``opendatahub-tests-prepare``
refreshes flags after ``export_component_plan`` version gates.
"""

from __future__ import annotations


def component_smoke_result_name(component_id: str) -> str:
    """Return parse-pipeline-tests result name for one catalog component id."""
    return f"RUN_SMOKE_{component_id}"


def ordered_component_ids(
    component_ids: tuple[str, ...],
    *,
    run_order: dict[str, str],
) -> tuple[str, ...]:
    """Catalog order with ``runOrder: last`` components appended after all others."""
    normal = [cid for cid in component_ids if run_order.get(cid) != "last"]
    last = [cid for cid in component_ids if run_order.get(cid) == "last"]
    return tuple(normal + last)
