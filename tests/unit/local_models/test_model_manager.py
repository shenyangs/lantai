# -*- coding: utf-8 -*-
# pylint: disable=protected-access

from __future__ import annotations

import sys
from pathlib import Path

import pytest

from lantai.local_models.download_manager import (
    DownloadTaskResult,
    DownloadTaskStatus,
)
from lantai.local_models.model_manager import ModelManager, DownloadSource

LEGACY_HF_ORG = "Agent" + "Scope"


class _FakeController:
    def __init__(self) -> None:
        self.started_spec = None
        self.cancel_called = False
        self.active = False
        self.snapshot_value = {
            "status": "idle",
            "model_name": None,
            "downloaded_bytes": 0,
            "total_bytes": None,
            "speed_bytes_per_sec": 0.0,
            "source": None,
            "error": None,
            "local_path": None,
        }

    def start(self, spec) -> None:
        self.started_spec = spec
        self.active = True

    def cancel(self) -> None:
        self.cancel_called = True
        self.active = False

    def snapshot(self) -> dict:
        return self.snapshot_value

    def is_active(self) -> bool:
        return self.active


def test_start_download_uses_reachable_source(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    downloader = ModelManager()
    controller = _FakeController()
    downloader.__dict__["_download_controller"] = controller
    downloader.__dict__["_download_tmp_dir"] = tmp_path / "tmp"
    target_dir = tmp_path / "resolved-model-dir"

    monkeypatch.setattr(
        downloader,
        "get_model_dir",
        lambda repo_id: target_dir,
    )
    monkeypatch.setattr(
        downloader,
        "_resolve_download_source",
        lambda: DownloadSource.MODELSCOPE,
    )
    monkeypatch.setattr(
        downloader,
        "_estimate_download_size",
        lambda **kwargs: 100,
    )
    monkeypatch.setattr(
        downloader,
        "_check_gguf_exists",
        lambda **kwargs: (True, ""),
    )

    downloader.start_download("Qwen/Qwen2-0.5B-Instruct-GGUF")

    assert controller.started_spec is not None
    assert controller.started_spec.command == [
        "lantai-model-download",
        "Qwen/Qwen2-0.5B-Instruct-GGUF",
        "modelscope",
    ]
    assert (
        controller.started_spec.model_name == "Qwen/Qwen2-0.5B-Instruct-GGUF"
    )
    assert controller.started_spec.source == "modelscope"
    assert controller.started_spec.total_bytes == 100
    assert controller.started_spec.task.payload == {
        "repo_id": "Qwen/Qwen2-0.5B-Instruct-GGUF",
        "source": "modelscope",
        "staging_dir": str(
            (tmp_path / "tmp").joinpath(
                Path(
                    controller.started_spec.task.payload["staging_dir"],
                ).name,
            ),
        ),
    }
    progress = controller.started_spec.task.probe_progress()
    assert progress is not None
    assert (
        Path(controller.started_spec.task.payload["staging_dir"]).parent
        == tmp_path / "tmp"
    )
    assert progress.total_bytes == 100
    assert progress.model_name == "Qwen/Qwen2-0.5B-Instruct-GGUF"
    assert progress.source == "modelscope"


def test_download_model_is_wrapper(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    downloader = ModelManager()
    calls: list[tuple[str, DownloadSource | None]] = []

    monkeypatch.setattr(
        downloader,
        "start_download",
        lambda model_id, source=None: calls.append((model_id, source)),
    )

    downloader.download_model(
        "Qwen/Qwen2-0.5B-Instruct-GGUF",
        source=DownloadSource.HUGGINGFACE,
    )

    assert calls == [
        (
            "Qwen/Qwen2-0.5B-Instruct-GGUF",
            DownloadSource.HUGGINGFACE,
        ),
    ]


def test_get_download_progress_returns_idle_by_default() -> None:
    downloader = ModelManager()

    assert downloader.get_download_progress() == {
        "status": "idle",
        "model_name": None,
        "downloaded_bytes": 0,
        "total_bytes": None,
        "speed_bytes_per_sec": 0.0,
        "source": None,
        "error": None,
        "local_path": None,
    }


def test_download_model_rejects_repo_without_gguf(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    downloader = ModelManager()
    controller = _FakeController()
    downloader.__dict__["_download_controller"] = controller

    monkeypatch.setattr(
        downloader,
        "get_model_dir",
        lambda repo_id: tmp_path / repo_id,
    )
    monkeypatch.setattr(
        downloader,
        "_resolve_download_source",
        lambda: DownloadSource.MODELSCOPE,
    )
    monkeypatch.setattr(
        downloader,
        "_estimate_download_size",
        lambda **kwargs: 100,
    )
    monkeypatch.setattr(
        downloader,
        "_check_gguf_exists",
        lambda **kwargs: (
            False,
            (
                "Repository demo/no-gguf does not contain any .gguf "
                "files on ModelScope."
            ),
        ),
    )

    with pytest.raises(
        ValueError,
        match="does not contain any .gguf files",
    ):
        downloader.start_download("demo/no-gguf")

    assert controller.started_spec is None


def test_cancel_download_delegates_to_controller() -> None:
    downloader = ModelManager()
    controller = _FakeController()
    downloader.__dict__["_download_controller"] = controller

    downloader.cancel_download()

    assert controller.cancel_called is True


def test_start_download_uses_explicit_source_without_probe(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    downloader = ModelManager()
    controller = _FakeController()
    downloader.__dict__["_download_controller"] = controller
    downloader.__dict__["_download_tmp_dir"] = tmp_path / "tmp"
    target_dir = tmp_path / "resolved-model-dir"

    monkeypatch.setattr(
        downloader,
        "get_model_dir",
        lambda repo_id: target_dir,
    )

    def _unexpected_probe() -> DownloadSource:
        raise AssertionError("source probing should be skipped")

    monkeypatch.setattr(
        downloader,
        "_resolve_download_source",
        _unexpected_probe,
    )
    monkeypatch.setattr(
        downloader,
        "_estimate_download_size",
        lambda **kwargs: 100,
    )
    monkeypatch.setattr(
        downloader,
        "_check_gguf_exists",
        lambda **kwargs: (True, ""),
    )

    downloader.start_download(
        "Qwen/Qwen2-0.5B-Instruct-GGUF",
        source=DownloadSource.HUGGINGFACE,
    )

    assert controller.started_spec is not None
    assert controller.started_spec.command == [
        "lantai-model-download",
        "Qwen/Qwen2-0.5B-Instruct-GGUF",
        "huggingface",
    ]
    assert controller.started_spec.source == "huggingface"


def test_finalize_download_result_promotes_staging_dir(
    tmp_path: Path,
) -> None:
    downloader = ModelManager()
    downloader._model_dir = tmp_path / "models"
    staging_dir = tmp_path / "staging"
    final_dir = downloader._model_dir / "vendor" / "final"
    staging_dir.mkdir()
    (staging_dir / "model.gguf").write_bytes(b"123")

    result, downloaded_bytes = downloader._finalize_download_result(
        DownloadTaskResult(
            status=DownloadTaskStatus.COMPLETED,
            local_path=str(staging_dir),
        ),
        staging_dir=staging_dir,
        final_dir=final_dir,
    )

    assert result.status == DownloadTaskStatus.COMPLETED
    assert result.local_path == str(final_dir)
    assert downloaded_bytes == 3
    assert not staging_dir.exists()
    assert (final_dir / "model.gguf").exists()


def test_promote_download_rejects_symlink_parent_without_writing_outside_root(
    tmp_path: Path,
) -> None:
    downloader = ModelManager()
    downloader._model_dir = tmp_path / "models"
    downloader._model_dir.mkdir()
    staging_dir = tmp_path / "staging"
    staging_dir.mkdir()
    (staging_dir / "model.gguf").write_bytes(b"download")
    outside = tmp_path / "outside"
    outside.mkdir()
    marker = outside / "keep.txt"
    marker.write_bytes(b"keep")
    (downloader._model_dir / "vendor").symlink_to(
        outside, target_is_directory=True
    )

    with pytest.raises(ValueError, match="Invalid local model ID"):
        downloader._promote_staging_directory(
            staging_dir=staging_dir,
            final_dir=downloader._model_dir / "vendor" / "model",
            local_path=staging_dir,
        )

    assert marker.read_bytes() == b"keep"
    assert not (outside / "model").exists()
    assert (staging_dir / "model.gguf").read_bytes() == b"download"


def test_get_model_dir_preserves_repo_id_path() -> None:
    downloader = ModelManager()

    model_dir = downloader.get_model_dir("Qwen/Qwen3-0.6B-GGUF")

    assert model_dir.parts[-2:] == ("Qwen", "Qwen3-0.6B-GGUF")


@pytest.mark.parametrize(
    ("memory_gb", "expected_ids"),
    [
        (
            8,
            [
                f"{LEGACY_HF_ORG}/Lantai-Flash-2B-Q4_K_M",
                f"{LEGACY_HF_ORG}/Lantai-Flash-2B-Q8_0",
            ],
        ),
        (
            16,
            [
                f"{LEGACY_HF_ORG}/Lantai-Flash-4B-Q4_K_M",
                f"{LEGACY_HF_ORG}/Lantai-Flash-4B-Q8_0",
            ],
        ),
        (
            32,
            [
                f"{LEGACY_HF_ORG}/Lantai-Flash-9B-Q4_K_M",
                f"{LEGACY_HF_ORG}/Lantai-Flash-9B-Q8_0",
            ],
        ),
    ],
)
def test_recommended_lantai_flash_ids_are_stable(
    monkeypatch: pytest.MonkeyPatch,
    memory_gb: float,
    expected_ids: list[str],
) -> None:
    downloader = ModelManager()
    monkeypatch.setattr(
        downloader,
        "_detect_available_memory_gb",
        lambda: memory_gb,
    )
    monkeypatch.setattr(downloader, "is_downloaded", lambda _model_id: False)

    models = downloader.get_recommended_models()

    assert [model.id for model in models] == expected_ids


def test_list_and_remove_downloaded_models_with_repo_id_layout(
    tmp_path: Path,
) -> None:
    downloader = ModelManager()
    downloader.__dict__["_model_dir"] = tmp_path / "models"

    repo_dir = downloader.get_model_dir("Qwen/Qwen3-0.6B-GGUF")
    repo_dir.mkdir(parents=True)
    (repo_dir / "model.gguf").write_bytes(b"123")
    (repo_dir / "README.md").write_text("demo", encoding="utf-8")

    models = downloader.list_downloaded_models()

    assert len(models) == 1
    assert models[0].id == "Qwen/Qwen3-0.6B-GGUF"
    assert models[0].name == "Qwen/Qwen3-0.6B-GGUF"

    downloader.remove_downloaded_model("Qwen/Qwen3-0.6B-GGUF")

    assert not repo_dir.exists()
    assert not (tmp_path / "models" / "Qwen").exists()


@pytest.mark.parametrize(
    "model_id",
    ["../victim", "namespace/../../victim", "..", ".", "", "/victim"],
)
def test_remove_downloaded_model_rejects_path_ids_outside_model_root(
    tmp_path: Path,
    model_id: str,
) -> None:
    manager = ModelManager()
    manager._model_dir = tmp_path / "models"
    manager._model_dir.mkdir()
    victim = tmp_path / "victim"
    victim.mkdir()
    marker = victim / "keep.txt"
    marker.write_text("keep", encoding="utf-8")

    with pytest.raises(ValueError, match="Invalid local model ID"):
        manager.remove_downloaded_model(model_id)

    assert marker.read_text(encoding="utf-8") == "keep"
    assert manager._model_dir.is_dir()


def test_remove_downloaded_model_rejects_symlink_parent(
    tmp_path: Path,
) -> None:
    manager = ModelManager()
    manager._model_dir = tmp_path / "models"
    manager._model_dir.mkdir()
    outside = tmp_path / "outside"
    victim = outside / "victim"
    victim.mkdir(parents=True)
    marker = victim / "keep.txt"
    marker.write_text("keep", encoding="utf-8")
    (manager._model_dir / "escape").symlink_to(outside, target_is_directory=True)

    with pytest.raises(ValueError, match="Invalid local model ID"):
        manager.remove_downloaded_model("escape/victim")

    assert marker.read_text(encoding="utf-8") == "keep"


def test_remove_downloaded_model_fails_closed_without_safe_file_operations(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manager = ModelManager()
    manager._model_dir = tmp_path / "models"
    model = manager.get_model_dir("vendor/model")
    model.mkdir(parents=True)
    marker = model / "model.gguf"
    marker.write_bytes(b"keep")
    monkeypatch.setattr(manager, "_supports_safe_model_dir_fd", lambda: False)

    with pytest.raises(RuntimeError, match="Secure local model deletion"):
        manager.remove_downloaded_model("vendor/model")

    assert marker.read_bytes() == b"keep"


def test_remove_downloaded_model_does_not_follow_descendant_symlinks(
    tmp_path: Path,
) -> None:
    manager = ModelManager()
    manager._model_dir = tmp_path / "models"
    model = manager.get_model_dir("vendor/model")
    model.mkdir(parents=True)
    (model / "model.gguf").write_bytes(b"model")
    outside = tmp_path / "outside"
    outside.mkdir()
    marker = outside / "keep.txt"
    marker.write_bytes(b"keep")
    (model / "outside-link").symlink_to(outside, target_is_directory=True)

    manager.remove_downloaded_model("vendor/model")

    assert marker.read_bytes() == b"keep"
    assert not model.exists()


def test_remove_downloaded_model_rejects_namespace_containing_other_models(
    tmp_path: Path,
) -> None:
    manager = ModelManager()
    manager._model_dir = tmp_path / "models"
    model = manager.get_model_dir("vendor/model")
    model.mkdir(parents=True)
    marker = model / "model.gguf"
    marker.write_bytes(b"keep")

    with pytest.raises(ValueError, match="not a downloaded model"):
        manager.remove_downloaded_model("vendor")

    assert marker.read_bytes() == b"keep"


def test_start_download_rejects_path_traversal_before_starting_worker(
    tmp_path: Path,
) -> None:
    manager = ModelManager()
    manager._model_dir = tmp_path / "models"
    manager._download_tmp_dir = tmp_path / "tmp"
    controller = _FakeController()
    manager._download_controller = controller

    with pytest.raises(ValueError, match="Invalid local model ID"):
        manager.start_download("../victim")

    assert controller.started_spec is None
    assert not (tmp_path / "victim").exists()


def test_list_downloaded_models_ignores_temporary_download_dirs(
    tmp_path: Path,
) -> None:
    downloader = ModelManager()
    downloader.__dict__["_model_dir"] = tmp_path / "models"

    completed_dir = downloader.get_model_dir("Qwen/Qwen3-0.6B-GGUF")
    completed_dir.mkdir(parents=True)
    (completed_dir / "model.gguf").write_bytes(b"123")

    staging_dir = (
        completed_dir.parent / ".Qwen3-0.6B-GGUF.1234abcd.downloading"
    )
    staging_dir.mkdir(parents=True)
    (staging_dir / "partial.gguf").write_bytes(b"12")

    models = downloader.list_downloaded_models()

    assert [model.id for model in models] == ["Qwen/Qwen3-0.6B-GGUF"]


def test_download_worker_sanitizes_standard_streams(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    queue_messages: list[dict[str, object | dict[str, object]]] = []
    calls: list[str] = []

    class _Queue:
        def put(self, item):
            queue_messages.append(item)

    monkeypatch.setattr(
        "lantai.local_models.model_manager.ensure_standard_streams",
        lambda: calls.append("sanitized"),
    )
    monkeypatch.setattr(
        ModelManager,
        "_download_to_directory",
        staticmethod(lambda **kwargs: str(tmp_path / "downloaded")),
    )

    getattr(ModelManager, "_download_worker")(
        {
            "repo_id": f"{LEGACY_HF_ORG}/demo",
            "source": "modelscope",
            "staging_dir": str(tmp_path / "staging"),
        },
        _Queue(),
    )

    assert calls == ["sanitized"]
    assert queue_messages[0]["type"] == "result"
    payload = queue_messages[0]["payload"]
    assert isinstance(payload, dict)
    assert payload["status"] == "completed"


def test_download_from_modelscope_redirects_broken_standard_streams(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    class _BrokenStream:
        encoding = "utf-8"

        def write(self, _text: str) -> int:
            raise OSError(22, "Invalid argument")

        def flush(self) -> None:
            raise OSError(22, "Invalid argument")

    captured: dict[str, object] = {}

    def _fake_snapshot_download(*, model_id: str, local_dir: str) -> str:
        captured["model_id"] = model_id
        captured["local_dir"] = local_dir
        captured["stdout"] = sys.stdout
        captured["stderr"] = sys.stderr
        sys.stderr.write("")
        sys.stderr.flush()
        return local_dir

    monkeypatch.setattr(sys, "stdout", _BrokenStream())
    monkeypatch.setattr(sys, "stderr", _BrokenStream())
    monkeypatch.setattr(
        ModelManager,
        "_get_modelscope_snapshot_download",
        staticmethod(lambda: _fake_snapshot_download),
    )

    result = ModelManager._download_from_modelscope(
        repo_id=f"{LEGACY_HF_ORG}/demo",
        local_dir=tmp_path / "download",
    )

    assert result == str(tmp_path / "download")
    assert captured["model_id"] == f"{LEGACY_HF_ORG}/demo"
    assert captured["local_dir"] == str(tmp_path / "download")
    assert captured["stdout"] is not sys.stdout
    assert captured["stderr"] is not sys.stderr
