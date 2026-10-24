# -*- coding: utf-8 -*-
from __future__ import annotations

import pytest

from lantai.providers.ollama_provider import OllamaProvider


def _make_provider(base_url: str = "http://localhost:11434") -> OllamaProvider:
    return OllamaProvider(
        id="ollama",
        name="Ollama",
        base_url=base_url,
        api_key="EMPTY",
        chat_model="OpenAIChatModel",
    )


@pytest.mark.parametrize(
    ("base_url", "expected_base_url"),
    [
        ("http://localhost:11434", "http://localhost:11434"),
        ("http://localhost:11434/", "http://localhost:11434"),
        ("http://localhost:11434/v1", "http://localhost:11434"),
        ("http://localhost:11434/v1/", "http://localhost:11434"),
    ],
)
def test_base_url_is_normalized_on_init(
    base_url: str,
    expected_base_url: str,
) -> None:
    provider = _make_provider(base_url=base_url)

    assert provider.base_url == expected_base_url


@pytest.mark.parametrize(
    "env_base_url",
    [
        "http://env-ollama.local:11434",
        "http://env-ollama.local:11434/",
        "http://env-ollama.local:11434/v1",
        "http://env-ollama.local:11434/v1/",
    ],
)
async def test_auto_load_from_env_normalizes_base_url(
    monkeypatch,
    env_base_url: str,
) -> None:
    monkeypatch.setenv("OLLAMA_HOST", env_base_url)

    provider = OllamaProvider(
        id="ollama",
        name="Ollama",
        chat_model="OpenAIChatModel",
    )

    assert provider.base_url == "http://env-ollama.local:11434"


def test_update_config_normalizes_base_url() -> None:
    provider = _make_provider()

    provider.update_config(
        {
            "base_url": "http://updated-ollama.local:11434/v1/",
        },
    )

    assert provider.base_url == "http://updated-ollama.local:11434"


@pytest.mark.parametrize(
    "base_url",
    [
        "http://localhost:11434",
        "http://localhost:11434/",
        "http://localhost:11434/v1",
        "http://localhost:11434/v1/",
    ],
)
def test_client_uses_single_v1_suffix(monkeypatch, base_url: str) -> None:
    captured: dict[str, object] = {}

    class FakeAsyncOpenAI:
        def __init__(self, *, base_url, api_key, timeout) -> None:
            captured["base_url"] = base_url
            captured["api_key"] = api_key
            captured["timeout"] = timeout

    monkeypatch.setattr(
        "lantai.providers.ollama_provider.AsyncOpenAI",
        FakeAsyncOpenAI,
    )

    provider = _make_provider(base_url=base_url)
    getattr(provider, "_client")(timeout=7)

    assert captured == {
        "base_url": "http://localhost:11434/v1",
        "api_key": "EMPTY",
        "timeout": 7,
    }


@pytest.mark.parametrize(
    "base_url",
    [
        "http://localhost:11434",
        "http://localhost:11434/",
        "http://localhost:11434/v1",
        "http://localhost:11434/v1/",
    ],
)
def test_get_chat_model_instance_uses_single_v1_suffix(
    monkeypatch,
    base_url: str,
) -> None:
    captured: dict[str, object] = {}

    class FakeOpenAIChatModelCompat:
        def __init__(self, **kwargs) -> None:
            captured.update(kwargs)

    monkeypatch.setattr(
        "lantai.providers.openai_chat_model_compat.OpenAIChatModelCompat",
        FakeOpenAIChatModelCompat,
    )

    provider = _make_provider(base_url=base_url)
    provider.get_chat_model_instance("llama3.1")

    assert captured["model_name"] == "llama3.1"
    assert captured["api_key"] == "EMPTY"
    assert captured["stream"] is True
    assert captured["stream_tool_parsing"] is False
    assert captured["normalize_content_to_text"] is True
    assert captured["client_kwargs"] == {
        "base_url": "http://localhost:11434/v1",
    }
    assert captured["generate_kwargs"] == {}


async def test_native_discovery_keeps_agent_capabilities_and_runtime_context(
    monkeypatch,
) -> None:
    provider = _make_provider()
    calls: list[tuple[str, str]] = []

    async def fake_request_json(
        self,
        method: str,
        path: str,
        *,
        timeout: float,
        json_body=None,
    ):
        del self, timeout
        calls.append((method, path))
        if path == "/api/tags":
            return {
                "models": [
                    {
                        "name": "qwen3:8b",
                        "model": "qwen3:8b",
                        "size": 5_000_000_000,
                        "details": {
                            "parameter_size": "8B",
                            "quantization_level": "Q4_K_M",
                        },
                    },
                ],
            }
        if path == "/api/ps":
            return {
                "models": [
                    {
                        "name": "qwen3:8b",
                        "model": "qwen3:8b",
                        "context_length": 65_536,
                    },
                ],
            }
        assert path == "/api/show"
        assert json_body == {"model": "qwen3:8b"}
        return {
            "capabilities": ["completion", "tools", "vision"],
            "model_info": {"qwen3.context_length": 131_072},
        }

    monkeypatch.setattr(OllamaProvider, "_request_json", fake_request_json)

    models = await provider.fetch_models(timeout=3)

    assert calls == [
        ("GET", "/api/tags"),
        ("GET", "/api/ps"),
        ("POST", "/api/show"),
    ]
    assert len(models) == 1
    model = models[0]
    assert model.id == "qwen3:8b"
    assert model.supports_tool_use is True
    assert model.supports_image is True
    assert model.supports_multimodal is True
    assert model.context_window == 65_536
    assert model.max_context_window == 131_072
    assert model.runtime_state == "loaded"
    assert model.parameter_size == "8B"
    assert model.quantization == "Q4_K_M"
    assert model.size_bytes == 5_000_000_000
    assert model.capability_source == "runtime:ollama-native"


async def test_native_discovery_keeps_partial_model_when_show_fails(
    monkeypatch,
) -> None:
    provider = _make_provider()

    async def fake_request_json(
        self,
        method: str,
        path: str,
        *,
        timeout: float,
        json_body=None,
    ):
        del self, method, timeout, json_body
        if path == "/api/tags":
            return {
                "models": [
                    {
                        "name": "custom:latest",
                        "details": {"parameter_size": "7B"},
                    },
                ],
            }
        if path == "/api/ps":
            raise ValueError("ps unavailable")
        raise ValueError("show unavailable")

    monkeypatch.setattr(OllamaProvider, "_request_json", fake_request_json)

    models = await provider.fetch_models()

    assert [model.id for model in models] == ["custom:latest"]
    assert models[0].supports_tool_use is None
    assert models[0].runtime_state == "available"
    assert models[0].parameter_size == "7B"


async def test_connection_failure_explains_how_to_start_ollama(monkeypatch) -> None:
    provider = _make_provider()

    async def fail_request(*_args, **_kwargs):
        raise OSError("connection refused")

    monkeypatch.setattr(OllamaProvider, "_request_json", fail_request)

    ok, detail = await provider.check_connection()

    assert ok is False
    assert "http://localhost:11434" in detail
    assert "ollama serve" in detail
