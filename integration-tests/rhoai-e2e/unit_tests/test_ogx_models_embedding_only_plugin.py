"""Tests for ogx_models_embedding_only_plugin."""

from __future__ import annotations

import sys
import types
from typing import Any, NamedTuple

import pytest


class _FakeEmbeddingModel(NamedTuple):
    id: str


class _FakeModel:
    def __init__(self, model_id: str, metadata: dict[str, Any]) -> None:
        self.id = model_id
        self.custom_metadata = metadata


class _FakeProvider:
    def __init__(self, provider_id: str) -> None:
        self.provider_id = provider_id


def _install_ogx_modules(monkeypatch: pytest.MonkeyPatch, select_ogx_model) -> None:
    constants = types.ModuleType("tests.ogx.constants")
    constants.OGX_CORE_INFERENCE_MODEL = "Qwen3.8-27B"

    class ModelInfo(NamedTuple):
        model_id: str
        embedding_model: Any
        embedding_dimension: int

    constants.ModelInfo = ModelInfo

    utils = types.ModuleType("tests.ogx.utils")
    utils.select_ogx_model = select_ogx_model

    ogx_pkg = types.ModuleType("tests.ogx")
    tests_pkg = types.ModuleType("tests")
    tests_pkg.ogx = ogx_pkg
    ogx_pkg.constants = constants
    ogx_pkg.utils = utils

    monkeypatch.setitem(sys.modules, "tests", tests_pkg)
    monkeypatch.setitem(sys.modules, "tests.ogx", ogx_pkg)
    monkeypatch.setitem(sys.modules, "tests.ogx.constants", constants)
    monkeypatch.setitem(sys.modules, "tests.ogx.utils", utils)


def test_delegates_to_upstream_when_llm_registered(monkeypatch: pytest.MonkeyPatch) -> None:
    sentinel = object()

    def _select_ogx_model(**kwargs):
        return sentinel

    _install_ogx_modules(monkeypatch, _select_ogx_model)

    import ogx_models_embedding_only_plugin as plugin

    result = plugin.build_ogx_model_info(
        models=[_FakeModel("Qwen3.8-27B", {"model_type": "llm"})],
        providers=[_FakeProvider("vllm-embedding")],
        configured_model="Qwen3.8-27B",
    )
    assert result is sentinel


def test_embedding_only_returns_model_info(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    def _select_ogx_model(**_kwargs):
        raise ValueError("No LLM models found in OGX client")

    _install_ogx_modules(monkeypatch, _select_ogx_model)
    monkeypatch.setenv("OGX_CORE_VLLM_URL", "https://litemaas.example/v1")
    monkeypatch.setenv("OGX_CORE_INFERENCE_MODEL", "Qwen3.8-27B")

    import ogx_models_embedding_only_plugin as plugin

    embedding_model = _FakeEmbeddingModel(id="nomic-embed-text-v2-moe")
    info = plugin.build_ogx_model_info(
        models=[
            _FakeModel(
                "nomic-embed-text-v2-moe",
                {
                    "model_type": "embedding",
                    "provider_id": "vllm-embedding",
                    "embedding_dimension": 768,
                },
            )
        ],
        providers=[_FakeProvider("vllm-embedding")],
        configured_model="Qwen3.8-27B",
    )

    assert info.model_id == ""
    assert info.embedding_model.id == embedding_model.id
    assert info.embedding_dimension == 768
    captured = capsys.readouterr().out
    assert "no llm-typed model registered" in captured
    assert "nomic-embed-text-v2-moe" in captured
    assert "https://litemaas.example/v1" in captured


def test_embedding_only_raises_without_embedding_provider(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def _select_ogx_model(**_kwargs):
        raise ValueError("No LLM models found in OGX client")

    _install_ogx_modules(monkeypatch, _select_ogx_model)

    import ogx_models_embedding_only_plugin as plugin

    with pytest.raises(ValueError, match="No embedding provider found in OGX client"):
        plugin.build_ogx_model_info(
            models=[_FakeModel("some-model", {"model_type": "embedding"})],
            providers=[_FakeProvider("other-provider")],
        )


def test_other_value_errors_are_not_swallowed(monkeypatch: pytest.MonkeyPatch) -> None:
    def _select_ogx_model(**_kwargs):
        raise ValueError("No embedding provider found")

    _install_ogx_modules(monkeypatch, _select_ogx_model)

    import ogx_models_embedding_only_plugin as plugin

    with pytest.raises(ValueError, match="No embedding provider found"):
        plugin.build_ogx_model_info(models=[], providers=[])
