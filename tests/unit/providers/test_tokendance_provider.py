"""Contracts for TokenDance's live, multi-protocol model catalog."""

from __future__ import annotations

from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import Mock

import httpx
import pytest

from lantai.exceptions import ProviderError, VideoGenerationAttemptException
from lantai.providers.provider import ModelInfo
from lantai.providers.provider_state import (
    TOKENDANCE_VERIFIED_CAPABILITIES_META_KEY,
)
from lantai.providers.tokendance_catalog import CATALOG
from lantai.providers.tokendance_oauth import TOKENDANCE_APP_URL
from lantai.providers.tokendance_provider import TokenDanceProvider


def _provider(*models: ModelInfo) -> TokenDanceProvider:
    verified_models = deepcopy(CATALOG)
    for model in models:
        verified_models[model.id] = {
            "supported_protocols": list(model.supported_protocols),
            "context_window": model.context_window,
        }
    return TokenDanceProvider(
        id="tokendance",
        name="TokenDance",
        base_url="https://tokendance.space/gateway/v1",
        api_key="oauth-secret",
        models=list(models),
        freeze_url=True,
        support_model_discovery=True,
        meta={
            TOKENDANCE_VERIFIED_CAPABILITIES_META_KEY: {
                "schema_version": 1,
                "revision": "test-verified",
                "models": verified_models,
            },
        },
    )


def test_catalog_preserves_protocols_and_context_capability() -> None:
    models = TokenDanceProvider._normalize_models_payload(
        {
            "data": [
                {
                    "id": "qwen3.8-flash",
                    "name": "Qwen: Qwen3.8 Flash",
                    "context_length": 1_000_000,
                    "supported_protocols": [
                        "openai:chat-completions",
                        "openai:responses",
                        "anthropic:messages",
                    ],
                },
                {
                    "id": "seedream-5.0-pro",
                    "name": "ByteDance Seed: Seedream 5.0 Pro",
                    "context_length": 0,
                    "supported_protocols": ["ark:image-generations"],
                },
            ],
        },
    )

    assert models[0].id == "qwen3.8-flash"
    assert models[0].context_window == 1_000_000
    assert models[0].max_context_window == 1_000_000
    assert models[0].capability_source == "provider-catalog"
    assert models[0].supported_protocols == [
        "openai:chat-completions",
        "openai:responses",
        "anthropic:messages",
    ]
    assert models[0].show_in_chat is False
    assert models[0].supports_chat is True
    assert models[1].supports_chat is False
    assert models[1].supports_image_generation is True


def test_video_catalog_model_is_media_only_and_hidden_from_chat() -> None:
    model = TokenDanceProvider._normalize_models_payload(
        {
            "data": [
                {
                    "id": "seedance-2.5",
                    "name": "Seedance 2.5",
                    "supported_protocols": ["seedance:generations"],
                },
            ],
        },
    )[0]

    assert model.supports_chat is False
    assert model.supports_video_generation is True
    assert model.show_in_chat is False
    assert model.use_for_video is False

    image_only = ModelInfo(
        id="happyhorse-1.1-i2v",
        name="HappyHorse I2V",
        supported_protocols=["happyhorse:video-synthesis"],
    )
    text_video = image_only.model_copy(
        update={"id": "happyhorse-1.1-t2v"},
    )
    assert image_only.supports_video_generation is True
    assert image_only.supports_text_to_video is False
    assert text_video.supports_text_to_video is True


@pytest.mark.parametrize(
    ("protocol", "expected_submit", "expected_query_fragment"),
    [
        (
            "seedance:generations",
            "/gateway/ark/v3/generations/tasks",
            "/gateway/ark/v3/generations/tasks/{task_id}",
        ),
        (
            "kling:text2video",
            "/gateway/kling/v1/text2video",
            "/gateway/kling/v1/text2video/{task_id}",
        ),
        (
            "wan3:video-synthesis",
            "/gateway/alibaba/wan3/v1/video-synthesis",
            "/gateway/alibaba/wan3/v1/tasks/{task_id}",
        ),
        (
            "happyhorse:video-synthesis",
            "/gateway/alibaba/happyhorse/v1/video-synthesis",
            "/gateway/alibaba/happyhorse/v1/tasks/{task_id}",
        ),
        (
            "minimax:video_generation_v2",
            "/gateway/minimax/v2/video_generation",
            "/gateway/minimax/v2/query/video_generation/{task_id}",
        ),
    ],
)
def test_video_profiles_follow_catalog_protocol(
    protocol,
    expected_submit,
    expected_query_fragment,
) -> None:
    profile = TokenDanceProvider._video_request_profile(
        protocol=protocol,
        model_id="video-model",
        prompt="海上日出",
        duration=5,
        aspect_ratio="16:9",
        resolution="720p",
    )

    assert profile.submit_url.endswith(expected_submit)
    assert profile.query_url_template.endswith(expected_query_fragment)


