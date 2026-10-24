# -*- coding: utf-8 -*-
from __future__ import annotations

import base64
from pathlib import Path
from types import SimpleNamespace

import lantai.providers.openai_provider as openai_provider_module
from lantai.providers.openai_provider import OpenAIProvider
from lantai.providers.provider import ModelInfo


def _make_provider(is_custom: bool = False) -> OpenAIProvider:
    return OpenAIProvider(
        id="openai",
        name="OpenAI",
        base_url="https://mock-openai.local/v1",
        api_key="sk-test",
        is_custom=is_custom,
        chat_model="OpenAIChatModel",
    )


async def test_generate_image_uses_images_api_and_normalizes_results(
    monkeypatch,
) -> None:
    provider = _make_provider()
    provider.models = [
        ModelInfo(
            id="gpt-image-2",
            name="GPT Image 2",
            generate_kwargs={
                "image_generation": {
                    "background": "transparent",
                    "model": "must-not-override",
                    "prompt": "must-not-override",
                },
            },
        ),
    ]
    captured: list[dict] = []
    encoded = base64.b64encode(b"fake-image").decode("ascii")

    class FakeImages:
        async def generate(self, **kwargs):
            captured.append(kwargs)
            return SimpleNamespace(
                created=123,
                data=[
                    SimpleNamespace(
                        url="https://images.example/generated.png",
                        b64_json="",
                        revised_prompt="revised",
                    ),
                    SimpleNamespace(
                        url="",
                        b64_json=encoded,
                        revised_prompt="",
                    ),
                ],
                model_dump=lambda: {"id": "image-response"},
            )

    fake_client = SimpleNamespace(images=FakeImages())
    monkeypatch.setattr(provider, "_client", lambda timeout=5: fake_client)

    result = await provider.generate_image(
        model_id="gpt-image-2",
        prompt=" draw a quiet lake ",
        n=2,
        size="1536x1024",
        quality="high",
        timeout=45,
    )

    assert captured == [
        {
            "background": "transparent",
            "model": "gpt-image-2",
            "prompt": "draw a quiet lake",
            "n": 2,
            "size": "1536x1024",
            "quality": "high",
            "timeout": 45,
        },
    ]
    assert result["created"] == 123
    assert result["data"][0]["url"].endswith("generated.png")
    assert result["data"][0]["revised_prompt"] == "revised"
    assert result["data"][1]["b64_json"] == encoded
    assert len(result["assets"]) == 2


async def test_generate_image_retries_minimum_pixel_error_with_upscaled_size(
    monkeypatch,
) -> None:
    provider = _make_provider()
    captured: list[dict] = []

    class FakeAPIError(Exception):
        status_code = 400
        code = "InvalidParameter"

    class FakeImages:
        async def generate(self, **kwargs):
            captured.append(kwargs)
            if len(captured) == 1:
                raise FakeAPIError(
                    "The parameter `size` is invalid: image size must be at "
                    "least 3686400 pixels",
                )
            return SimpleNamespace(
                created=789,
                data=[
                    SimpleNamespace(
                        url="https://images.example/retried.png",
                        b64_json="",
                        revised_prompt="",
                    ),
                ],
                model_dump=lambda: {"id": "retried-image-response"},
            )

    fake_client = SimpleNamespace(images=FakeImages())
    monkeypatch.setattr(provider, "_client", lambda timeout=5: fake_client)
    monkeypatch.setattr(openai_provider_module, "APIError", FakeAPIError)

    result = await provider.generate_image(
        model_id="seedream-5.0-lite",
        prompt="draw a cinematic landscape",
        size="1920x1080",
        timeout=45,
    )

    assert [call["size"] for call in captured] == [
        "1920x1080",
        "2560x1440",
    ]
    assert result["requested_size"] == "1920x1080"
    assert result["effective_size"] == "2560x1440"
    assert result["data"][0]["url"].endswith("retried.png")


async def test_edit_image_uses_local_references_and_normalizes_results(
    monkeypatch,
    tmp_path,
) -> None:
    provider = _make_provider()
    reference_a = tmp_path / "reference-a.png"
    reference_b = tmp_path / "reference-b.jpg"
    reference_a.write_bytes(b"png")
    reference_b.write_bytes(b"jpg")
    captured: list[dict] = []

    class FakeImages:
        async def edit(self, **kwargs):
            images = kwargs.pop("image")
            captured.append(
                {
                    **kwargs,
                    "image_names": [Path(image.name).name for image in images],
                    "image_bytes": [image.read() for image in images],
                }
            )
            return SimpleNamespace(
                created=456,
                data=[
                    SimpleNamespace(
                        url="https://images.example/edited.png",
                        b64_json="",
                        revised_prompt="edited",
                    )
                ],
                model_dump=lambda: {"id": "image-edit-response"},
            )

    fake_client = SimpleNamespace(images=FakeImages())
    monkeypatch.setattr(provider, "_client", lambda timeout=5: fake_client)

    result = await provider.edit_image(
        model_id="gpt-image-2",
        prompt="保留构图，改成水彩风格",
        image_paths=[str(reference_a), str(reference_b)],
        n=1,
        size="1024x1024",
        quality="high",
        timeout=50,
    )

    assert captured == [
        {
            "model": "gpt-image-2",
            "prompt": "保留构图，改成水彩风格",
            "n": 1,
            "size": "1024x1024",
            "quality": "high",
            "timeout": 50,
            "image_names": ["reference-a.png", "reference-b.jpg"],
            "image_bytes": [b"png", b"jpg"],
        }
    ]
    assert result["data"][0]["url"].endswith("edited.png")


