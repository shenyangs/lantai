"""Conversation-triggered TokenDance video generation contracts."""

from __future__ import annotations

import logging
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest

from lantai.agents.tools import video_generation
from lantai.config.config import Config, ModelSlotConfig, VideoGenerationConfig
from lantai.exceptions import VideoGenerationAttemptException
from lantai.providers.provider import ModelInfo


def test_video_filename_caps_opaque_resource_urls() -> None:
    filename = video_generation._video_filename(  # noqa: SLF001
        "https://tokendance.space/gateway/resources/" + ("signed-token" * 80),
    )

    assert filename.endswith(".mp4")
    assert len(filename.encode("utf-8")) <= 120


@pytest.mark.asyncio
async def test_generate_video_uses_only_explicitly_enabled_media_model(
    monkeypatch,
    tmp_path,
) -> None:
    generator = AsyncMock(
        return_value={
            "task_id": "task-1",
            "protocol": "seedance:generations",
            "assets": [{"type": "video", "url": "https://cdn/video.mp4"}],
        },
    )
    provider = SimpleNamespace(
        id="tokendance",
        name="TokenDance",
        api_key="configured",
        models=[],
        extra_models=[
            ModelInfo(
                id="hidden-video",
                name="Hidden",
                supported_protocols=["seedance:generations"],
                use_for_video=False,
            ),
            ModelInfo(
                id="seedance-2.5",
                name="Seedance",
                supported_protocols=["seedance:generations"],
                use_for_video=True,
            ),
        ],
        generate_video=generator,
    )
    manager = SimpleNamespace(get_provider=lambda provider_id: provider)
    monkeypatch.setattr(
        video_generation.ProviderManager,
        "get_instance",
        lambda: manager,
    )
    monkeypatch.setattr(video_generation, "load_config", lambda: Config())
    monkeypatch.setattr(
        video_generation,
        "is_watcha_account_connected",
        lambda: True,
    )
    monkeypatch.setattr(video_generation, "DEFAULT_MEDIA_DIR", tmp_path)

    async def download(_url, _download_dir):
        target = tmp_path / "generated_video.mp4"
        target.write_bytes(b"video")
        return str(target), "video/mp4"

    monkeypatch.setattr(video_generation, "_download_video_to_cache", download)

    response = await video_generation.generate_video("海上日出")

    generator.assert_awaited_once()
    assert generator.await_args.kwargs["model_id"] == "seedance-2.5"
    assert response.content[0]["type"] == "video"
    assert response.metadata["task_id"] == "task-1"


@pytest.mark.asyncio
async def test_tokendance_video_tool_passes_runtime_submission_observer(
    monkeypatch,
    tmp_path,
) -> None:
    observer = object()

    class FakeTokenDanceProvider:
        id = "tokendance"
        name = "TokenDance"
        api_key = "configured"
        require_api_key = True
        models = [
            ModelInfo(
                id="seedance-2.5",
                name="Seedance",
                supported_protocols=["seedance:generations"],
                use_for_video=True,
            )
        ]
        extra_models = []

        async def generate_video(self, **kwargs):
            assert kwargs["submission_observer"] is observer
            return {
                "task_id": "task-bound",
                "protocol": "seedance:generations",
                "assets": [{"type": "video", "url": "https://cdn/video.mp4"}],
            }

    provider = FakeTokenDanceProvider()
    manager = SimpleNamespace(get_provider=lambda _provider_id: provider)
    monkeypatch.setattr(video_generation, "TokenDanceProvider", FakeTokenDanceProvider)
    monkeypatch.setattr(
        video_generation.ProviderManager,
        "get_instance",
        lambda: manager,
    )
    monkeypatch.setattr(video_generation, "load_config", lambda: Config())
    monkeypatch.setattr(
        video_generation,
        "is_watcha_account_connected",
        lambda: True,
    )
    monkeypatch.setattr(
        video_generation,
        "_tokendance_remote_submission_observer",
        lambda **_kwargs: observer,
    )
    monkeypatch.setattr(video_generation, "DEFAULT_MEDIA_DIR", tmp_path)

    async def download(_url, _download_dir):
        target = tmp_path / "generated_video.mp4"
        target.write_bytes(b"video")
        return str(target), "video/mp4"

    monkeypatch.setattr(video_generation, "_download_video_to_cache", download)

    response = await video_generation.generate_video("海上日出")

    assert response.metadata["task_id"] == "task-bound"


