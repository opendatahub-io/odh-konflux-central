"""Load rhoai-e2e-dsc-install.yaml and resolve DSC keys for install vs component prep."""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any

from suite.component_version_gate import (
    _compare_version_strings,
    normalize_version_for_enablement,
)
from suite.errors import AppError
from suite.tests_config import _load_yaml_document


@dataclass(frozen=True)
class SmokeDscMapping:
    keys: tuple[str, ...]
    pre35_extra_keys: tuple[str, ...] = ()


@dataclass(frozen=True)
class DscInstallVersionBand:
    min_rhoai: str | None
    max_rhoai: str | None
    install_removed: frozenset[str]


@dataclass(frozen=True)
class DscInstallPolicyDocument:
    smoke_components: dict[str, SmokeDscMapping]
    version_bands: tuple[DscInstallVersionBand, ...]


def default_dsc_install_policy_path() -> Path:
    base = Path(__file__).resolve().parent.parent
    config = base / "config"
    json_path = config / "rhoai-e2e-dsc-install.json"
    if json_path.is_file():
        return json_path
    return config / "rhoai-e2e-dsc-install.yaml"


def _load_policy_document(path: Path) -> dict[str, Any]:
    if not path.is_file():
        raise AppError(f"DSC install policy not found: {path}", 2)
    if path.suffix.lower() == ".json":
        try:
            loaded = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise AppError(f"Cannot read DSC install policy {path}: {exc}", 2) from exc
        if isinstance(loaded, dict):
            return loaded
        raise AppError(f"DSC install policy root must be a mapping: {path}", 2)
    return _load_yaml_document(path)


def _parse_string_list(
    raw: Any,
    *,
    path: Path,
    label: str,
    required: bool = False,
) -> tuple[str, ...]:
    if raw is None:
        if required:
            raise AppError(f"{label} is required in {path}", 2)
        return ()
    if not isinstance(raw, list) or not raw:
        raise AppError(f"{label} must be a non-empty list in {path}", 2)
    out: list[str] = []
    for i, item in enumerate(raw):
        if not isinstance(item, str) or not item.strip():
            raise AppError(f"{label}[{i}] must be a non-empty string in {path}", 2)
        out.append(item.strip())
    return tuple(out)


def _parse_smoke_components(raw: Any, path: Path) -> dict[str, SmokeDscMapping]:
    if not isinstance(raw, dict):
        raise AppError(f"smokeComponents must be a mapping in {path}", 2)
    out: dict[str, SmokeDscMapping] = {}
    for smoke_id, entry in raw.items():
        if not isinstance(smoke_id, str) or not smoke_id.strip():
            raise AppError(f"smokeComponents keys must be non-empty strings in {path}", 2)
        if not isinstance(entry, dict):
            raise AppError(f"smokeComponents.{smoke_id} must be a mapping in {path}", 2)
        keys = _parse_string_list(
            entry.get("keys"),
            path=path,
            label=f"smokeComponents.{smoke_id}.keys",
            required=True,
        )
        pre35 = _parse_string_list(
            entry.get("pre35ExtraKeys"),
            path=path,
            label=f"smokeComponents.{smoke_id}.pre35ExtraKeys",
        )
        out[smoke_id.strip()] = SmokeDscMapping(keys=keys, pre35_extra_keys=pre35)
    return out


def _parse_version_bands(raw: Any, path: Path) -> tuple[DscInstallVersionBand, ...]:
    install_policy = raw if isinstance(raw, dict) else {}
    bands_raw = install_policy.get("versionBands")
    if bands_raw is None:
        return ()
    if not isinstance(bands_raw, list):
        raise AppError(f"installPolicy.versionBands must be a list in {path}", 2)
    bands: list[DscInstallVersionBand] = []
    for i, band in enumerate(bands_raw):
        if not isinstance(band, dict):
            raise AppError(f"installPolicy.versionBands[{i}] must be a mapping in {path}", 2)
        min_rhoai = str(band.get("minRhoai", "")).strip() or None
        max_rhoai = str(band.get("maxRhoai", "")).strip() or None
        removed = frozenset(
            _parse_string_list(
                band.get("installRemoved"),
                path=path,
                label=f"installPolicy.versionBands[{i}].installRemoved",
            )
        )
        bands.append(
            DscInstallVersionBand(
                min_rhoai=min_rhoai,
                max_rhoai=max_rhoai,
                install_removed=removed,
            )
        )
    return tuple(bands)