async def test_check_connection_success(monkeypatch) -> None:
    provider = _make_provider()
    calls: list[float | None] = []

    class FakeModels:
        async def list(self, timeout=None):
            calls.append(timeout)
            return SimpleNamespace(data=[])

    fake_client = SimpleNamespace(models=FakeModels())
    monkeypatch.setattr(provider, "_client", lambda timeout=5: fake_client)

    ok, msg = await provider.check_connection(timeout=2.5)

    assert ok is True
    assert msg == ""
    assert calls == [2.5]


async def test_check_connection_api_error_returns_false(monkeypatch) -> None:
    provider = _make_provider()

    class FakeModels:
        async def list(self, timeout=None):
            raise RuntimeError("boom")

    fake_client = SimpleNamespace(models=FakeModels())
    monkeypatch.setattr(provider, "_client", lambda timeout=5: fake_client)
    monkeypatch.setattr(openai_provider_module, "APIError", Exception)

    ok, msg = await provider.check_connection(timeout=1)

    assert ok is False
    assert msg == f"API error when connecting to `{provider.base_url}`: boom"


async def test_list_model_normalizes_and_deduplicates(monkeypatch) -> None:
    provider = _make_provider()
    rows = [
        SimpleNamespace(id="gpt-4o-mini", name="GPT-4o Mini"),
        SimpleNamespace(id="gpt-4o-mini", name="dup"),
        SimpleNamespace(id="gpt-4.1", name=""),
        SimpleNamespace(id="   ", name="invalid"),
    ]

    class FakeModels:
        async def list(self, timeout=None):
            _ = timeout
            return SimpleNamespace(data=rows)

    fake_client = SimpleNamespace(models=FakeModels())
    monkeypatch.setattr(provider, "_client", lambda timeout=5: fake_client)

    models = await provider.fetch_models(timeout=3)

    assert [m.id for m in models] == ["gpt-4o-mini", "gpt-4.1"]
    assert [m.name for m in models] == ["GPT-4o Mini", "gpt-4.1"]
    assert not provider.models  # should not update provider state


async def test_list_model_api_error_returns_empty(monkeypatch) -> None:
    provider = _make_provider()

    class FakeModels:
        async def list(self, timeout=None):
            raise RuntimeError("failed")

    fake_client = SimpleNamespace(models=FakeModels())
    monkeypatch.setattr(provider, "_client", lambda timeout=5: fake_client)
    monkeypatch.setattr(openai_provider_module, "APIError", Exception)

    models = await provider.fetch_models(timeout=3)

    assert models == []


async def test_check_model_connection_success(monkeypatch) -> None:
    provider = _make_provider()
    captured: list[dict] = []

    class FakeStream:
        def __aiter__(self):
            return self

        async def __anext__(self):
            raise StopAsyncIteration

    class FakeCompletions:
        async def create(self, **kwargs):
            captured.append(kwargs)
            return FakeStream()

    fake_client = SimpleNamespace(
        chat=SimpleNamespace(completions=FakeCompletions()),
    )
    monkeypatch.setattr(provider, "_client", lambda timeout=5: fake_client)

    ok, msg = await provider.check_model_connection("gpt-4o-mini", timeout=4)

    assert ok is True
    assert msg == ""
    assert len(captured) == 1
    assert captured[0]["model"] == "gpt-4o-mini"
    assert captured[0]["timeout"] == 4
    assert captured[0]["max_tokens"] == 1
    assert captured[0]["stream"] is True
    assert captured[0]["messages"][0]["content"] == [
        {"type": "text", "text": "ping"},
    ]


async def test_local_check_model_connection_uses_plain_text(
    monkeypatch,
) -> None:
    provider = OpenAIProvider(
        id="lantai-local",
        name="Lantai Local",
        base_url="http://127.0.0.1:43210/v1",
        api_key="",
        is_local=True,
    )
    captured: list[dict] = []

    class FakeStream:
        def __aiter__(self):
            return self

        async def __anext__(self):
            raise StopAsyncIteration

    class FakeCompletions:
        async def create(self, **kwargs):
            captured.append(kwargs)
            return FakeStream()

    fake_client = SimpleNamespace(
        chat=SimpleNamespace(completions=FakeCompletions()),
    )
    monkeypatch.setattr(provider, "_client", lambda timeout=5: fake_client)

    ok, msg = await provider.check_model_connection("local-model", timeout=4)

    assert ok is True
    assert msg == ""
    assert captured[0]["messages"][0]["content"] == "ping"


