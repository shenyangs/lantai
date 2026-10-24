# -*- coding: utf-8 -*-
"""Class-based local model downloader."""

from __future__ import annotations

import contextlib
import errno
import importlib
import logging
import multiprocessing as mp
import os
import shutil
import stat
import uuid
from pathlib import Path
from queue import Empty
from typing import Any, Optional
from enum import Enum
from contextlib import ExitStack

import traceback
import httpx
from pydantic import Field

from .download_manager import (
    DownloadProgressTracker,
    DownloadProgressUpdate,
    ProcessDownloadTask,
    DownloadTaskResult,
    DownloadTaskStatus,
    ProcessDownloadController,
    ProcessDownloadTaskSpec,
)
from ..utils import system_info
from ..utils.stdio import ensure_standard_streams
from ..providers.provider import ModelInfo
from ..constant import DEFAULT_LOCAL_PROVIDER_DIR

logger = logging.getLogger(__name__)


_LANTAI_FLASH_HF_ORG = "Agent" + "Scope"


def _lantai_flash_repo_id(model_name: str) -> str:
    return f"{_LANTAI_FLASH_HF_ORG}/{model_name}"


class DownloadSource(str, Enum):
    HUGGINGFACE = "huggingface"
    MODELSCOPE = "modelscope"
    # First try Hugging Face, then fall back to ModelScope if unreachable
    AUTO = "auto"


class LocalModelInfo(ModelInfo):
    """Metadata for a local model"""

    size_bytes: int = Field(
        ...,
        description="Model size in bytes, if known",
    )
    downloaded: bool = Field(
        default=False,
        description="Whether the model is fully downloaded and ready to use",
    )
    source: DownloadSource = Field(
        default=DownloadSource.AUTO,
        description="Preferred source to download the model from",
    )


class InvalidModelIdError(ValueError):
    """The supplied repository id cannot safely address a local model."""