@pytest.mark.asyncio
async def test_seedance_video_submission_and_polling_use_one_task(
    monkeypatch,
) -> None:
    provider = _provider(
        ModelInfo(
            id="seedance-2.5",
            name="Seedance",
            supported_protocols=["seedance:generations"],
            use_for_video=True,
        ),
    )
    calls: list[tuple[str, str, dict]] = []

    class FakeResponse:
        def __init__(self, payload):
            self.payload = payload

        def raise_for_status(self) -> None:
            return None

        def json(self):
            return self.payload

    class FakeClient:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

        async def post(self, url, **kwargs):
            calls.append(("POST", url, kwargs))
            return FakeResponse({"id": "task-1"})

        async def get(self, url, **kwargs):
            calls.append(("GET", url, kwargs))
            return FakeResponse(
                {
                    "status": "succeeded",
                    "content": {"video_url": "https://cdn.example/video.mp4"},
                },
            )

    monkeypatch.setattr(
        "lantai.providers.tokendance_provider.httpx.AsyncClient",
        lambda **_kwargs: FakeClient(),
    )

    result = await provider.generate_video(
        "seedance-2.5",
        "海上日出",
        duration=5,
        aspect_ratio="16:9",
        resolution="720p",
        timeout=30,
    )

    assert [call[0] for call in calls] == ["POST", "GET"]
    assert calls[0][2]["headers"]["X-App-URL"] == TOKENDANCE_APP_URL
    assert calls[0][2]["json"]["content"] == [
        {"type": "text", "text": "海上日出"},
    ]
    assert result["task_id"] == "task-1"
    assert result["assets"] == [
        {"type": "video", "url": "https://cdn.example/video.mp4"},
    ]


@pytest.mark.asyncio
async def test_video_submission_observer_fences_post_and_tracks_late_responses(
    monkeypatch,
) -> None:
    provider = _provider(
        ModelInfo(
            id="seedance-2.5",
            name="Seedance",
            supported_protocols=["seedance:generations"],
            use_for_video=True,
        ),
    )
    events: list[object] = []

    class Observer:
        def before_submit(self) -> None:
            events.append("before_submit")

        def response_received(self) -> None:
            events.append("response_received")

        def submission_unknown(self) -> None:
            events.append("submission_unknown")

        def submitted(self, task_id: str) -> None:
            events.append(("submitted", task_id))

        def succeeded(self, result_hash: str) -> None:
            events.append(("succeeded", result_hash))

        def failed(self, failure_hash: str) -> None:
            events.append(("failed", failure_hash))

    class FakeResponse:
        def __init__(self, payload):
            self.payload = payload

        def raise_for_status(self) -> None:
            return None

        def json(self):
            return self.payload

    class FakeClient:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

        async def post(self, *_args, **_kwargs):
            assert events == ["before_submit"]
            return FakeResponse({"id": "task-bound"})

        async def get(self, *_args, **_kwargs):
            assert events[-1] == ("submitted", "task-bound")
            return FakeResponse(
                {
                    "status": "succeeded",
                    "content": {"video_url": "https://cdn.example/video.mp4"},
                },
            )

    monkeypatch.setattr(
        "lantai.providers.tokendance_provider.httpx.AsyncClient",
        lambda **_kwargs: FakeClient(),
    )

    await provider.generate_video(
        "seedance-2.5",
        "海上日出",
        timeout=30,
        submission_observer=Observer(),
    )

    assert events[:4] == [
        "before_submit",
        "response_received",
        ("submitted", "task-bound"),
        "response_received",
    ]
    assert events[-1][0] == "succeeded"
    assert str(events[-1][1]).startswith("sha256:")