def test_local_chat_model_normalizes_content_to_text(monkeypatch) -> None:
    captured: dict[str, object] = {}

    class FakeOpenAIChatModelCompat:
        def __init__(self, **kwargs) -> None:
            captured.update(kwargs)

    monkeypatch.setattr(
        "lantai.providers.openai_chat_model_compat.OpenAIChatModelCompat",
        FakeOpenAIChatModelCompat,
    )

    provider = OpenAIProvider(
        id="lantai-local",
        name="Lantai Local",
        base_url="http://127.0.0.1:43210/v1",
        api_key="",
        is_local=True,
    )

    provider.get_chat_model_instance("local-model")

    assert captured["normalize_content_to_text"] is True


async def test_check_model_connection_api_error_returns_false(
    monkeypatch,
) -> None:
    provider = _make_provider()

    class FakeCompletions:
        async def create(self, **kwargs):
            _ = kwargs
            raise RuntimeError("failed")

    fake_client = SimpleNamespace(
        chat=SimpleNamespace(completions=FakeCompletions()),
    )
    monkeypatch.setattr(provider, "_client", lambda timeout=5: fake_client)
    monkeypatch.setattr(openai_provider_module, "APIError", Exception)

    ok, msg = await provider.check_model_connection("gpt-4o-mini", timeout=4)

    assert ok is False
    assert msg == "API error when connecting to model 'gpt-4o-mini': failed"


async def test_update_config_updates_non_none_values_and_get_info() -> None:
    provider = _make_provider(is_custom=True)

    provider.update_config(
        {
            "name": "OpenAI Custom",
            "base_url": "https://new.example/v1",
            "api_key": "sk-new",
            "chat_model": "OpenAIChatModel",
            "api_key_prefix": "sk-",
            "generate_kwargs": {"temperature": 0.2, "top_p": 0.9},
        },
    )

    info = await provider.get_info(mock_secret=False)

    assert provider.name == "OpenAI Custom"
    assert provider.base_url == "https://new.example/v1"
    assert provider.api_key == "sk-new"
    assert provider.chat_model == "OpenAIChatModel"
    assert provider.api_key_prefix == "sk-"
    assert provider.generate_kwargs == {"temperature": 0.2, "top_p": 0.9}
    assert info.name == "OpenAI Custom"
    assert info.base_url == "https://new.example/v1"
    assert info.api_key == "sk-new"
    assert info.chat_model == "OpenAIChatModel"
    assert info.api_key_prefix == "sk-"
    assert info.generate_kwargs == {"temperature": 0.2, "top_p": 0.9}
    assert info.is_custom
    assert not info.support_connection_check


async def test_update_config_skips_none_values() -> None:  # noqa: E501
    provider = _make_provider()
    provider.api_key_prefix = "sk-"
    provider.generate_kwargs = {"temperature": 0.1}

    provider.update_config(
        {
            "name": None,
            "base_url": None,
            "api_key": None,
            "chat_model": None,
            "api_key_prefix": None,
            "generate_kwargs": None,
        },
    )

    info = await provider.get_info()

    assert provider.name == "OpenAI"
    assert provider.base_url == "https://mock-openai.local/v1"
    assert provider.api_key == "sk-test"
    assert provider.chat_model == "OpenAIChatModel"
    assert provider.api_key_prefix == "sk-"
    assert provider.generate_kwargs == {"temperature": 0.1}
    assert info.name == "OpenAI"
    assert info.base_url == "https://mock-openai.local/v1"
    assert info.api_key == "sk-******"
    assert info.chat_model == "OpenAIChatModel"
    assert info.api_key_prefix == "sk-"
    assert info.generate_kwargs == {"temperature": 0.1}


async def test_update_config_does_not_update_chat_model() -> None:
    provider = _make_provider()

    provider.update_config(
        {
            "chat_model": "AnotherChatModel",
            "name": "OpenAI Updated",
        },
    )

    info = await provider.get_info(mock_secret=False)

    assert provider.name == "OpenAI Updated"
    assert provider.chat_model == "OpenAIChatModel"
    assert info.name == "OpenAI Updated"
    assert info.chat_model == "OpenAIChatModel"


async def test_update_config_updates_chat_model_for_custom_provider() -> None:
    provider = _make_provider()
    provider.is_custom = True

    provider.update_config(
        {
            "chat_model": "AnotherChatModel",
            "name": "Custom OpenAI",
        },
    )

    info = await provider.get_info(mock_secret=False)

    assert provider.name == "Custom OpenAI"
    assert provider.chat_model == "AnotherChatModel"
    assert info.name == "Custom OpenAI"
    assert info.chat_model == "AnotherChatModel"


async def test_update_config_does_not_update_base_url_when_frozen() -> None:
    provider = _make_provider()
    provider.freeze_url = True

    provider.update_config(
        {
            "base_url": "https://blocked.example/v1",
            "api_key": "sk-frozen",
        },
    )

    info = await provider.get_info(mock_secret=False)

    assert provider.base_url == "https://mock-openai.local/v1"
    assert provider.api_key == "sk-frozen"
    assert info.base_url == "https://mock-openai.local/v1"
    assert info.api_key == "sk-frozen"
