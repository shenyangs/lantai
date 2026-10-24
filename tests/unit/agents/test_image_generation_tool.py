"""Behavior locks for image generation inside chat."""

from __future__ import annotations

import base64
import os
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from lantai.agents.tools import image_generation
from lantai.config.config import ImageGenerationConfig, ModelSlotConfig
from lantai.config.context import set_current_workspace_dir
from lantai.exceptions import TokenDanceRecoveryException
from lantai.providers.provider import ModelInfo
from lantai.storage_retention import clean_storage_retention


@pytest.fixture(autouse=True)
def _cache_remote_images_in_test_media_dir(tmp_path, monkeypatch):
    async def fake_download(url: str, filename=None, download_dir: str = "") -> str:
        path = Path(download_dir) / str(filename)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(f"cached:{url}".encode())
        return str(path)

    monkeypatch.setattr(image_generation, "DEFAULT_MEDIA_DIR", tmp_path)
    monkeypatch.setattr(
        image_generation,
        "download_file_from_url",
        fake_download,
    )
    set_current_workspace_dir(None)
    yield
    set_current_workspace_dir(None)


def _provider(
    provider_id: str,
    model_id: str,
    *,
    result=None,
    error: Exception | None = None,
):
    generate = AsyncMock(return_value=result)
    edit = AsyncMock(return_value=result)
    if error is not None:
        generate.side_effect = error
    return SimpleNamespace(
        id=provider_id,
        name=provider_id.title(),
        models=[ModelInfo(id=model_id, name=model_id.title())],
        extra_models=[],
        generate_image=generate,
        edit_image=edit,
    )


def _install_config(monkeypatch, models, *, timeout_seconds: float = 120) -> None:
    monkeypatch.setattr(
        image_generation,
        "load_config",
        lambda: SimpleNamespace(
            image_generation=ImageGenerationConfig(
                enabled=True,
                models=models,
                timeout_seconds=timeout_seconds,
            ),
        ),
    )


async def test_generate_image_fails_over_to_next_configured_model(
    monkeypatch,
) -> None:
    first = _provider(
        "first",
        "image-a",
        error=RuntimeError("temporary outage"),
    )
    second = _provider(
        "second",
        "image-b",
        result={
            "data": [
                {"url": "https://images.example/success.png"},
            ],
        },
    )
    providers = {"first": first, "second": second}
    manager = SimpleNamespace(
        get_provider=lambda provider_id: providers.get(provider_id),
        is_model_slot_available=lambda slot: slot.provider_id in providers,
    )
    monkeypatch.setattr(
        image_generation.ProviderManager,
        "get_instance",
        lambda: manager,
    )
    _install_config(
        monkeypatch,
        [
            ModelSlotConfig(provider_id="first", model="image-a"),
            ModelSlotConfig(provider_id="second", model="image-b"),
        ],
        timeout_seconds=33,
    )

    response = await image_generation.generate_image(
        "A moonlit lake",
        size="1536x1024",
        quality="high",
    )

    first.generate_image.assert_awaited_once()
    second.generate_image.assert_awaited_once_with(
        model_id="image-b",
        prompt="A moonlit lake",
        n=1,
        size="1536x1024",
        quality="high",
        response_format=None,
        timeout=33.0,
    )
    assert response.content[0]["type"] == "image"
    assert response.content[0]["image_url"].endswith("success.png")
    assert response.metadata["provider_id"] == "second"
    assert response.metadata["model"] == "image-b"
    assert response.metadata["attempts"] == 2
    assert response.metadata["operation"] == "generate"
    assert response.metadata["generation_id"].startswith("image-gen-")
    assert response.metadata["prompt_sha256"].startswith("sha256:")


