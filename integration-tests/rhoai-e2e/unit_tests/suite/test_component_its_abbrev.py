"""ITS abbrev map matches smoke catalog ids."""

from __future__ import annotations

from pathlib import Path

import yaml

from suite.component_catalog import default_components_smoke_config_path, load_components_smoke_catalog

_ROOT = Path(__file__).resolve().parents[2]
_ABBREV_PATH = _ROOT / "config" / "rhoai-e2e-component-its-abbrev.yaml"


def test_abbrev_covers_all_catalog_ids() -> None:
    catalog = load_components_smoke_catalog(default_components_smoke_config_path())
    doc = yaml.safe_load(_ABBREV_PATH.read_text(encoding="utf-8"))
    abbrev = doc.get("abbrev") or {}
    assert isinstance(abbrev, dict)
    missing = [cid for cid in catalog.enabled_component_ids if cid not in abbrev]
    assert not missing, f"missing abbrev for: {missing}"
    values = list(abbrev.values())
    assert len(values) == len(set(values)), "duplicate abbrev tokens"
    for token in abbrev.values():
        t = str(token)
        assert t and t.isalnum(), f"abbrev must be one dash-free token: {t!r}"
        assert len(t) <= 4, f"abbrev must be at most 4 chars: {t!r}"
