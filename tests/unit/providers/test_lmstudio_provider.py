# -*- coding: utf-8 -*-
from __future__ import annotations

import pytest

from lantai.providers.lmstudio_provider import LMStudioProvider


def _make_provider(
    base_url: str = "http://localhost:1234/v1",
) -> LMStudioProvider:
    return LMStudioProvider(
        id="lmstudio",
        name="LM Studio",
        base_url=base_url,
        api_key="",
        chat_model="OpenAIChatModel",
    )


@pytest.mark.parametrize(
    ("base_url", "expected_base_url"),
    [
        ("http://localhost:1234", "http://localhost:1234/v1"),
        ("http://localhost:1234/", "http://localhost:1234/v1"),
        ("http://localhost:1234/v1", "http://localhost:1234/v1"),
        ("http://localhost:1234/v1/", "http://localhost:1234/v1"),
    ],
)
def test_base_url_accepts_root_or_openai_v1(
    base_url: str,
    expected_base_url: str,
) -> None:
    provider = _make_provider(base_url)

    assert provider.base_url == expected_base_url


async def test_native_v1_discovery_filters_embeddings_and_reports_readiness(
    monkeypatch,
) -> None:
    provider = _make_provider()

    async def fake_request_json(
        self,
        path: str,
        *,
        timeout: float,
    ):
        del self, timeout
        assert path == "/api/v1/models"
        return {
            "models": [
                {
                    "type": "llm",
                    "key": "qwen/qwen3-coder-30b",
                    "display_name": "Qwen3 Coder 30B",
                    "quantization": {"name": "Q4_K_M"},
                    "size_bytes": 18_000_000_000,
                    "params_string": "30B",
                    "loaded_instances": [
                        {
                            "id": "qwen3-coder",
                            "config": {"context_length": 65_536},
                        },
                    ],
                    "max_context_length": 262_144,
                    "capabilities": {
                        "vision": False,
                        "trained_for_tool_use": True,
                    },
                },
                {
                    "type": "embedding",
                    "key": "nomic-embed-text",
                    "display_name": "Nomic Embed Text",
                    "loaded_instances": [],
                    "max_context_length": 2_048,
                },
            ],
        }

    monkeypatch.setattr(LMStudioProvider, "_request_native_json", fake_request_json)

    models = await provider.fetch_models()

    assert len(models) == 1
    model = models[0]
    assert model.id == "qwen/qwen3-coder-30b"
    assert model.name == "Qwen3 Coder 30B"
    assert model.supports_tool_use is True
    assert model.supports_image is False
    assert model.supports_multimodal is False
    assert model.context_window == 65_536
    assert model.max_context_window == 262_144
    assert model.runtime_state == "loaded"
    assert model.parameter_size == "30B"
    assert model.quantization == "Q4_K_M"
    assert model.size_bytes == 18_000_000_000
    assert model.capability_source == "runtime:lmstudio-native-v1"


async def test_connection_without_models_is_reachable_but_actionable(
    monkeypatch,
) -> None:
    provider = _make_provider()

    async def fake_request_json(self, path: str, *, timeout: float):
        del self, timeout
        assert path == "/api/v1/models"
        return {"models": []}

    monkeypatch.setattr(LMStudioProvider, "_request_native_json", fake_request_json)

    ok, detail = await provider.check_connection()

    assert ok is True
    assert "no local models" in detail.lower()


async def test_connection_failure_explains_how_to_start_lm_studio(
    monkeypatch,
) -> None:
    provider = _make_provider()

    async def fail_request(*_args, **_kwargs):
        raise OSError("connection refused")

    monkeypatch.setattr(LMStudioProvider, "_request_native_json", fail_request)

    ok, detail = await provider.check_connection()

    assert ok is False
    assert "http://localhost:1234" in detail
    assert "Developer" in detail