async def test_generate_image_saves_base64_result_to_media_dir(
    tmp_path,
    monkeypatch,
) -> None:
    encoded = base64.b64encode(b"image-bytes").decode("ascii")
    provider = _provider(
        "provider",
        "image-model",
        result={"data": [{"b64_json": encoded}]},
    )
    manager = SimpleNamespace(
        get_provider=lambda _provider_id: provider,
        is_model_slot_available=lambda _slot: True,
    )
    monkeypatch.setattr(
        image_generation.ProviderManager,
        "get_instance",
        lambda: manager,
    )
    monkeypatch.setattr(image_generation, "DEFAULT_MEDIA_DIR", tmp_path)
    _install_config(
        monkeypatch,
        [ModelSlotConfig(provider_id="provider", model="image-model")],
    )

    response = await image_generation.generate_image("A paper-cut landscape")

    image_path = response.content[0]["image_url"]
    assert image_path.startswith(str(tmp_path / "generated_images"))
    assert (tmp_path / "generated_images").is_dir()
    assert response.content[0]["source"] == {
        "type": "url",
        "url": image_path,
    }
    assert Path(image_path).read_bytes() == b"image-bytes"


async def test_generate_image_stages_a_durable_copy_in_the_current_workspace(
    tmp_path,
    monkeypatch,
) -> None:
    encoded = base64.b64encode(b"workspace-image").decode("ascii")
    provider = _provider(
        "provider",
        "image-model",
        result={"data": [{"b64_json": encoded}]},
    )
    manager = SimpleNamespace(
        get_provider=lambda _provider_id: provider,
        is_model_slot_available=lambda _slot: True,
    )
    monkeypatch.setattr(
        image_generation.ProviderManager,
        "get_instance",
        lambda: manager,
    )
    working = tmp_path / "working"
    monkeypatch.setattr(image_generation, "WORKING_DIR", working)
    monkeypatch.setattr(
        image_generation,
        "DEFAULT_MEDIA_DIR",
        working / "media",
    )
    _install_config(
        monkeypatch,
        [ModelSlotConfig(provider_id="provider", model="image-model")],
    )
    workspace = working / "workspaces" / "default" / "topic-workspace"
    workspace.mkdir(parents=True)
    set_current_workspace_dir(workspace)

    response = await image_generation.generate_image("A desk lamp")

    metadata = response.content[0]["metadata"]
    workspace_path = Path(metadata["workspace_path"])
    managed_path = Path(metadata["managed_path"])
    assert workspace_path.parent == workspace / ".lantai" / "generated_images"
    assert workspace_path.read_bytes() == b"workspace-image"
    assert managed_path.read_bytes() == b"workspace-image"
    assert str(workspace_path) in response.content[-1]["text"]
    assert response.metadata["artifacts"][0]["workspace_path"] == str(workspace_path)


async def test_external_workspace_keeps_only_retention_managed_image_copy(
    tmp_path,
    monkeypatch,
) -> None:
    encoded = base64.b64encode(b"external-workspace-image").decode("ascii")
    provider = _provider(
        "provider",
        "image-model",
        result={"data": [{"b64_json": encoded}]},
    )
    manager = SimpleNamespace(
        get_provider=lambda _provider_id: provider,
        is_model_slot_available=lambda _slot: True,
    )
    working = tmp_path / "working"
    external_workspace = tmp_path / "external-project"
    working.mkdir()
    external_workspace.mkdir()
    monkeypatch.setattr(
        image_generation.ProviderManager,
        "get_instance",
        lambda: manager,
    )
    monkeypatch.setattr(image_generation, "WORKING_DIR", working)
    monkeypatch.setattr(
        image_generation,
        "DEFAULT_MEDIA_DIR",
        working / "media",
    )
    _install_config(
        monkeypatch,
        [ModelSlotConfig(provider_id="provider", model="image-model")],
    )
    set_current_workspace_dir(external_workspace)

    response = await image_generation.generate_image("A safe external project")

    metadata = response.content[0]["metadata"]
    managed_path = Path(metadata["managed_path"])
    assert managed_path.is_relative_to(working / "media" / "generated_images")
    assert "workspace_path" not in metadata
    assert metadata["workspace_staging"]["status"] == "skipped"
    assert metadata["workspace_staging"]["reason"] == "workspace_outside_managed_root"
    assert not (external_workspace / ".lantai").exists()

    os.utime(managed_path, (0, 0))
    result = clean_storage_retention(dry_run=False, root=working)
    assert result["generated_images"]["files_removed"] == 1
    assert not managed_path.exists()