@pytest.mark.asyncio
async def test_video_submission_observer_keeps_ambiguous_post_unknown(
    monkeypatch,
) -> None:
    provider = _provider(
        ModelInfo(
            id="seedance-2.5",
            name="Seedance",
            supported_protocols=["seedance:generations"],
        ),
    )
    events: list[object] = []

    class Observer:
        def before_submit(self) -> None:
            events.append("before_submit")

        def response_received(self) -> None:
            events.append("response_received")

        def submission_unknown(self) -> None:
            events.append("submission_unknown")

        def submitted(self, task_id: str) -> None:
            events.append(("submitted", task_id))

        def succeeded(self, result_hash: str) -> None:
            events.append(("succeeded", result_hash))

        def failed(self, failure_hash: str) -> None:
            events.append(("failed", failure_hash))

    class FakeClient:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

        async def post(self, *_args, **_kwargs):
            raise httpx.ReadTimeout("lost response")

    monkeypatch.setattr(
        "lantai.providers.tokendance_provider.httpx.AsyncClient",
        lambda **_kwargs: FakeClient(),
    )

    with pytest.raises(VideoGenerationAttemptException, match="outcome is unknown"):
        await provider.generate_video(
            "seedance-2.5",
            "海上日出",
            timeout=30,
            submission_observer=Observer(),
        )

    assert events == ["before_submit", "submission_unknown"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "poll_failure",
    ["read_timeout", "http_5xx", "invalid_json", "client_deadline"],
)
async def test_video_poll_failures_after_task_id_never_allow_failover(
    monkeypatch,
    poll_failure: str,
) -> None:
    provider = _provider(
        ModelInfo(
            id="seedance-2.5",
            name="Seedance",
            supported_protocols=["seedance:generations"],
        ),
    )
    calls: list[str] = []

    class FakeResponse:
        status_code = 200
        headers: dict[str, str] = {}

        def __init__(self, payload=None):
            self.payload = payload

        def raise_for_status(self) -> None:
            if poll_failure == "http_5xx" and self.payload == "query":
                response = httpx.Response(
                    503,
                    request=httpx.Request(
                        "GET",
                        "https://cdn.example/task?token=SECRET",
                    ),
                )
                raise httpx.HTTPStatusError(
                    "server error",
                    request=response.request,
                    response=response,
                )

        def json(self):
            if poll_failure == "invalid_json" and self.payload == "query":
                raise ValueError("SECRET malformed response")
            if self.payload == "query":
                return {"status": "processing"}
            return self.payload

    class FakeClient:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

        async def post(self, *_args, **_kwargs):
            calls.append("POST")
            return FakeResponse({"id": "task-safe"})

        async def get(self, *_args, **_kwargs):
            calls.append("GET")
            if poll_failure == "read_timeout":
                request = httpx.Request(
                    "GET",
                    "https://cdn.example/task?token=SECRET",
                )
                raise httpx.ReadTimeout("SECRET timeout", request=request)
            return FakeResponse("query")

    monkeypatch.setattr(
        "lantai.providers.tokendance_provider.httpx.AsyncClient",
        lambda **_kwargs: FakeClient(),
    )
    if poll_failure == "client_deadline":
        monotonic = Mock(side_effect=[0.0, 11.0])
        monkeypatch.setattr(
            "lantai.providers.tokendance_provider.time",
            SimpleNamespace(monotonic=monotonic),
        )

    with pytest.raises(VideoGenerationAttemptException) as captured:
        await provider.generate_video(
            "seedance-2.5",
            "海上日出",
            timeout=10,
            poll_interval=1,
        )

    assert captured.value.task_id == "task-safe"
    assert captured.value.may_fail_over is False
    assert "SECRET" not in str(captured.value)
    assert calls == ["POST", "GET"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("status", "may_fail_over"),
    [("failed", True), ("unknown", False), ("succeeded", False)],
)
async def test_video_terminal_status_controls_failover_explicitly(
    monkeypatch,
    status: str,
    may_fail_over: bool,
) -> None:
    provider = _provider(
        ModelInfo(
            id="seedance-2.5",
            name="Seedance",
            supported_protocols=["seedance:generations"],
        ),
    )

    class FakeResponse:
        headers: dict[str, str] = {}

        def __init__(self, payload):
            self.payload = payload

        def raise_for_status(self) -> None:
            return None

        def json(self):
            return self.payload

    class FakeClient:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

        async def post(self, *_args, **_kwargs):
            return FakeResponse({"id": "task-terminal"})

        async def get(self, *_args, **_kwargs):
            return FakeResponse({"status": status})

    monkeypatch.setattr(
        "lantai.providers.tokendance_provider.httpx.AsyncClient",
        lambda **_kwargs: FakeClient(),
    )

    with pytest.raises(VideoGenerationAttemptException) as captured:
        await provider.generate_video("seedance-2.5", "海上日出", timeout=10)

    assert captured.value.task_id == "task-terminal"
    assert captured.value.may_fail_over is may_fail_over