@pytest.mark.asyncio
async def test_generate_video_requires_watcha_login(monkeypatch) -> None:
    provider = SimpleNamespace(
        id="tokendance",
        name="TokenDance",
        api_key="configured",
        models=[
            ModelInfo(
                id="seedance-2.5",
                name="Seedance",
                supported_protocols=["seedance:generations"],
                use_for_video=True,
            ),
        ],
        extra_models=[],
        generate_video=AsyncMock(),
    )
    monkeypatch.setattr(
        video_generation.ProviderManager,
        "get_instance",
        lambda: SimpleNamespace(get_provider=lambda _provider_id: provider),
    )
    monkeypatch.setattr(video_generation, "load_config", lambda: Config())
    monkeypatch.setattr(
        video_generation,
        "is_watcha_account_connected",
        lambda: False,
    )

    with pytest.raises(RuntimeError, match="观猹"):
        await video_generation.generate_video("海上日出")


def test_video_tool_availability_follows_enabled_catalog_model(
    monkeypatch,
) -> None:
    provider = SimpleNamespace(
        api_key="configured",
        models=[
            ModelInfo(
                id="seedance-2.5",
                name="Seedance",
                supported_protocols=["seedance:generations"],
                use_for_video=True,
            ),
        ],
        extra_models=[],
        generate_video=AsyncMock(),
    )
    monkeypatch.setattr(
        video_generation.ProviderManager,
        "get_instance",
        lambda: SimpleNamespace(get_provider=lambda _provider_id: provider),
    )
    monkeypatch.setattr(video_generation, "load_config", lambda: Config())
    monkeypatch.setattr(
        video_generation,
        "is_watcha_account_connected",
        lambda: True,
    )

    assert video_generation.is_video_generation_available() is True


@pytest.mark.asyncio
async def test_generate_video_uses_central_failover_order_and_timeout(
    monkeypatch,
    tmp_path,
) -> None:
    first_generator = AsyncMock(
        side_effect=VideoGenerationAttemptException(
            "Video task reached an explicit terminal failure.",
            task_id="task-capacity",
            may_fail_over=True,
        ),
    )
    second_generator = AsyncMock(
        return_value={
            "task_id": "task-2",
            "protocol": "minimax:video_generation_v2",
            "assets": [{"type": "video", "url": "https://cdn/video.mp4"}],
        },
    )

    def make_provider(provider_id: str, model_id: str, generator):
        return SimpleNamespace(
            id=provider_id,
            name=provider_id.title(),
            api_key="configured",
            models=[
                ModelInfo(
                    id=model_id,
                    name=model_id,
                    supported_protocols=["minimax:video_generation_v2"],
                ),
            ],
            extra_models=[],
            generate_video=generator,
        )

    providers = {
        "first": make_provider("first", "video-a", first_generator),
        "second": make_provider("second", "video-b", second_generator),
    }
    manager = SimpleNamespace(get_provider=lambda provider_id: providers[provider_id])
    root = Config(
        video_generation=VideoGenerationConfig(
            enabled=True,
            models=[
                ModelSlotConfig(provider_id="first", model="video-a"),
                ModelSlotConfig(provider_id="second", model="video-b"),
            ],
            timeout_seconds=900,
            legacy_model_flags_migrated=True,
        ),
    )
    monkeypatch.setattr(video_generation, "load_config", lambda: root)
    monkeypatch.setattr(
        video_generation.ProviderManager,
        "get_instance",
        lambda: manager,
    )
    monkeypatch.setattr(
        video_generation,
        "is_watcha_account_connected",
        lambda: True,
    )
    monkeypatch.setattr(video_generation, "DEFAULT_MEDIA_DIR", tmp_path)

    async def download(_url, _download_dir):
        target = tmp_path / "generated_video.mp4"
        target.write_bytes(b"video")
        return str(target), "video/mp4"

    monkeypatch.setattr(video_generation, "_download_video_to_cache", download)

    response = await video_generation.generate_video("一朵花缓慢绽放")

    assert first_generator.await_args.kwargs["timeout"] == 900
    assert second_generator.await_args.kwargs["timeout"] == 900
    assert response.metadata["provider_id"] == "second"
    assert response.metadata["model"] == "video-b"
    assert response.metadata["attempts"] == [
        {
            "provider_id": "first",
            "model": "video-a",
            "error": "Video task reached an explicit terminal failure.",
            "task_id": "task-capacity",
        },
    ]