async def test_workspace_staging_rejects_symlink_escape(
    tmp_path,
    monkeypatch,
) -> None:
    encoded = base64.b64encode(b"symlink-escape-image").decode("ascii")
    provider = _provider(
        "provider",
        "image-model",
        result={"data": [{"b64_json": encoded}]},
    )
    manager = SimpleNamespace(
        get_provider=lambda _provider_id: provider,
        is_model_slot_available=lambda _slot: True,
    )
    working = tmp_path / "working"
    workspace = working / "workspaces" / "default" / "topic-workspace"
    outside = tmp_path / "outside"
    workspace.mkdir(parents=True)
    outside.mkdir()
    (workspace / ".lantai").symlink_to(outside, target_is_directory=True)
    monkeypatch.setattr(
        image_generation.ProviderManager,
        "get_instance",
        lambda: manager,
    )
    monkeypatch.setattr(image_generation, "WORKING_DIR", working)
    monkeypatch.setattr(
        image_generation,
        "DEFAULT_MEDIA_DIR",
        working / "media",
    )
    _install_config(
        monkeypatch,
        [ModelSlotConfig(provider_id="provider", model="image-model")],
    )
    set_current_workspace_dir(workspace)

    response = await image_generation.generate_image("A blocked symlink escape")

    metadata = response.content[0]["metadata"]
    assert Path(metadata["managed_path"]).is_file()
    assert "workspace_path" not in metadata
    assert metadata["workspace_staging"]["status"] == "failed"
    assert metadata["workspace_staging"]["reason"] == "unsafe_staging_path"
    assert not (outside / "generated_images").exists()


async def test_workspace_staging_failure_does_not_retry_image_provider(
    tmp_path,
    monkeypatch,
) -> None:
    encoded = base64.b64encode(b"copy-failure-image").decode("ascii")
    first = _provider(
        "first",
        "image-a",
        result={"data": [{"b64_json": encoded}]},
    )
    second = _provider(
        "second",
        "image-b",
        result={"data": [{"b64_json": encoded}]},
    )
    providers = {"first": first, "second": second}
    manager = SimpleNamespace(
        get_provider=lambda provider_id: providers.get(provider_id),
        is_model_slot_available=lambda _slot: True,
    )
    working = tmp_path / "working"
    workspace = working / "workspaces" / "default" / "topic-workspace"
    workspace.mkdir(parents=True)
    monkeypatch.setattr(
        image_generation.ProviderManager,
        "get_instance",
        lambda: manager,
    )
    monkeypatch.setattr(image_generation, "WORKING_DIR", working)
    monkeypatch.setattr(
        image_generation,
        "DEFAULT_MEDIA_DIR",
        working / "media",
    )
    monkeypatch.setattr(
        image_generation.shutil,
        "copy2",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError("disk full")),
    )
    _install_config(
        monkeypatch,
        [
            ModelSlotConfig(provider_id="first", model="image-a"),
            ModelSlotConfig(provider_id="second", model="image-b"),
        ],
    )
    set_current_workspace_dir(workspace)

    response = await image_generation.generate_image("A recoverable copy failure")

    first.generate_image.assert_awaited_once()
    second.generate_image.assert_not_awaited()
    metadata = response.content[0]["metadata"]
    assert Path(metadata["managed_path"]).is_file()
    assert "workspace_path" not in metadata
    assert metadata["workspace_staging"]["status"] == "failed"
    assert metadata["workspace_staging"]["reason"] == "copy_failed"
    assert "工作区副本未写入" in response.content[-1]["text"]