@pytest.mark.asyncio
async def test_tokendance_provider_info_exposes_video_runtime_contract() -> None:
    info = await _provider().get_info()

    assert info.supports_video_generation is True


def test_native_chat_uses_release_chat_first_policy() -> None:
    provider = _provider(
        ModelInfo(
            id="qwen3.8-flash",
            name="Qwen3.8 Flash",
            supported_protocols=[
                "openai:chat-completions",
                "openai:responses",
            ],
        ),
    )

    endpoint = provider.resolve_model_endpoint("qwen3.8-flash")

    assert endpoint.native_kind == "openai_compatible"
    assert endpoint.base_url == "https://tokendance.space/gateway/v1"
    assert endpoint.metadata["supported_protocols"] == [
        "openai:chat-completions",
        "openai:responses",
    ]
    assert endpoint.metadata["protocol_source"] == "live_or_configured"
    assert endpoint.metadata["routing_snapshot"] == {
        "model_id": "qwen3.8-flash",
        "protocol": "openai:chat-completions",
        "endpoint_family": "openai",
        "native_kind": "openai_compatible",
        "purpose": "chat",
        "adapter_version": "tokendance-routing-v1",
        "allowed_data_scope": "tokendance",
        "allow_provider_fallback": False,
        "reasons": [
            "live_catalog",
            "verified_capability",
            "fixed_priority",
        ],
        "catalog_revision": endpoint.metadata["catalog_revision"],
        "verification_revision": "test-verified",
    }
    assert endpoint.default_headers == {"X-App-URL": TOKENDANCE_APP_URL}


def test_responses_wins_over_anthropic_for_every_chat_entrypoint() -> None:
    provider = _provider(
        ModelInfo(
            id="responses-first",
            name="Responses first",
            supported_protocols=[
                "openai:responses",
                "anthropic:messages",
            ],
        ),
    )

    endpoint = provider.resolve_model_endpoint("responses-first")

    assert endpoint.native_kind == "openai_responses"
    assert endpoint.family == "openai"


def test_anthropic_only_model_uses_anthropic_gateway() -> None:
    provider = _provider(
        ModelInfo(
            id="anthropic-only",
            name="Anthropic only",
            supported_protocols=["anthropic:messages"],
        ),
    )

    endpoint = provider.resolve_model_endpoint("anthropic-only")

    assert endpoint.family == "anthropic"
    assert endpoint.native_kind == "legacy"
    assert endpoint.chat_model == "AnthropicChatModel"
    assert endpoint.base_url == "https://tokendance.space/gateway"
    assert endpoint.default_headers == {"X-App-URL": TOKENDANCE_APP_URL}
    assert provider._anthropic_delegate()._default_headers() == {
        "X-App-URL": TOKENDANCE_APP_URL,
    }


def test_image_only_model_cannot_be_selected_for_chat() -> None:
    provider = _provider(
        ModelInfo(
            id="seedream-5.0-lite",
            name="Seedream",
            supported_protocols=[
                "ark:image-generations",
                "openai:image-generations",
            ],
        ),
    )

    with pytest.raises(
        ProviderError, match="does not support a chat protocol"
    ):
        provider.resolve_model_endpoint("seedream-5.0-lite")


@pytest.mark.asyncio
async def test_responses_only_probe_uses_responses_protocol(
    monkeypatch,
) -> None:
    provider = _provider(
        ModelInfo(
            id="responses-only",
            name="Responses only",
            supported_protocols=["openai:responses"],
        ),
    )
    captured: dict = {}

    class FakeStream:
        def __aiter__(self):
            return self

        async def __anext__(self):
            raise StopAsyncIteration

    class FakeResponses:
        async def create(self, **kwargs):
            captured.update(kwargs)
            return FakeStream()

    monkeypatch.setattr(
        provider,
        "_client",
        lambda timeout=5: SimpleNamespace(responses=FakeResponses()),
    )

    ok, message = await provider.check_model_connection(
        "responses-only",
        timeout=4,
    )

    assert ok is True
    assert message == ""
    assert captured["model"] == "responses-only"
    assert captured["input"] == "ping"
    assert captured["stream"] is True