class ModelManager:
    """A manager for downloading local models with progress tracking."""

    def __init__(
        self,
    ) -> None:
        self._context = mp.get_context("spawn")
        self._model_dir = DEFAULT_LOCAL_PROVIDER_DIR / "models"
        self._download_tmp_dir = DEFAULT_LOCAL_PROVIDER_DIR / "tmp"
        self._progress = DownloadProgressTracker()
        self._download_controller = ProcessDownloadController(
            context=self._context,
            progress=self._progress,
        )

    def get_recommended_models(self) -> list[LocalModelInfo]:
        """Recommend model names from the current machine capacity."""
        memory_gb = self._detect_available_memory_gb()

        if memory_gb < 4:
            return []

        if memory_gb <= 8:
            models = [
                LocalModelInfo(
                    id=_lantai_flash_repo_id("Lantai-Flash-2B-Q4_K_M"),
                    name="Lantai-Flash-2B-Q4_K_M",
                    size_bytes=1560460768,
                    source=DownloadSource.MODELSCOPE,
                ),
                LocalModelInfo(
                    id=_lantai_flash_repo_id("Lantai-Flash-2B-Q8_0"),
                    name="Lantai-Flash-2B-Q8_0",
                    size_bytes=2552356320,
                    source=DownloadSource.MODELSCOPE,
                ),
            ]
        elif memory_gb <= 16:
            models = [
                LocalModelInfo(
                    id=_lantai_flash_repo_id("Lantai-Flash-4B-Q4_K_M"),
                    name="Lantai-Flash-4B-Q4_K_M",
                    size_bytes=3066384736,
                    source=DownloadSource.MODELSCOPE,
                ),
                LocalModelInfo(
                    id=_lantai_flash_repo_id("Lantai-Flash-4B-Q8_0"),
                    name="Lantai-Flash-4B-Q8_0",
                    size_bytes=5157833056,
                    source=DownloadSource.MODELSCOPE,
                ),
            ]
        else:
            models = [
                LocalModelInfo(
                    id=_lantai_flash_repo_id("Lantai-Flash-9B-Q4_K_M"),
                    name="Lantai-Flash-9B-Q4_K_M",
                    size_bytes=5476080128,
                    source=DownloadSource.MODELSCOPE,
                ),
                LocalModelInfo(
                    id=_lantai_flash_repo_id("Lantai-Flash-9B-Q8_0"),
                    name="Lantai-Flash-9B-Q8_0",
                    size_bytes=10590617600,
                    source=DownloadSource.MODELSCOPE,
                ),
            ]

        # check local download status for each recommended model
        for model in models:
            model.downloaded = self.is_downloaded(model.id)

        return models

    def _check_model_exists(self, model_id: str) -> bool:
        """Check if a model directory contains .gguf files."""
        local_path = self.get_model_dir(model_id)
        return local_path.exists() and any(local_path.glob("*.gguf"))

    @staticmethod
    def _model_id_parts(model_id: str) -> tuple[str, ...]:
        if (
            not isinstance(model_id, str)
            or not model_id
            or model_id.startswith("/")
            or "\\" in model_id
            or "\x00" in model_id
            or ":" in model_id
        ):
            raise InvalidModelIdError(f"Invalid local model ID: {model_id!r}")
        parts = tuple(model_id.split("/"))
        if any(part in ("", ".", "..") for part in parts):
            raise InvalidModelIdError(f"Invalid local model ID: {model_id!r}")
        return parts

    def get_model_dir(self, model_id: str) -> Path:
        """Get the expected local path for a given model id."""
        parts = self._model_id_parts(model_id)
        root = self._model_dir.expanduser().resolve()
        candidate = self._model_dir.joinpath(*parts)
        current = self._model_dir
        for part in parts:
            current = current / part
            if current.is_symlink():
                raise InvalidModelIdError(f"Invalid local model ID: {model_id!r}")
        if not candidate.resolve().is_relative_to(root):
            raise InvalidModelIdError(f"Invalid local model ID: {model_id!r}")
        return candidate

    def is_downloaded(self, model_id: str) -> bool:
        """Check if a model id is already downloaded."""
        return self._check_model_exists(model_id)

    def list_downloaded_models(self) -> list[LocalModelInfo]:
        """Return all downloaded local model repositories."""
        model_root = self._model_dir
        if not model_root.exists():
            return []

        models: list[LocalModelInfo] = []
        for entry in self._iter_downloaded_model_dirs():
            repo_id = self._infer_repo_id_from_path(entry)
            size_bytes = self._calculate_downloaded_size(entry)
            models.append(
                LocalModelInfo(
                    id=repo_id,
                    name=repo_id,
                    size_bytes=size_bytes,
                    downloaded=True,
                ),
            )

        return models

    def remove_downloaded_model(self, model_id: str) -> None:
        """Delete a downloaded local model by repo id or directory name."""
        parts = self._model_id_parts(model_id)
        model_path = self.get_model_dir(model_id)
        if not model_path.exists():
            raise ValueError(f"Downloaded local model not found: {model_id}")

        if self._supports_safe_model_dir_fd():
            with ExitStack() as stack:
                root_fd = os.open(
                    self._model_dir.expanduser(),
                    os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                )
                stack.callback(os.close, root_fd)
                fds = [root_fd]
                for part in parts[:-1]:
                    try:
                        fd = os.open(
                            part,
                            os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                            dir_fd=fds[-1],
                        )
                    except OSError as exc:
                        raise InvalidModelIdError(
                            f"Invalid local model ID: {model_id!r}"
                        ) from exc
                    stack.callback(os.close, fd)
                    fds.append(fd)

                name = parts[-1]
                try:
                    entry = os.stat(name, dir_fd=fds[-1], follow_symlinks=False)
                except FileNotFoundError as exc:
                    raise ValueError(
                        f"Downloaded local model not found: {model_id}"
                    ) from exc
                if stat.S_ISDIR(entry.st_mode):
                    child_fd = os.open(
                        name,
                        os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                        dir_fd=fds[-1],
                    )
                    try:
                        with os.scandir(child_fd) as children:
                            is_model = any(
                                child.name.lower().endswith(".gguf")
                                and child.is_file(follow_symlinks=False)
                                for child in children
                            )
                    finally:
                        os.close(child_fd)
                    if not is_model:
                        raise InvalidModelIdError(
                            f"Directory is not a downloaded model: {model_id!r}"
                        )
                    self._remove_tree_at(fds[-1], name)
                elif stat.S_ISREG(entry.st_mode):
                    os.unlink(name, dir_fd=fds[-1])
                else:
                    raise InvalidModelIdError(
                        f"Invalid local model ID: {model_id!r}"
                    )

                for index in range(len(fds) - 1, 0, -1):
                    try:
                        os.rmdir(parts[index - 1], dir_fd=fds[index - 1])
                    except OSError as exc:
                        if exc.errno == errno.ENOTEMPTY:
                            break
                        raise
            return

        raise RuntimeError(
            "Secure local model deletion is unavailable on this platform"
        )

    @staticmethod
    def _supports_safe_model_dir_fd() -> bool:
        return (
            hasattr(os, "O_NOFOLLOW")
            and hasattr(os, "O_DIRECTORY")
            and os.scandir in os.supports_fd
            and os.open in os.supports_dir_fd
            and os.mkdir in os.supports_dir_fd
            and os.stat in os.supports_dir_fd
            and os.rename in os.supports_dir_fd
            and os.unlink in os.supports_dir_fd
            and os.rmdir in os.supports_dir_fd
        )

    @classmethod
    def _remove_tree_at(cls, parent_fd: int, name: str) -> None:
        """Remove a directory relative to an open parent, without following links."""
        child_fd = os.open(
            name,
            os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
            dir_fd=parent_fd,
        )
        try:
            with os.scandir(child_fd) as entries:
                for entry in entries:
                    child = os.stat(
                        entry.name, dir_fd=child_fd, follow_symlinks=False
                    )
                    if stat.S_ISDIR(child.st_mode):
                        cls._remove_tree_at(child_fd, entry.name)
                    else:
                        os.unlink(entry.name, dir_fd=child_fd)
        finally:
            os.close(child_fd)
        os.rmdir(name, dir_fd=parent_fd)

    def start_download(
        self,
        model_id: str,
        source: DownloadSource | None = None,
    ) -> None:
        """Start downloading the selected model into the target directory."""
        logger.info("Starting download for [%s] %s", source, model_id)
        if self._is_download_active():
            raise RuntimeError("A model download is already in progress.")

        repo_id = model_id
        final_dir = self.get_model_dir(repo_id).expanduser()
        self._download_tmp_dir.mkdir(parents=True, exist_ok=True)
        resolved_source = source or self._resolve_download_source()
        total_bytes = self._estimate_download_size(
            repo_id=repo_id,
            source=resolved_source,
        )
        has_gguf, error_msg = self._check_gguf_exists(
            repo_id=repo_id,
            source=resolved_source,
        )
        if not has_gguf:
            raise ValueError(error_msg)

        task_id = uuid.uuid4().hex
        staging_dir = self._download_tmp_dir / task_id
        payload = {
            "repo_id": repo_id,
            "source": resolved_source.value,
            "staging_dir": str(staging_dir),
        }
        spec = ProcessDownloadTaskSpec(
            process_name=f"lantai-model-download-{task_id}",
            command=[
                "lantai-model-download",
                repo_id,
                resolved_source.value,
            ],
            task=ProcessDownloadTask(
                target=type(self)._download_worker,
                payload=payload,
                progress_probe=lambda: DownloadProgressUpdate(
                    downloaded_bytes=self._calculate_downloaded_size(
                        staging_dir,
                    ),
                    total_bytes=total_bytes,
                    model_name=repo_id,
                    source=resolved_source.value,
                ),
                finalize_result=lambda result: self._finalize_download_result(
                    result,
                    staging_dir=staging_dir,
                    final_dir=final_dir,
                ),
                cleanup=lambda: self._cleanup_path(staging_dir),
            ),
            model_name=repo_id,
            source=resolved_source.value,
            total_bytes=total_bytes,
        )
        self._download_controller.start(spec)

    def download_model(
        self,
        model_id: str,
        source: DownloadSource | None = None,
    ) -> None:
        self.start_download(model_id, source=source)

    def get_download_progress(self) -> dict[str, Any]:
        """Return the current download progress."""
        return self._download_controller.snapshot()

    def cancel_download(self) -> None:
        """Cancel the current download task."""
        self._download_controller.cancel()

    def _is_download_active(self) -> bool:
        """Return whether a download process is still active."""
        return self._download_controller.is_active()

    def _finalize_download_result(
        self,
        result: DownloadTaskResult,
        *,
        staging_dir: Path,
        final_dir: Path,
    ) -> tuple[DownloadTaskResult, int | None]:
        if result.status != DownloadTaskStatus.COMPLETED:
            return result, None
        local_path = self._promote_staging_directory(
            staging_dir=staging_dir,
            final_dir=final_dir,
            local_path=Path(result.local_path or staging_dir),
        )
        downloaded_bytes = self._calculate_downloaded_size(final_dir)
        return (
            DownloadTaskResult(
                status=result.status,
                local_path=str(local_path),
            ),
            downloaded_bytes,
        )

    def _resolve_download_source(self) -> DownloadSource:
        """Choose Hugging Face when reachable, otherwise use ModelScope."""
        if self._probe_huggingface():
            return DownloadSource.HUGGINGFACE
        return DownloadSource.MODELSCOPE

    def _estimate_download_size(
        self,
        repo_id: str,
        source: DownloadSource,
    ) -> Optional[int]:
        """Best-effort total byte estimation for progress."""
        if source == DownloadSource.HUGGINGFACE:
            return self._estimate_huggingface_size(
                repo_id=repo_id,
            )
        return self._estimate_modelscope_size(
            repo_id=repo_id,
        )

    def _check_gguf_exists(
        self,
        repo_id: str,
        source: DownloadSource,
    ) -> tuple[bool, str]:
        """Return whether the remote repository contains at least one GGUF."""
        if source == DownloadSource.HUGGINGFACE:
            return self._check_huggingface_gguf_exists(
                repo_id=repo_id,
            )
        return self._check_modelscope_gguf_exists(
            repo_id=repo_id,
        )

    @staticmethod
    def _download_worker(payload: dict[str, Any], queue: Any) -> None:
        """Run the blocking SDK download in a child process."""
        ensure_standard_streams()
        repo_id = payload["repo_id"]
        source = DownloadSource(payload["source"])
        staging_dir = Path(payload["staging_dir"]).expanduser().resolve()

        try:
            ModelManager._cleanup_path(staging_dir)
            staging_dir.mkdir(parents=True, exist_ok=True)
            local_path = ModelManager._download_to_directory(
                repo_id=repo_id,
                source=source,
                local_dir=staging_dir,
            )
        except Exception:
            queue.put(
                DownloadTaskResult(
                    status=DownloadTaskStatus.FAILED,
                    error="Download failed: " + traceback.format_exc(),
                ).to_message(),
            )
            logger.error(
                "Error when downloading model [%s]:\n%s",
                repo_id,
                traceback.format_exc(),
            )
            raise
        queue.put(
            DownloadTaskResult(
                status=DownloadTaskStatus.COMPLETED,
                local_path=str(Path(local_path).resolve()),
            ).to_message(),
        )

    @staticmethod
    def _download_to_directory(
        repo_id: str,
        source: DownloadSource,
        local_dir: Path,
    ) -> str:
        """Download a model into the target directory."""
        if source == DownloadSource.HUGGINGFACE:
            return ModelManager._download_from_huggingface(
                repo_id=repo_id,
                local_dir=local_dir,
            )
        return ModelManager._download_from_modelscope(
            repo_id=repo_id,
            local_dir=local_dir,
        )

    @staticmethod
    def _download_from_huggingface(
        repo_id: str,
        local_dir: Path,
    ) -> str:
        """Download a model repository from Hugging Face Hub."""
        try:
            from huggingface_hub import snapshot_download
        except ImportError as exc:
            raise ImportError(
                "huggingface_hub is required for Hugging Face downloads.",
            ) from exc

        return snapshot_download(
            repo_id=repo_id,
            local_dir=str(local_dir),
        )

    @staticmethod
    def _download_from_modelscope(
        repo_id: str,
        local_dir: Path,
    ) -> str:
        """Download a model repository from ModelScope."""
        with open(
            os.devnull,
            "a",
            encoding="utf-8",
            buffering=1,
            errors="replace",
        ) as sink_stream:
            with contextlib.redirect_stdout(
                sink_stream,
            ), contextlib.redirect_stderr(sink_stream):
                return ModelManager._get_modelscope_snapshot_download()(
                    model_id=repo_id,
                    local_dir=str(local_dir),
                )

    def _estimate_huggingface_size(
        self,
        repo_id: str,
    ) -> Optional[int]:
        """Estimate total download bytes from Hugging Face metadata."""
        try:
            from huggingface_hub import HfApi
        except ImportError:
            return None

        try:
            info = HfApi().repo_info(
                repo_id=repo_id,
                repo_type="model",
                files_metadata=True,
            )
        except (OSError, RuntimeError, TypeError, ValueError):
            return None

        siblings = getattr(info, "siblings", None) or []
        total = 0
        found = False
        for sibling in siblings:
            name = getattr(sibling, "rfilename", None)
            size = getattr(sibling, "size", None)
            if not isinstance(name, str) or not isinstance(size, int):
                continue
            if name.startswith("."):
                continue
            total += size
            found = True
        return total if found else None

    def _estimate_modelscope_size(
        self,
        repo_id: str,
    ) -> Optional[int]:
        """Estimate total download bytes from ModelScope metadata."""
        try:
            hub_api_module = importlib.import_module("modelscope.hub.api")
        except ImportError:
            return None

        try:
            files = hub_api_module.HubApi().get_model_files(
                repo_id,
            )
        except (OSError, RuntimeError, TypeError, ValueError):
            return None

        total = 0
        found = False
        for item in files:
            if not isinstance(item, dict):
                continue
            size = item.get("Size")
            if isinstance(size, int):
                total += size
                found = True
        return total if found else None

    def _check_huggingface_gguf_exists(self, repo_id: str) -> tuple[bool, str]:
        try:
            from huggingface_hub import HfApi
            from huggingface_hub.errors import RepositoryNotFoundError
        except ImportError:
            return False, "`huggingface_hub` is not installed"
        try:
            files = HfApi().list_repo_files(repo_id=repo_id)
            if any(f.endswith(".gguf") for f in files):
                return True, ""
            return (
                False,
                (
                    f"{repo_id} is not supported by Llama.cpp because it does "
                    "not contain any .gguf files."
                ),
            )
        except RepositoryNotFoundError:
            return False, f"Repository {repo_id} not found"
        except (OSError, RuntimeError, TypeError, ValueError) as e:
            return False, f"Error when checking repository: {e}"

    def _check_modelscope_gguf_exists(self, repo_id: str) -> tuple[bool, str]:
        try:
            hub_api_module = importlib.import_module("modelscope.hub.api")
        except ImportError:
            return False, "`modelscope` is not installed"
        try:
            files = hub_api_module.HubApi().get_model_files(repo_id)
        except (OSError, RuntimeError, TypeError, ValueError):
            return False, f"Failed to fetch info from {repo_id}"

        if any(
            isinstance(f, dict) and f.get("Name", "").endswith(".gguf")
            for f in files
        ):
            return True, ""
        return (
            False,
            (
                f"{repo_id} is not supported by Llama.cpp because it does "
                "not contain any .gguf files."
            ),
        )

    def _detect_available_memory_gb(self) -> float:
        """Prefer VRAM when available, otherwise use system memory."""
        gpu_memory_gb = system_info.get_vram_size_gb()
        if gpu_memory_gb > 0:
            return gpu_memory_gb
        return system_info.get_memory_size_gb()

    def _probe_huggingface(self) -> bool:
        """Return whether Hugging Face is reachable from this machine."""
        try:
            response = httpx.get(
                "https://huggingface.co",
                follow_redirects=True,
                timeout=5,
            )
        except httpx.HTTPError:
            return False
        return response.status_code < 500

    @staticmethod
    def _get_modelscope_snapshot_download() -> Any:
        """Return a compatible ModelScope snapshot downloader."""
        try:
            snapshot_module = importlib.import_module(
                "modelscope.hub.snapshot_download",
            )
            return snapshot_module.snapshot_download
        except ImportError:
            try:
                modelscope_module = importlib.import_module("modelscope")
                return modelscope_module.snapshot_download
            except ImportError as exc:
                raise ImportError(
                    "ModelScope snapshot download is required.",
                ) from exc

    @staticmethod
    def _drain_queue_message(queue: Any) -> Optional[dict[str, Any]]:
        """Return the latest worker message, if available."""
        if queue is None:
            return None

        latest = None
        while True:
            try:
                latest = queue.get_nowait()
            except Empty:
                return latest

    @staticmethod
    def _calculate_downloaded_size(path: Path) -> int:
        """Compute currently materialized bytes on disk."""
        if not path.exists():
            return 0
        if path.is_file():
            return path.stat().st_size
        return sum(
            entry.stat().st_size
            for entry in path.rglob("*")
            if entry.is_file()
        )

    def _promote_staging_directory(
        self,
        staging_dir: Path,
        final_dir: Path,
        local_path: Path,
    ) -> Path:
        """Move a finished staged download into the final directory."""
        try:
            model_id = final_dir.relative_to(self._model_dir.expanduser()).as_posix()
        except ValueError as exc:
            raise InvalidModelIdError(f"Invalid local model path: {final_dir}") from exc
        parts = self._model_id_parts(model_id)
        self.get_model_dir(model_id)
        if not self._supports_safe_model_dir_fd():
            raise RuntimeError(
                "Secure local model promotion is unavailable on this platform"
            )

        self._model_dir.mkdir(parents=True, exist_ok=True)
        with ExitStack() as stack:
            root_fd = os.open(
                self._model_dir.expanduser(),
                os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
            )
            stack.callback(os.close, root_fd)
            parent_fd = root_fd
            for part in parts[:-1]:
                try:
                    os.mkdir(part, dir_fd=parent_fd)
                except FileExistsError:
                    pass
                try:
                    parent_fd = os.open(
                        part,
                        os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                        dir_fd=parent_fd,
                    )
                except OSError as exc:
                    raise InvalidModelIdError(
                        f"Invalid local model ID: {model_id!r}"
                    ) from exc
                stack.callback(os.close, parent_fd)

            name = parts[-1]
            try:
                existing = os.stat(
                    name, dir_fd=parent_fd, follow_symlinks=False
                )
            except FileNotFoundError:
                pass
            else:
                if not stat.S_ISDIR(existing.st_mode):
                    raise InvalidModelIdError(
                        f"Invalid local model ID: {model_id!r}"
                    )
                self._remove_tree_at(parent_fd, name)
            os.rename(staging_dir, name, dst_dir_fd=parent_fd)

        if local_path == staging_dir:
            return final_dir
        return final_dir / local_path.relative_to(staging_dir)

    @staticmethod
    def _cleanup_path(path: Path) -> None:
        """Delete a file or directory if it exists."""
        if not path.exists():
            return
        if path.is_dir():
            shutil.rmtree(path, ignore_errors=True)
            return
        path.unlink(missing_ok=True)

    def _iter_downloaded_model_dirs(self) -> list[Path]:
        candidates: list[Path] = []
        entries = sorted(
            self._model_dir.rglob("*"),
            key=lambda item: item.parts,
        )
        for entry in entries:
            if not entry.is_dir():
                continue
            if self._is_temporary_download_dir(entry):
                continue
            if not any(entry.rglob("*.gguf")):
                continue
            if not self._looks_like_model_root(entry):
                continue
            candidates.append(entry)

        selected: list[Path] = []
        for candidate in sorted(candidates, key=lambda item: len(item.parts)):
            if any(candidate.is_relative_to(parent) for parent in selected):
                continue
            selected.append(candidate)
        return selected

    def _is_temporary_download_dir(self, path: Path) -> bool:
        relative_parts = path.relative_to(self._model_dir).parts
        return any(
            part.startswith(".") or part.endswith(".downloading")
            for part in relative_parts
        )

    def _looks_like_model_root(self, path: Path) -> bool:
        visible_children = [
            child for child in path.iterdir() if not child.name.startswith(".")
        ]
        return any(not child.is_dir() for child in visible_children)

    def _infer_repo_id_from_path(self, model_dir: Path) -> str:
        relative_path = model_dir.relative_to(self._model_dir)
        return "/".join(relative_path.parts)