async def test_generate_image_caches_remote_result_as_durable_local_asset(
    tmp_path,
    monkeypatch,
) -> None:
    signed_url = "https://images.example/generated.jpeg?signature=temporary-secret"
    provider = _provider(
        "provider",
        "image-model",
        result={"data": [{"url": signed_url}]},
    )
    manager = SimpleNamespace(
        get_provider=lambda _provider_id: provider,
        is_model_slot_available=lambda _slot: True,
    )
    downloaded: list[str] = []

    async def fake_download(url: str, filename=None, download_dir: str = "") -> str:
        downloaded.append(url)
        path = Path(download_dir) / str(filename)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"remote-image")
        return str(path)

    monkeypatch.setattr(
        image_generation.ProviderManager,
        "get_instance",
        lambda: manager,
    )
    monkeypatch.setattr(image_generation, "DEFAULT_MEDIA_DIR", tmp_path)
    monkeypatch.setattr(
        image_generation,
        "download_file_from_url",
        fake_download,
        raising=False,
    )
    _install_config(
        monkeypatch,
        [ModelSlotConfig(provider_id="provider", model="image-model")],
    )

    response = await image_generation.generate_image("A durable result")

    image_path = response.content[0]["image_url"]
    assert downloaded == [signed_url]
    assert image_path.startswith(str(tmp_path / "generated_images"))
    assert Path(image_path).read_bytes() == b"remote-image"
    assert response.content[0]["metadata"]["durable"] is True
    assert response.metadata["artifacts"][0]["url"] == image_path
    assert signed_url not in str(response)


async def test_generate_image_reports_provider_adjusted_size(monkeypatch) -> None:
    provider = _provider(
        "provider",
        "image-model",
        result={
            "data": [{"url": "https://images.example/adjusted.png"}],
            "requested_size": "1920x1080",
            "effective_size": "2560x1440",
        },
    )
    manager = SimpleNamespace(
        get_provider=lambda _provider_id: provider,
        is_model_slot_available=lambda _slot: True,
    )
    monkeypatch.setattr(
        image_generation.ProviderManager,
        "get_instance",
        lambda: manager,
    )
    _install_config(
        monkeypatch,
        [ModelSlotConfig(provider_id="provider", model="image-model")],
    )

    response = await image_generation.generate_image(
        "A cinematic landscape",
        size="1920x1080",
    )

    assert "尺寸：2560x1440（由 1920x1080 自动适配）" in response.content[-1]["text"]
    assert response.metadata["requested_size"] == "1920x1080"
    assert response.metadata["effective_size"] == "2560x1440"


async def test_generate_image_normalizes_aspect_ratio_before_provider_call(
    monkeypatch,
) -> None:
    encoded = base64.b64encode(b"image-bytes").decode("ascii")
    provider = _provider(
        "provider",
        "image-model",
        result={"data": [{"b64_json": encoded}]},
    )
    manager = SimpleNamespace(
        get_provider=lambda _provider_id: provider,
        is_model_slot_available=lambda _slot: True,
    )
    monkeypatch.setattr(
        image_generation.ProviderManager,
        "get_instance",
        lambda: manager,
    )
    _install_config(
        monkeypatch,
        [ModelSlotConfig(provider_id="provider", model="image-model")],
    )

    response = await image_generation.generate_image(
        "A cinematic landscape",
        size="16:9",
    )

    assert provider.generate_image.await_args.kwargs["size"] == "1920x1080"
    assert response.metadata["requested_size_input"] == "16:9"
    assert response.metadata["requested_size"] == "1920x1080"


async def test_generate_image_edits_from_local_references_with_version_provenance(
    tmp_path,
    monkeypatch,
) -> None:
    reference = tmp_path / "reference.png"
    reference.write_bytes(b"reference")
    provider = _provider(
        "provider",
        "image-model",
        result={"data": [{"url": "https://images.example/edited.png"}]},
    )
    manager = SimpleNamespace(
        get_provider=lambda _provider_id: provider,
        is_model_slot_available=lambda _slot: True,
    )
    monkeypatch.setattr(
        image_generation.ProviderManager,
        "get_instance",
        lambda: manager,
    )
    _install_config(
        monkeypatch,
        [ModelSlotConfig(provider_id="provider", model="image-model")],
    )

    response = await image_generation.generate_image(
        "保留构图，改成水彩风格",
        reference_images=[str(reference)],
        parent_artifact_id="artifact-original",
    )

    provider.generate_image.assert_not_awaited()
    provider.edit_image.assert_awaited_once_with(
        model_id="image-model",
        prompt="保留构图，改成水彩风格",
        image_paths=[str(reference.resolve())],
        n=1,
        size="1024x1024",
        quality="auto",
        response_format=None,
        timeout=120.0,
    )
    assert response.metadata["operation"] == "edit"
    assert response.metadata["parent_artifact_id"] == "artifact-original"
    assert response.metadata["reference_images"] == [str(reference.resolve())]
    assert response.content[0]["metadata"]["parent_artifact_id"] == (
        "artifact-original"
    )
    assert response.content[0]["metadata"]["operation"] == "edit"