@pytest.mark.asyncio
async def test_generate_video_ignores_tokendance_recovery_headers_from_other_providers(
    monkeypatch,
    tmp_path,
) -> None:
    first_generator = AsyncMock(
        side_effect=VideoGenerationAttemptException(
            "Video task reached an explicit terminal failure.",
            task_id="foreign-task",
            may_fail_over=True,
            response=SimpleNamespace(
                headers={"TokenDance-Recovery-Action": "top_up_balance"},
            ),
        ),
    )
    second_generator = AsyncMock(
        return_value={
            "task_id": "task-foreign-failover",
            "protocol": "minimax:video_generation_v2",
            "assets": [{"type": "video", "url": "https://cdn/video.mp4"}],
        },
    )

    def make_provider(provider_id: str, model_id: str, generator):
        return SimpleNamespace(
            id=provider_id,
            name=provider_id.title(),
            api_key="configured",
            models=[
                ModelInfo(
                    id=model_id,
                    name=model_id,
                    supported_protocols=["minimax:video_generation_v2"],
                ),
            ],
            extra_models=[],
            generate_video=generator,
        )

    providers = {
        "foreign": make_provider("foreign", "video-a", first_generator),
        "second": make_provider("second", "video-b", second_generator),
    }
    root = Config(
        video_generation=VideoGenerationConfig(
            enabled=True,
            models=[
                ModelSlotConfig(provider_id="foreign", model="video-a"),
                ModelSlotConfig(provider_id="second", model="video-b"),
            ],
            legacy_model_flags_migrated=True,
        ),
    )
    monkeypatch.setattr(video_generation, "load_config", lambda: root)
    monkeypatch.setattr(
        video_generation.ProviderManager,
        "get_instance",
        lambda: SimpleNamespace(
            get_provider=lambda provider_id: providers[provider_id],
        ),
    )
    monkeypatch.setattr(video_generation, "DEFAULT_MEDIA_DIR", tmp_path)

    async def download(_url, _download_dir):
        target = tmp_path / "generated_video.mp4"
        target.write_bytes(b"video")
        return str(target), "video/mp4"

    monkeypatch.setattr(video_generation, "_download_video_to_cache", download)

    response = await video_generation.generate_video("一束光穿过森林")

    assert response.metadata["provider_id"] == "second"
    second_generator.assert_awaited_once()


@pytest.mark.asyncio
async def test_generate_video_never_fails_over_after_ambiguous_submission(
    monkeypatch,
) -> None:
    first_generator = AsyncMock(
        side_effect=VideoGenerationAttemptException(
            "Video task polling timed out.",
            task_id="task-ambiguous",
            may_fail_over=False,
        ),
    )
    providers = {
        "tokendance": SimpleNamespace(
            id="tokendance",
            name="TokenDance",
            api_key="configured",
            models=[
                ModelInfo(
                    id="seedance-2.0",
                    name="Seedance 2.0",
                    supported_protocols=["seedance:generations"],
                ),
                ModelInfo(
                    id="minimax-h3",
                    name="MiniMax H3",
                    supported_protocols=["minimax:video_generation_v2"],
                ),
            ],
            extra_models=[],
            generate_video=first_generator,
        ),
    }
    root = Config(
        video_generation=VideoGenerationConfig(
            enabled=True,
            models=[
                ModelSlotConfig(
                    provider_id="tokendance",
                    model="seedance-2.0",
                ),
                ModelSlotConfig(
                    provider_id="tokendance",
                    model="minimax-h3",
                ),
            ],
            legacy_model_flags_migrated=True,
        ),
    )
    manager = SimpleNamespace(get_provider=lambda provider_id: providers[provider_id])
    monkeypatch.setattr(video_generation, "load_config", lambda: root)
    monkeypatch.setattr(
        video_generation.ProviderManager,
        "get_instance",
        lambda: manager,
    )
    monkeypatch.setattr(
        video_generation,
        "is_watcha_account_connected",
        lambda: True,
    )

    with pytest.raises(VideoGenerationAttemptException) as captured:
        await video_generation.generate_video("海边日落")

    assert captured.value.task_id == "task-ambiguous"
    assert first_generator.await_count == 1


