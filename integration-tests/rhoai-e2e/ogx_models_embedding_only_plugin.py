"""Pytest plugin: let ``ogx_models`` work when OGX registers embedding-only models.

EPHC/EA smoke failures seen on ``4d1f8783``:

    tests/ogx/conftest.py:371 in ogx_models
    tests/ogx/utils.py:419 in select_ogx_model
    ValueError: No LLM models found in OGX client

The OGX server was healthy (``version:1.4.0+rhaiv.0 models:1``) and ``GET /v1/models``
returned 200, but the single registered model had no ``model_type == "llm"`` entry, so
``select_ogx_model`` aborted before the vector-store fixtures could run.

``tests/ogx/vector_io/test_vector_stores.py`` smoke tests (``test_vector_stores_file_upload``,
``test_vector_stores_search``) never read ``ogx_models.model_id``; the ``vector_store``
fixture needs only ``embedding_model.id`` and ``embedding_dimension``. This plugin replaces
the ``ogx_models`` fixture with a variant that keeps upstream selection when an ``llm`` model
is registered and degrades to embedding-only (with a loud, attributable warning) when it is
not. ``model_id`` is left empty so any LLM-dependent test fails at the API call instead of
passing on a fake id.

Patching happens at the fixture layer, not in ``tests.ogx.utils``: upstream
``tests/ogx/models/test_model_selection.py`` asserts that ``select_ogx_model`` raises.
"""

from __future__ import annotations

import os

_PATCH_FLAG = "_ogx_models_embedding_only_patched"
_EMBEDDING_PROVIDER_PRIORITY = ("sentence-transformers", "vllm-embedding")


def _model_metadata(model) -> dict:
    return getattr(model, "custom_metadata", None) or {}


def _llm_models(models: list) -> list:
    return [model for model in models if _model_metadata(model).get("model_type") == "llm"]


def _embedding_provider_id(providers: list, models: list | None = None) -> str:
    """Pick the highest-priority provider that actually serves an embedding model."""
    provider_ids = [getattr(provider, "provider_id", "") for provider in providers]
    for candidate in _EMBEDDING_PROVIDER_PRIORITY:
        if candidate not in provider_ids:
            continue
        if models is not None and _select_embedding(models, candidate)[0] is None:
            continue
        return candidate
    return ""


def _select_embedding(models: list, provider_id: str) -> tuple[object | None, int]:
    for model in models:
        metadata = _model_metadata(model)
        if metadata.get("model_type") != "embedding" or metadata.get("provider_id") != provider_id:
            continue
        try:
            return model, int(metadata["embedding_dimension"])
        except (KeyError, TypeError, ValueError):
            return None, 0
    return None, 0


def _describe_registered_models(models: list, providers: list) -> str:
    entries = []
    for model in models:
        metadata = _model_metadata(model)
        entries.append(
            f"{getattr(model, 'id', '?')}"
            f"(model_type={metadata.get('model_type')!r}, provider_id={metadata.get('provider_id')!r})"
        )
    provider_ids = [getattr(provider, "provider_id", "?") for provider in providers]
    return (
        f"registered models: {entries or '[]'}; providers: {provider_ids}; "
        f"OGX_CORE_INFERENCE_MODEL={os.environ.get('OGX_CORE_INFERENCE_MODEL', '')!r}; "
        f"OGX_CORE_VLLM_URL={os.environ.get('OGX_CORE_VLLM_URL', '')!r}"
    )


def build_ogx_model_info(models: list, providers: list, configured_model: str = ""):
    """Return ModelInfo; fall back to embedding-only when no ``llm`` model is registered."""
    from tests.ogx.constants import ModelInfo
    from tests.ogx.utils import select_ogx_model

    try:
        return select_ogx_model(
            models=models,
            providers=providers,
            configured_model=configured_model,
        )
    except ValueError as exc:
        if "No LLM models found" not in str(exc):
            raise

    provider_id = _embedding_provider_id(providers, models)
    if not provider_id:
        raise ValueError(
            "No embedding provider found in OGX client "
            f"({_describe_registered_models(models, providers)})"
        ) from None
    embedding_model, embedding_dimension = _select_embedding(models, provider_id)
    if embedding_model is None:
        raise ValueError(
            f"No embedding model registered for provider {provider_id!r} "
            f"({_describe_registered_models(models, providers)})"
        ) from None

    print(
        "WARN: ogx_models_embedding_only_plugin: no llm-typed model registered in OGX; "
        "continuing with embedding-only ModelInfo (model_id=''). "
        f"embedding_model={getattr(embedding_model, 'id', '?')!r} "
        f"embedding_dimension={embedding_dimension} provider_id={provider_id!r}. "
        f"{_describe_registered_models(models, providers)}",
        flush=True,
    )
    return ModelInfo(
        model_id="",
        embedding_model=embedding_model,
        embedding_dimension=embedding_dimension,
    )


def apply_ogx_models_embedding_only_patch() -> bool:
    """Replace the ``ogx_models`` fixture with an embedding-only tolerant variant."""
    try:
        import pytest
        import tests.ogx.conftest as ogx_conftest
        from tests.ogx.constants import OGX_CORE_INFERENCE_MODEL
    except ImportError:
        return False
    if getattr(ogx_conftest, _PATCH_FLAG, False):
        return True

    @pytest.fixture(scope="class")
    def ogx_models_embedding_only(ogx_client):
        models = ogx_client.models.list()
        providers = ogx_client.providers.list()
        return build_ogx_model_info(
            models=models.data,
            providers=providers,
            configured_model=OGX_CORE_INFERENCE_MODEL,
        )

    ogx_conftest.ogx_models = ogx_models_embedding_only
    setattr(ogx_conftest, _PATCH_FLAG, True)
    print(
        "✓ ogx_models_embedding_only_plugin: ogx_models tolerates embedding-only OGX servers",
        flush=True,
    )
    return True


def pytest_configure(config) -> None:  # noqa: ARG001
    if apply_ogx_models_embedding_only_patch():
        return
    print(
        "WARN: ogx_models_embedding_only_plugin: patch not applied (import failure)",
        flush=True,
    )