async def test_generate_image_reports_all_model_failures(monkeypatch) -> None:
    first = _provider("first", "image-a", error=RuntimeError("offline"))
    second = _provider("second", "image-b", result={"data": []})
    providers = {"first": first, "second": second}
    manager = SimpleNamespace(
        get_provider=lambda provider_id: providers.get(provider_id),
        is_model_slot_available=lambda _slot: True,
    )
    monkeypatch.setattr(
        image_generation.ProviderManager,
        "get_instance",
        lambda: manager,
    )
    _install_config(
        monkeypatch,
        [
            ModelSlotConfig(provider_id="first", model="image-a"),
            ModelSlotConfig(provider_id="second", model="image-b"),
        ],
    )

    with pytest.raises(RuntimeError, match="All configured image models failed"):
        await image_generation.generate_image("A test image")


async def test_generate_image_stops_failover_for_tokendance_recovery(
    monkeypatch,
) -> None:
    monkeypatch.setattr(
        image_generation,
        "is_watcha_account_connected",
        lambda: True,
    )

    class RecoveryError(Exception):
        response = SimpleNamespace(
            headers={"TokenDance-Recovery-Action": "top_up_balance"},
        )

    first = _provider("tokendance", "image-a", error=RecoveryError("hidden"))
    second = _provider("second", "image-b", result={"data": []})
    providers = {"tokendance": first, "second": second}
    manager = SimpleNamespace(
        get_provider=lambda provider_id: providers.get(provider_id),
        is_model_slot_available=lambda _slot: True,
    )
    monkeypatch.setattr(
        image_generation.ProviderManager,
        "get_instance",
        lambda: manager,
    )
    _install_config(
        monkeypatch,
        [
            ModelSlotConfig(provider_id="tokendance", model="image-a"),
            ModelSlotConfig(provider_id="second", model="image-b"),
        ],
    )

    with pytest.raises(TokenDanceRecoveryException) as exc_info:
        await image_generation.generate_image("A test image")

    assert exc_info.value.code == "TOKENDANCE_TOP_UP_REQUIRED"
    second.generate_image.assert_not_awaited()


async def test_generate_image_requires_explicit_configuration(
    monkeypatch,
) -> None:
    monkeypatch.setattr(
        image_generation,
        "load_config",
        lambda: SimpleNamespace(
            image_generation=ImageGenerationConfig(),
        ),
    )

    with pytest.raises(RuntimeError, match="not configured"):
        await image_generation.generate_image("A test image")


async def test_logged_out_tokendance_falls_through_to_user_image_api(
    monkeypatch,
) -> None:
    tokendance = _provider(
        "tokendance",
        "seedream",
        result={"data": [{"url": "https://images.example/blocked.png"}]},
    )
    user_provider = _provider(
        "my-api",
        "my-image",
        result={"data": [{"url": "https://images.example/user.png"}]},
    )
    providers = {"tokendance": tokendance, "my-api": user_provider}
    monkeypatch.setattr(
        image_generation.ProviderManager,
        "get_instance",
        lambda: SimpleNamespace(
            get_provider=lambda provider_id: providers.get(provider_id),
            is_model_slot_available=lambda _slot: True,
        ),
    )
    monkeypatch.setattr(
        image_generation,
        "is_watcha_account_connected",
        lambda: False,
    )
    _install_config(
        monkeypatch,
        [
            ModelSlotConfig(provider_id="tokendance", model="seedream"),
            ModelSlotConfig(provider_id="my-api", model="my-image"),
        ],
    )

    response = await image_generation.generate_image("A test image")

    tokendance.generate_image.assert_not_awaited()
    user_provider.generate_image.assert_awaited_once()
    assert response.metadata["provider_id"] == "my-api"