@pytest.mark.asyncio
async def test_poll_recovery_header_does_not_hide_existing_task_id(
    monkeypatch,
) -> None:
    failure = VideoGenerationAttemptException(
        "Video task status request failed.",
        task_id="task-existing",
        response=SimpleNamespace(
            headers={"TokenDance-Recovery-Action": "top_up_balance"},
        ),
    )
    provider = SimpleNamespace(
        id="tokendance",
        name="TokenDance",
        api_key="configured",
        models=[
            ModelInfo(
                id="seedance-2.0",
                name="Seedance 2.0",
                supported_protocols=["seedance:generations"],
            ),
        ],
        extra_models=[],
        generate_video=AsyncMock(side_effect=failure),
    )
    monkeypatch.setattr(
        video_generation.ProviderManager,
        "get_instance",
        lambda: SimpleNamespace(get_provider=lambda _provider_id: provider),
    )
    monkeypatch.setattr(
        video_generation,
        "load_config",
        lambda: Config(
            video_generation=VideoGenerationConfig(
                enabled=True,
                models=[
                    ModelSlotConfig(
                        provider_id="tokendance",
                        model="seedance-2.0",
                    ),
                ],
                legacy_model_flags_migrated=True,
            ),
        ),
    )
    monkeypatch.setattr(
        video_generation,
        "is_watcha_account_connected",
        lambda: True,
    )

    with pytest.raises(VideoGenerationAttemptException) as captured:
        await video_generation.generate_video("海边日落")

    assert captured.value.task_id == "task-existing"


@pytest.mark.asyncio
async def test_generate_video_only_fails_over_after_explicit_terminal_failure(
    monkeypatch,
    tmp_path,
) -> None:
    first_generator = AsyncMock(
        side_effect=VideoGenerationAttemptException(
            "Video task failed.",
            task_id="task-failed",
            may_fail_over=True,
        ),
    )
    second_generator = AsyncMock(
        return_value={
            "task_id": "task-second",
            "protocol": "seedance:generations",
            "assets": [{"type": "video", "url": "https://cdn/video.mp4"}],
        },
    )

    def make_provider(provider_id, model_id, generator):
        return SimpleNamespace(
            id=provider_id,
            name=provider_id,
            api_key="configured",
            models=[
                ModelInfo(
                    id=model_id,
                    name=model_id,
                    supported_protocols=["seedance:generations"],
                ),
            ],
            extra_models=[],
            generate_video=generator,
        )

    providers = {
        "first": make_provider("first", "video-a", first_generator),
        "second": make_provider("second", "video-b", second_generator),
    }
    monkeypatch.setattr(
        video_generation,
        "load_config",
        lambda: Config(
            video_generation=VideoGenerationConfig(
                enabled=True,
                models=[
                    ModelSlotConfig(provider_id="first", model="video-a"),
                    ModelSlotConfig(provider_id="second", model="video-b"),
                ],
                legacy_model_flags_migrated=True,
            ),
        ),
    )
    monkeypatch.setattr(
        video_generation.ProviderManager,
        "get_instance",
        lambda: SimpleNamespace(
            get_provider=lambda provider_id: providers[provider_id],
        ),
    )

    async def download(_url, _download_dir):
        return str(tmp_path / "video.mp4"), "video/mp4"

    monkeypatch.setattr(video_generation, "_download_video_to_cache", download)

    response = await video_generation.generate_video("海边日落")

    assert first_generator.await_count == 1
    assert second_generator.await_count == 1
    assert response.metadata["attempts"][0]["task_id"] == "task-failed"


class _FakeDownloadResponse:
    def __init__(self, *, status: int, content_type: str, chunks: list[bytes]):
        self.status_code = status
        self.headers = {"content-type": content_type}
        self._chunks = chunks

    async def aiter_bytes(self, chunk_size=65536):
        del chunk_size
        for chunk in self._chunks:
            yield chunk


class _FakeDownloadContext:
    def __init__(self, response):
        self.response = response

    async def __aenter__(self):
        return self.response

    async def __aexit__(self, *_args):
        return None