def load_dsc_install_policy(path: Path | None = None) -> DscInstallPolicyDocument:
    policy_path = path or Path(os.environ.get("DSC_INSTALL_POLICY_PATH", "").strip() or default_dsc_install_policy_path())
    doc = _load_policy_document(policy_path)
    ver = doc.get("schemaVersion")
    if ver != 1:
        raise AppError(f"Unsupported DSC install policy schemaVersion {ver!r} in {policy_path} (expected 1).", 2)
    return DscInstallPolicyDocument(
        smoke_components=_parse_smoke_components(doc.get("smokeComponents"), policy_path),
        version_bands=_parse_version_bands(doc.get("installPolicy"), policy_path),
    )


@lru_cache(maxsize=4)
def _cached_policy(path_str: str) -> DscInstallPolicyDocument:
    return load_dsc_install_policy(Path(path_str))


def _active_policy() -> DscInstallPolicyDocument:
    path = os.environ.get("DSC_INSTALL_POLICY_PATH", "").strip() or str(default_dsc_install_policy_path())
    return _cached_policy(path)


def _version_band_matches(compare_ver: str, band: DscInstallVersionBand) -> bool:
    if band.min_rhoai and _compare_version_strings(compare_ver, band.min_rhoai) < 0:
        return False
    if band.max_rhoai and _compare_version_strings(compare_ver, band.max_rhoai) > 0:
        return False
    return True


def _install_removed_for_version(operator_version: str, policy: DscInstallPolicyDocument) -> frozenset[str]:
    compare_ver, is_numeric = normalize_version_for_enablement(operator_version)
    if not is_numeric:
        return frozenset()
    removed: set[str] = set()
    for band in policy.version_bands:
        if _version_band_matches(compare_ver, band):
            removed.update(band.install_removed)
    return frozenset(removed)


def _version_gated_smoke_ids(operator_version: str) -> frozenset[str]:
    """Catalog smoke ids that should not patch DSC for the installed RHOAI version."""
    from suite.component_catalog import default_components_smoke_config_path, load_components_smoke_catalog
    from suite.component_version_gate import component_enabled_for_version

    ver = (operator_version or "").strip()
    if not ver:
        return frozenset()
    product = os.environ.get("PRODUCT", "").strip().lower()
    catalog = load_components_smoke_catalog(default_components_smoke_config_path())
    gated: set[str] = set()
    for comp in catalog.components.values():
        if not component_enabled_for_version(comp, ver, product=product).enabled:
            gated.add(comp.id)
    return frozenset(gated)


def resolve_managed_dsc_keys(
    components_csv: str,
    operator_version: str = "",
    *,
    for_install: bool = False,
) -> set[str]:
    """Return DSC component keys that should be Managed for the given smoke selection."""
    policy = _active_policy()
    ids = {c.strip() for c in components_csv.split(",") if c.strip()}
    compare_ver, is_numeric = normalize_version_for_enablement(operator_version)
    pre35 = is_numeric and _compare_version_strings(compare_ver, "3.5") < 0

    managed: set[str] = set()
    if for_install:
        # Install Tekton image may lack PyYAML/yq; versionBands.installRemoved covers 3.5+ deferrals.
        gated: frozenset[str] = frozenset()
    else:
        gated = _version_gated_smoke_ids(operator_version) if operator_version.strip() else frozenset()
    for smoke_id in ids:
        if smoke_id in gated:
            continue
        mapping = policy.smoke_components.get(smoke_id)
        if mapping is None:
            continue
        managed.update(mapping.keys)
        if pre35:
            managed.update(mapping.pre35_extra_keys)

    if for_install and operator_version.strip():
        managed -= _install_removed_for_version(operator_version, policy)
    if "ogx" in ids and "llama_stack" not in ids:
        managed.discard("llamastackoperator")
    return managed


def stale_removed_dsc_keys_for_smoke(components_csv: str, operator_version: str = "") -> frozenset[str]:
    """DSC keys that should be Removed for this smoke run but may still be Managed on-cluster."""
    policy = _active_policy()
    managed = resolve_managed_dsc_keys(components_csv, operator_version, for_install=False)
    stale = set(_install_removed_for_version(operator_version, policy)) - managed
    stale.add("llamastackoperator")
    if "ogx" not in managed:
        stale.add("ogx")
    return frozenset(stale)