@pytest.mark.asyncio
async def test_catalog_fetch_is_public_and_does_not_send_api_key(
    monkeypatch,
) -> None:
    provider = _provider()
    captured: dict = {}

    class FakeResponse:
        def raise_for_status(self) -> None:
            return None

        def json(self) -> dict:
            return {
                "data": [
                    {
                        "id": "deepseek-v4.1-flash",
                        "name": "DeepSeek V4.1 Flash",
                        "context_length": 1_000_000,
                        "supported_protocols": ["openai:responses"],
                    },
                ],
            }

    class FakeClient:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

        async def get(self, url, **kwargs):
            captured.update(url=url, kwargs=kwargs)
            return FakeResponse()

    monkeypatch.setattr(
        "lantai.providers.tokendance_provider.httpx.AsyncClient",
        lambda **_kwargs: FakeClient(),
    )

    models = await provider.fetch_models(timeout=3)

    assert [model.id for model in models] == ["deepseek-v4.1-flash"]
    assert captured == {
        "url": "https://tokendance.space/gateway/v1/models",
        "kwargs": {},
    }


@pytest.mark.asyncio
async def test_ark_image_generation_uses_native_endpoint_without_leaking_key(
    monkeypatch,
) -> None:
    provider = _provider(
        ModelInfo(
            id="seedream-5.0-pro",
            name="Seedream",
            supported_protocols=["ark:image-generations"],
        ),
    )
    captured: dict = {}

    class FakeResponse:
        def raise_for_status(self) -> None:
            return None

        def json(self) -> dict:
            return {"data": [{"url": "https://cdn.example/image.png"}]}

    class FakeClient:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

        async def post(self, url, **kwargs):
            captured.update(url=url, kwargs=kwargs)
            return FakeResponse()

    monkeypatch.setattr(
        "lantai.providers.tokendance_provider.httpx.AsyncClient",
        lambda **_kwargs: FakeClient(),
    )

    result = await provider.generate_image(
        "seedream-5.0-pro",
        "水墨山峦",
        size="2K",
    )

    assert captured["url"] == (
        "https://tokendance.space/gateway/ark/v3/images/generations"
    )
    assert captured["kwargs"]["json"] == {
        "model": "seedream-5.0-pro",
        "prompt": "水墨山峦",
        "size": "2K",
        "output_format": "png",
        "response_format": "url",
        "watermark": False,
    }
    assert captured["kwargs"]["headers"] == {
        "Authorization": "Bearer oauth-secret",
        "Content-Type": "application/json",
        "X-App-URL": TOKENDANCE_APP_URL,
    }
    assert result["assets"] == [
        {"type": "image", "url": "https://cdn.example/image.png"},
    ]


@pytest.mark.asyncio
async def test_ark_image_edit_embeds_local_reference_as_data_url(
    monkeypatch,
    tmp_path,
) -> None:
    provider = _provider(
        ModelInfo(
            id="seedream-5.0-lite",
            name="Seedream",
            supported_protocols=[
                "ark:image-generations",
                "openai:image-generations",
            ],
        ),
    )
    image_path = tmp_path / "reference.png"
    image_path.write_bytes(b"png-bytes")
    captured: dict = {}

    class FakeResponse:
        def raise_for_status(self) -> None:
            return None

        def json(self) -> dict:
            return {"data": [{"url": "https://cdn.example/edited.png"}]}

    class FakeClient:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

        async def post(self, url, **kwargs):
            captured.update(url=url, kwargs=kwargs)
            return FakeResponse()

    monkeypatch.setattr(
        "lantai.providers.tokendance_provider.httpx.AsyncClient",
        lambda **_kwargs: FakeClient(),
    )

    result = await provider.edit_image(
        "seedream-5.0-lite",
        "改成水彩",
        [str(image_path)],
        size="2K",
    )

    assert captured["kwargs"]["json"]["image"] == (
        "data:image/png;base64,cG5nLWJ5dGVz"
    )
    assert result["assets"][0]["url"].endswith("edited.png")