class _FakeDownloadClient:
    def __init__(self, response):
        self.response = response

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return None

    def stream(self, *_args, **_kwargs):
        return _FakeDownloadContext(self.response)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("content_type", "payload", "suffix", "mime_type"),
    [
        ("video/mp4", b"\x00\x00\x00\x18ftypisomvideo", ".mp4", "video/mp4"),
        ("video/webm", b"\x1a\x45\xdf\xa3webmvideo", ".webm", "video/webm"),
    ],
)
async def test_video_download_validates_container_and_atomically_renames(
    monkeypatch,
    tmp_path,
    content_type,
    payload,
    suffix,
    mime_type,
) -> None:
    response = _FakeDownloadResponse(
        status=200,
        content_type=content_type,
        chunks=[payload[:3], payload[3:]],
    )
    monkeypatch.setattr(
        video_generation.httpx,
        "AsyncClient",
        lambda **_kwargs: _FakeDownloadClient(response),
    )

    path, detected_mime = await video_generation._download_video_to_cache(
        "https://user:pass@cdn.example/result?token=SECRET#fragment",
        tmp_path,
    )

    assert Path(path).suffix == suffix
    assert Path(path).read_bytes() == payload
    assert detected_mime == mime_type
    assert not list(tmp_path.glob("*.part"))


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("status", "content_type", "payload"),
    [
        (403, "text/html", b"<html>forbidden</html>"),
        (404, "text/html", b"<html>missing</html>"),
        (200, "video/mp4", b""),
        (200, "video/webm", b"\x00\x00\x00\x18ftypisomvideo"),
    ],
)
async def test_video_download_rejects_false_success_without_artifact_or_secret(
    monkeypatch,
    tmp_path,
    status,
    content_type,
    payload,
) -> None:
    response = _FakeDownloadResponse(
        status=status,
        content_type=content_type,
        chunks=[payload] if payload else [],
    )
    monkeypatch.setattr(
        video_generation.httpx,
        "AsyncClient",
        lambda **_kwargs: _FakeDownloadClient(response),
    )

    with pytest.raises(video_generation.VideoDownloadError) as captured:
        await video_generation._download_video_to_cache(
            "https://user:pass@cdn.example/result?token=SECRET#fragment",
            tmp_path,
        )

    assert "SECRET" not in str(captured.value)
    assert "user:pass" not in str(captured.value)
    assert list(tmp_path.iterdir()) == []


@pytest.mark.asyncio
async def test_video_download_redacts_signed_url_from_real_httpx_logs(
    monkeypatch,
    tmp_path,
    caplog,
) -> None:
    signed_url = (
        "https://leaky-user:leaky-pass@cdn.example/result.mp4"
        "?X-Signature=TOP_SECRET_SIGNATURE#private-fragment"
    )
    real_async_client = httpx.AsyncClient

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            headers={"content-type": "video/mp4"},
            content=b"\x00\x00\x00\x18ftypisomvideo",
        )

    transport = httpx.MockTransport(handler)
    monkeypatch.setattr(
        video_generation.httpx,
        "AsyncClient",
        lambda **kwargs: real_async_client(transport=transport, **kwargs),
    )

    with caplog.at_level(logging.INFO, logger="httpx"):
        await video_generation._download_video_to_cache(signed_url, tmp_path)

    assert "TOP_SECRET_SIGNATURE" not in caplog.text
    assert "leaky-user" not in caplog.text
    assert "leaky-pass" not in caplog.text
    assert "private-fragment" not in caplog.text
    assert "https://cdn.example/result.mp4" in caplog.text


@pytest.mark.asyncio
async def test_generate_video_marks_temporary_result_without_logging_signed_url(
    monkeypatch,
    caplog,
) -> None:
    signed_url = (
        "https://user:pass@cdn.example/result.mp4?token=SECRET#fragment"
    )
    provider = SimpleNamespace(
        id="provider",
        name="Provider",
        api_key="configured",
        models=[
            ModelInfo(
                id="video",
                name="Video",
                supported_protocols=["seedance:generations"],
            ),
        ],
        extra_models=[],
        generate_video=AsyncMock(
            return_value={
                "task_id": "task-temporary",
                "assets": [{"type": "video", "url": signed_url}],
            },
        ),
    )
    monkeypatch.setattr(
        video_generation.ProviderManager,
        "get_instance",
        lambda: SimpleNamespace(get_provider=lambda _provider_id: provider),
    )
    monkeypatch.setattr(
        video_generation,
        "load_config",
        lambda: Config(
            video_generation=VideoGenerationConfig(
                enabled=True,
                models=[
                    ModelSlotConfig(provider_id="provider", model="video"),
                ],
                legacy_model_flags_migrated=True,
            ),
        ),
    )

    async def fail_download(_url, _download_dir):
        raise video_generation.VideoDownloadError("HTTP 403")

    monkeypatch.setattr(
        video_generation,
        "_download_video_to_cache",
        fail_download,
    )

    response = await video_generation.generate_video("海上日出")

    assert response.metadata["durable"] is False
    assert response.content[0]["metadata"]["durable"] is False
    assert "临时链接" in response.content[1]["text"]
    assert "SECRET" not in caplog.text
    assert "user:pass" not in caplog.text
