# -*- coding: utf-8 -*-
from __future__ import annotations

import atexit
import asyncio
import hashlib
import json
import os
import re
import logging
import multiprocessing as mp
import shutil
import socket
import uuid
from contextlib import contextmanager, suppress
from dataclasses import asdict
from pathlib import Path
from typing import Any, Iterator, Optional

import httpx
from pydantic import BaseModel

from lantai.constant import DEFAULT_LOCAL_PROVIDER_DIR
from lantai.providers.provider import ModelInfo

from .download_manager import (
    DownloadProgressUpdate,
    DownloadProgressTracker,
    ProcessDownloadTask,
    DownloadTaskResult,
    DownloadTaskStatus,
    ProcessDownloadController,
    ProcessDownloadTaskSpec,
)
from ..utils.command_runner import (
    ManagedProcess,
    run_command,
    run_command_async,
    shutdown_process,
    shutdown_process_sync,
    start_command_async,
)
from ..utils import system_info
from ..utils.stdio import ensure_standard_streams

from .llamacpp_assets import (
    OFFICIAL_RELEASE_BASE, TRUSTED_LLAMA_CPP_ASSETS, TrustedLlamaCppAsset,
)
from .llamacpp_install import (
    DirectoryAnchor, absolute_path, extract_archive, identity, read_receipt,
    validate_candidate_files, validate_receipt, write_notices, write_receipt,
)
from .llamacpp_notices import reviewed_license_manifest

logger = logging.getLogger(__name__)


class LlamaCppServerSetupResult(BaseModel):
    """Runtime information for a started llama.cpp server."""

    port: int
    model_info: ModelInfo


class LlamaCppBackend:
    """
    澜台 local model backend for managing llama.cpp server installation
    and setup.
    """

    _MIN_MACOS_VERSION = (13, 3)

    def __init__(self):
        self.os_name = self._resolve_os_name()
        self.arch = self._resolve_arch()
        self.cuda_version = self._resolve_cuda_version()
        self.backend = self._resolve_backend()
        self.target_dir = DEFAULT_LOCAL_PROVIDER_DIR / "bin"
        self.log_dir = DEFAULT_LOCAL_PROVIDER_DIR / "logs"
        self._context = mp.get_context("spawn")
        self._server_process: ManagedProcess | None = None
        self._server_log_task: asyncio.Task[None] | None = None
        self._server_port: int | None = None
        self._server_model_name: str | None = None
        self._server_transitioning = False
        self._progress = DownloadProgressTracker()
        self._download_controller = ProcessDownloadController(
            context=self._context,
            progress=self._progress,
        )
        atexit.register(self._shutdown_server_at_exit)

    # -----------------------------
    # Public APIs
    # -----------------------------
    @property
    def executable(self) -> Path:
        """The expected path of the llama.cpp server executable after download
        and extraction."""
        if self.os_name == "windows":
            return self.target_dir / "llama-server.exe"
        return self.target_dir / "llama-server"

    def check_llamacpp_installation(self) -> tuple[bool, str]:
        """Only a pinned, intact installation is eligible for execution."""
        try:
            target = absolute_path(self.target_dir)
            with DirectoryAnchor(target.parent) as parent:
                self._recover_installation(parent, target.name)
                parent.require_dir(target.name)
                with DirectoryAnchor(target) as directory:
                    self._validate_installed_directory(directory)
            return True, ""
        except Exception as exc:
            return False, f"llama.cpp is not installed or is unverified: {exc}"

    def _resolve_release_asset(self, base_url: str, tag: str) -> TrustedLlamaCppAsset:
        """Pure lookup of independently reviewed release bytes."""
        if base_url != OFFICIAL_RELEASE_BASE:
            raise ValueError("llama.cpp downloads require the reviewed official release source")
        for asset in TRUSTED_LLAMA_CPP_ASSETS:
            if (asset.tag, asset.os_name, asset.arch, asset.backend, asset.cuda_version) == (
                tag, self.os_name, self.arch, self.backend, self.cuda_version):
                return asset
        raise ValueError("No reviewed llama.cpp asset for this version and platform")

    def _validate_installed_directory(self, directory: DirectoryAnchor) -> TrustedLlamaCppAsset:
        receipt = read_receipt(directory)
        fields = receipt.get("asset")
        if not isinstance(fields, dict):
            raise ValueError("Installation receipt has no trusted source identity")
        asset = TrustedLlamaCppAsset(**fields)
        if (asset.os_name, asset.arch, asset.backend, asset.cuda_version) != (
            self.os_name, self.arch, self.backend, getattr(self, "cuda_version", None)):
            raise ValueError("Installation receipt is for a different platform")
        # Records cached here were admitted through the pure trust resolver;
        # receipt-provided digests never populate this cache.
        admitted = getattr(self, "_admitted_release_assets", {})
        trusted = admitted.get(asset.sha256)
        if trusted is None:
            trusted = self._resolve_release_asset(OFFICIAL_RELEASE_BASE, asset.tag)
        if trusted != asset:
            raise ValueError("Installation receipt does not match the reviewed release catalog")
        validate_receipt(directory, trusted, license_manifest=reviewed_license_manifest(trusted))
        return trusted

    def check_llamacpp_installability(self) -> tuple[bool, str]:
        """Check whether the current environment can install llama.cpp."""
        if self.os_name == "macos":
            supported, message = self._ensure_supported_macos_version()
            if not supported:
                return False, message

        try:
            self._build_filename("b0")
        except RuntimeError as exc:
            return False, str(exc)

        return True, ""

    def get_download_progress(self) -> dict[str, Any]:
        """Return the current llama.cpp download progress."""
        return self._progress.snapshot()

    def get_server_status(self) -> dict[str, Any]:
        """Return the current llama.cpp server status snapshot."""
        process = self._server_process
        running = bool(process is not None and process.returncode is None)
        return {
            "running": running,
            "port": self._server_port,
            "model_name": self._server_model_name,
            "pid": process.pid if running and process is not None else None,
        }

    def is_server_transitioning(self) -> bool:
        """Return whether the llama.cpp server is starting or stopping."""
        return self._server_transitioning

    def cancel_download(self) -> None:
        """Request cancellation of the current llama.cpp download."""
        self._download_controller.cancel()

    async def has_update(self, latest_version: str) -> bool:
        """Check if there is a newer version of llama.cpp available."""
        if not self.check_llamacpp_installation()[0]:
            return False
        try:
            return int(latest_version[1:]) > int(
                (await self.get_version()),
            )
        except Exception as exc:
            logger.warning(f"Failed to check for llama.cpp updates: {exc}")
            return True

    def download(
        self,
        base_url: str,
        tag: str,
        chunk_size: int = 1024 * 1024,
        timeout: int = 30,
    ) -> None:
        self.start_download(
            base_url=base_url,
            tag=tag,
            chunk_size=chunk_size,
            timeout=timeout,
        )

    def start_download(
        self,
        base_url: str,
        tag: str,
        chunk_size: int = 1024 * 1024,
        timeout: int = 30,
    ) -> None:
        """Start downloading and extracting the llama.cpp release package.

        Args:
          - chunk_size:
              Size of each read chunk
          - timeout:
              Network timeout in seconds

        Raises:
          - RuntimeError: another llama.cpp download is already in progress
          - ValueError: target_dir is an existing file path instead of a
            directory
        """
        installable, message = self.check_llamacpp_installability()
        if not installable:
            raise RuntimeError(message)
        dest_dir = self._resolve_dest_dir(self.target_dir)
        if self._is_download_active():
            raise RuntimeError(
                "A llama.cpp download is already in progress.",
            )

        asset = self._resolve_release_asset(base_url, tag)
        if not isinstance(asset, TrustedLlamaCppAsset):
            raise ValueError("Invalid trusted release record")
        self._admitted_release_assets = dict(getattr(self, "_admitted_release_assets", {}))
        self._admitted_release_assets[asset.sha256] = asset
        nonce = uuid.uuid4().hex
        staging_dir = dest_dir.parent / f".llamacpp-{nonce}"
        with DirectoryAnchor(dest_dir.parent, create=True) as parent:
            self._recover_installation(parent, dest_dir.name)
            ownership = parent.snapshot()
            # Capture the actual directory before the worker or cancellation
            # can run. A matching nonce alone never grants cleanup ownership.
            parent.mkdir(staging_dir.name)
            staging_identity = parent.require_dir(staging_dir.name)
        spec = ProcessDownloadTaskSpec(
            process_name=f"lantai-llamacpp-download-{staging_dir.name}",
            command=["lantai-llamacpp-download", asset.url],
            task=ProcessDownloadTask(
                target=type(self)._download_worker,
                owns_child_processes=True,
                payload={
                    "url": asset.url,
                    "staging_dir": str(staging_dir),
                    "file_name": asset.file_name,
                    "trusted_asset": asdict(asset),
                    "parent_ownership": ownership,
                    "staging_identity": staging_identity,
                    "job_nonce": nonce,
                    "chunk_size": chunk_size,
                    "timeout": timeout,
                    "headers": self._download_headers,
                },
                finalize_result=lambda result: self._finalize_download_result(
                    result, staging_dir=staging_dir, final_dir=dest_dir,
                    asset=asset, parent_ownership=ownership, nonce=nonce,
                    staging_identity=staging_identity,
                ),
                cleanup=lambda: self._cleanup_download_path(staging_dir, ownership, staging_identity),
            ),
            source=asset.url,
            poll_interval=0.2,
        )
        try:
            self._download_controller.start(spec)
        except BaseException:
            self._cleanup_download_path(staging_dir, ownership, staging_identity)
            raise

    async def setup_server(
        self,
        model_path: Path,
        model_name: str,
        max_context_length: int | None = None,
        port: int | None = None,
    ) -> LlamaCppServerSetupResult:
        """Setup llama.cpp server and return the runtime port and model info.

        Args:
            model_path: Path to a local HF repo directory or GGUF file
            model_name: Name of the model to be used in the server
            max_context_length: Optional context window passed to llama.cpp
            port: Optional fixed server port. None means auto select.
        """
        installed, message = self.check_llamacpp_installation()
        if not installed:
            raise RuntimeError(message or "llama.cpp server is not installed")
        if not model_path.exists():
            raise FileNotFoundError(f"Model path not found: {model_path}")

        resolved_model_path, resolved_mmproj_path = self._resolve_model_file(
            model_path,
        )
        model_info = self._build_model_info(
            model_name=model_name,
            resolved_mmproj_path=resolved_mmproj_path,
        )

        if (
            model_name == self._server_model_name
            and self._server_process is not None
        ):
            if self._server_process.returncode is None and (
                port is None or port == self._server_port
            ):
                logger.info(
                    "Requested model %s is already served on port %s",
                    model_name,
                    self._server_port,
                )
                return LlamaCppServerSetupResult(
                    port=self._server_port,
                    model_info=model_info,
                )
            else:
                logger.warning(
                    "Previous llama.cpp server process for model %s exited "
                    "unexpectedly with code %s. Restarting server.",
                    self._server_model_name,
                    self._server_process.returncode,
                )
        if self._server_process and self._server_process.returncode is None:
            await self.shutdown_server()

        self._server_transitioning = True
        port = self._resolve_server_port(port)
        process_kwargs: dict[str, Any] = {}
        if self.os_name != "windows":
            process_kwargs["start_new_session"] = True
        process = await self._create_server_process(
            resolved_model_path=resolved_model_path,
            resolved_mmproj_path=resolved_mmproj_path,
            model_name=model_name,
            port=port,
            max_context_length=max_context_length,
            process_kwargs=process_kwargs,
        )

        self._server_process = process
        self._server_port = port
        self._server_model_name = model_name
        self._server_log_task = asyncio.create_task(
            self._drain_server_logs(),
            name="llamacpp_server_logs",
        )

        try:
            logger.info("Waiting for llama.cpp server to become ready...")
            await self.server_ready()
            logger.info("llama.cpp server is ready")
        except Exception:
            await self.shutdown_server()
            raise
        finally:
            self._server_transitioning = False

        logger.info(
            "llama.cpp server started on port %s for model %s",
            port,
            model_name,
        )
        return LlamaCppServerSetupResult(
            port=port,
            model_info=model_info,
        )

    async def list_devices(self) -> list[str]:
        """List available devices for llama.cpp using
        `llama-server --list-devices`."""
        installed, message = self.check_llamacpp_installation()
        if not installed:
            raise RuntimeError(message or "llama.cpp server is not installed")

        result = await run_command_async(
            [str(self.executable), "--list-devices"],
            timeout=30,
        )
        return [
            line.strip()
            for line in result.combined_output.splitlines()
            if line.strip() and not line.startswith("ggml")
        ][1:]

    async def get_version(self) -> str:
        """get llama.cpp server version using `llama-server --version`."""
        installed, message = self.check_llamacpp_installation()
        if not installed:
            raise RuntimeError(message or "llama.cpp server is not installed")

        result = await run_command_async(
            [str(self.executable), "--version"],
            timeout=30,
        )
        lines = result.stderr_lines
        for line in lines:
            if line.startswith("version:"):
                version = re.match(r"version:\s*(?:b)?([0-9]+)(?:\s|$)", line)
                if version:
                    return version.group(1)
        raise RuntimeError(
            "Unexpected version output from llama.cpp server: "
            f"{result.combined_output}",
        )

    def _is_download_active(self) -> bool:
        """Return whether the background download thread is active."""
        return self._download_controller.is_active()

    async def _create_server_process(
        self,
        resolved_model_path: Path,
        resolved_mmproj_path: Path | None,
        model_name: str,
        port: int,
        max_context_length: int | None,
        process_kwargs: dict[str, Any],
    ) -> ManagedProcess:
        self.log_dir.mkdir(parents=True, exist_ok=True)
        command = [
            str(self.executable),
            "--host",
            "127.0.0.1",
            "--port",
            str(port),
            "--model",
            str(resolved_model_path),
            "--alias",
            model_name,
            "--log-file",
            str(self.log_dir / "llama-server.log"),
            "--gpu-layers",
            "auto",
        ]
        logger.info(
            "Start llama.cpp server with command:\n  > %s",
            " ".join(command),
        )
        if max_context_length is not None:
            command.extend(["--ctx-size", str(max_context_length)])
        if resolved_mmproj_path is not None:
            command.extend(
                [
                    "--mmproj",
                    str(resolved_mmproj_path),
                ],
            )

        logger.info(
            "Setting up llama.cpp server for model %s at path %s%s",
            model_name,
            resolved_model_path,
            (
                f" with mmproj {resolved_mmproj_path}"
                if resolved_mmproj_path is not None
                else ""
            ),
        )
        return await start_command_async(
            command,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            **process_kwargs,
        )

    async def shutdown_server(self) -> None:
        """Shutdown the llama.cpp server if it's running."""
        with self._server_shutdown_context() as process:
            await self._cancel_server_log_task()

            if process and process.returncode is None:
                await shutdown_process(
                    process,
                    graceful_timeout=5.0,
                    kill_timeout=3.0,
                )

    def shutdown_server_sync(self) -> None:
        """Best-effort synchronous cleanup for shutdown and atexit paths."""
        with self._server_shutdown_context() as process:
            self._cancel_server_log_task_nowait()

            if process and process.returncode is None:
                shutdown_process_sync(
                    process,
                    graceful_timeout=5.0,
                    kill_timeout=1.0,
                )

    # -----------------------------
    # Internal helpers
    # -----------------------------
    def _resolve_dest_dir(self, dest: str | Path) -> Path:
        path = absolute_path(Path(dest))
        if os.path.lexists(path) and (path.is_symlink() or not path.is_dir()):
            raise ValueError("dest must be a directory path without symlinks")
        if os.path.lexists(path) and getattr(path.lstat(), "st_file_attributes", 0) & 0x400:
            raise ValueError("dest must be a directory path without reparse points")
        return path

    def _resolve_model_file(
        self,
        model_path: Path,
    ) -> tuple[Path, Path | None]:
        if model_path.is_file():
            if model_path.suffix.lower() != ".gguf":
                raise RuntimeError(
                    f"Model file must be a .gguf file: {model_path}",
                )
            return model_path.resolve(), None

        gguf_files = sorted(
            candidate
            for candidate in model_path.rglob("*.gguf")
            if candidate.is_file()
            and not any(
                part.startswith(".")
                for part in candidate.relative_to(model_path).parts[:-1]
            )
        )
        if not gguf_files:
            raise RuntimeError(
                "Model repository at "
                f"{model_path} does not contain any .gguf files.",
            )

        mmproj_files = [
            candidate
            for candidate in gguf_files
            if candidate.name.lower().startswith("mmproj")
        ]
        model_files = [
            candidate
            for candidate in gguf_files
            if not candidate.name.lower().startswith("mmproj")
        ]

        if not model_files:
            raise RuntimeError(
                "Model repository at "
                f"{model_path} does not contain any model .gguf files.",
            )

        return (
            model_files[0].resolve(),
            mmproj_files[0].resolve() if mmproj_files else None,
        )

    @staticmethod
    def _build_model_info(
        model_name: str,
        resolved_mmproj_path: Path | None,
    ) -> ModelInfo:
        supports_multimodal = resolved_mmproj_path is not None
        return ModelInfo(
            id=model_name,
            name=model_name,
            supports_multimodal=supports_multimodal,
            # TODO: add more detailed capability flags
            supports_image=supports_multimodal,
            supports_video=False,
            probe_source="probed",
        )

    @staticmethod
    def _transaction_name(final_name: str) -> str:
        DirectoryAnchor._name(final_name)
        return f".llamacpp-transaction-{final_name}.json"

    def _recover_installation(self, parent: DirectoryAnchor, final_name: str) -> None:
        """Resolve an interrupted swap using recorded directory identities."""
        journal_name = self._transaction_name(final_name)
        if parent.info(journal_name) is None:
            return
        with parent.file(journal_name) as stream:
            data = stream.read(8193)
            if len(data) > 8192:
                raise ValueError("Installation transaction is too large")
            journal = json.loads(data)
        if not isinstance(journal, dict) or journal.get("schema_version") != 1 or journal.get("final_name") != final_name:
            raise ValueError("Invalid llama.cpp installation transaction")
        nonce = journal.get("job_nonce", "")
        if not isinstance(nonce, str) or not re.fullmatch(r"[0-9a-f]{32}", nonce):
            raise ValueError("Invalid installation transaction owner")
        stage_name, backup_name = f".llamacpp-{nonce}", f".llamacpp-backup-{nonce}"
        if journal.get("stage_name") != stage_name or journal.get("backup_name") != backup_name:
            raise ValueError("Invalid installation transaction paths")
        def recorded_identity(value):
            if not isinstance(value, list) or len(value) != 2 or any(
                type(part) is not int or part < 0 for part in value):
                raise ValueError("Invalid installation transaction identity")
            return tuple(value)
        candidate_id = recorded_identity(journal.get("candidate_identity"))
        old_id = recorded_identity(journal["old_identity"]) if journal.get("old_identity") is not None else None
        final_info, backup_info = parent.info(final_name), parent.info(backup_name)
        if final_info is not None and identity(final_info) == candidate_id:
            try:
                with DirectoryAnchor(parent.path / final_name) as directory:
                    self._validate_installed_directory(directory)
            except Exception:
                if old_id is None or backup_info is None:
                    raise
                # The failed candidate is retained privately until the old
                # directory has been restored and the journal can be retired.
                parent.rename(final_name, stage_name, candidate_id)
                parent.rename(backup_name, final_name, old_id)
                parent.remove_dir(stage_name, candidate_id)
            else:
                if backup_info is not None:
                    if old_id is None:
                        raise ValueError("Unexpected installation backup")
                    parent.remove_dir(backup_name, old_id)
        elif final_info is None:
            if old_id is not None:
                parent.rename(backup_name, final_name, old_id)
            stage_info = parent.info(stage_name)
            if stage_info is not None:
                parent.remove_dir(stage_name, candidate_id)
        elif old_id is not None and identity(final_info) == old_id and backup_info is None:
            stage_info = parent.info(stage_name)
            if stage_info is not None:
                parent.remove_dir(stage_name, candidate_id)
        else:
            raise ValueError("Installation transaction entries changed; manual recovery required")
        parent.unlink(journal_name)

    def _finalize_download_result(
        self, result: DownloadTaskResult, *, staging_dir: Path, final_dir: Path,
        asset: TrustedLlamaCppAsset | None = None,
        parent_ownership: dict[str, Any] | None = None, nonce: str | None = None,
        staging_identity: tuple[int, int] | None = None,
    ) -> tuple[DownloadTaskResult, int | None]:
        if result.status != DownloadTaskStatus.COMPLETED:
            return result, None
        try:
            staging_dir, final_dir = absolute_path(staging_dir), absolute_path(final_dir)
            if result.local_path != str(staging_dir) or staging_dir.parent != final_dir.parent:
                raise ValueError("Download completion belongs to a different candidate")
            if asset is None or parent_ownership is None or nonce is None or staging_identity is None:
                raise ValueError("Download completion has no admitted installation owner")
            if staging_dir.name != f".llamacpp-{nonce}":
                raise ValueError("Download candidate name does not match its owner")
            with DirectoryAnchor(final_dir.parent, expected=parent_ownership) as parent:
                self._recover_installation(parent, final_dir.name)
                candidate_id = parent.require_dir(staging_dir.name, staging_identity)
                with DirectoryAnchor(staging_dir) as candidate:
                    if tuple(candidate.chain[-1]) != staging_identity:
                        raise ValueError("Download candidate directory identity changed")
                    validate_receipt(candidate, asset, nonce=nonce,
                                     license_manifest=reviewed_license_manifest(asset))
                old_info = parent.info(final_dir.name)
                old_id = parent.require_dir(final_dir.name) if old_info is not None else None
                backup_name = f".llamacpp-backup-{nonce}"
                if parent.info(backup_name) is not None:
                    raise ValueError("Installation backup name is occupied")
                journal_name = self._transaction_name(final_dir.name)
                journal = {"schema_version": 1, "final_name": final_dir.name,
                           "stage_name": staging_dir.name, "backup_name": backup_name,
                           "job_nonce": nonce, "candidate_identity": candidate_id,
                           "old_identity": old_id}
                with parent.file(journal_name, "wb") as stream:
                    stream.write((json.dumps(journal) + "\n").encode())
                    stream.flush()
                    os.fsync(stream.fileno())
                parent.sync()
                moved_old = promoted = False
                try:
                    if old_id is not None:
                        parent.rename(final_dir.name, backup_name, old_id)
                        moved_old = True
                    parent.rename(staging_dir.name, final_dir.name, candidate_id)
                    promoted = True
                    parent.assert_current()
                    with DirectoryAnchor(final_dir) as installed:
                        validate_receipt(installed, asset, nonce=nonce,
                                         license_manifest=reviewed_license_manifest(asset))
                except Exception:
                    if promoted:
                        parent.rename(final_dir.name, staging_dir.name, candidate_id, rollback=True)
                    if moved_old and old_id is not None:
                        parent.rename(backup_name, final_dir.name, old_id, rollback=True)
                    parent.unlink(journal_name)
                    raise
                # A cleanup failure leaves a recoverable journal, rather than
                # turning a verified new installation into a destructive retry.
                try:
                    if moved_old and old_id is not None:
                        parent.remove_dir(backup_name, old_id)
                    parent.unlink(journal_name)
                except Exception as exc:
                    logger.warning("Verified llama.cpp install awaits transaction cleanup: %s", exc)
            return DownloadTaskResult(status=DownloadTaskStatus.COMPLETED, local_path=str(final_dir)), None
        except Exception as exc:
            logger.warning("Failed to finalize llama.cpp installation: %s", exc)
            return DownloadTaskResult(
                status=DownloadTaskStatus.FAILED,
                error=f"llama.cpp download completed, but installing files to {final_dir} failed: {exc}",
            ), None

    @staticmethod
    def _staged_process_environment() -> dict[str, str]:
        environment = {"PATH": os.defpath, "LC_ALL": "C"}
        if os.name == "nt":
            system_root = os.environ.get("SystemRoot", r"C:\Windows")
            environment.update(SystemRoot=system_root, WINDIR=system_root,
                               PATH=os.pathsep.join((str(Path(system_root) / "System32"), system_root)))
        return environment

    @staticmethod
    def _check_staged_health(candidate: DirectoryAnchor, asset: TrustedLlamaCppAsset) -> dict[str, Any]:
        # Archive bytes and every executable/dependency must be admitted before
        # any downloaded code runs. The private job remains unpublished.
        validate_candidate_files(candidate, asset)
        server = "llama-server.exe" if asset.os_name == "windows" else "llama-server"
        command = [str(candidate.path / server), "--version"]
        # --version loads ggml backends before printing. Inherit neither
        # external plugin locations nor runtime options or user credentials.
        environment = LlamaCppBackend._staged_process_environment()
        candidate.assert_current()
        result = run_command(command, timeout=15, cwd=str(candidate.path),
                             env=environment, check=False)
        match = re.search(r"(?m)^version:\s*(?:b)?([0-9]+)(?:\s|$)", result.combined_output)
        if result.returncode != 0 or match is None or match.group(1) != asset.tag.removeprefix("b"):
            raise ValueError("Staged llama-server health/version check failed")
        validate_candidate_files(candidate, asset)
        return {"version": match.group(1), "returncode": 0}

    @staticmethod
    def _download_worker(payload: dict[str, Any], queue: Any) -> None:
        ensure_standard_streams()
        url = str(payload.get("url", ""))
        try:
            asset = TrustedLlamaCppAsset(**payload["trusted_asset"])
            if (not re.fullmatch(r"[0-9a-f]{64}", asset.sha256)
                    or not isinstance(asset.size_bytes, int) or asset.size_bytes <= 0
                    or asset.url != url or asset.file_name != payload["file_name"]
                    or asset.url != f"{OFFICIAL_RELEASE_BASE}/{asset.tag}/{asset.file_name}"):
                raise ValueError("Invalid trusted release identity")
            staging_dir = absolute_path(Path(payload["staging_dir"]))
            nonce = payload["job_nonce"]
            if not isinstance(nonce, str) or not re.fullmatch(r"[0-9a-f]{32}", nonce) or staging_dir.name != f".llamacpp-{nonce}":
                raise ValueError("Invalid download staging owner")
            chunk_size, timeout = int(payload["chunk_size"]), int(payload["timeout"])
            if chunk_size <= 0 or timeout <= 0:
                raise ValueError("Invalid download limits")
            staging_identity = payload["staging_identity"]
            if (not isinstance(staging_identity, (list, tuple)) or len(staging_identity) != 2
                    or any(type(part) is not int or part < 0 for part in staging_identity)):
                raise ValueError("Invalid download directory identity")
            staging_identity = tuple(staging_identity)
            with DirectoryAnchor(staging_dir.parent, expected=payload["parent_ownership"]) as parent:
                parent.require_dir(staging_dir.name, staging_identity)
                with DirectoryAnchor(staging_dir) as candidate:
                    if tuple(candidate.chain[-1]) != staging_identity:
                        raise ValueError("Download candidate directory identity changed")
                    downloaded, digest = 0, hashlib.sha256()
                    with httpx.Client(follow_redirects=True, timeout=timeout, trust_env=False) as client:
                        with client.stream("GET", url, headers=dict(payload["headers"])) as response:
                            response.raise_for_status()
                            for redirect in [*getattr(response, "history", []), response]:
                                address = getattr(redirect, "url", None)
                                if address is not None and (address.scheme != "https" or address.host not in {
                                    "github.com", "release-assets.githubusercontent.com", "objects.githubusercontent.com"
                                }):
                                    raise ValueError("Release download redirected outside the trusted HTTPS hosts")
                            length = response.headers.get("Content-Length")
                            if length is not None and (not length.isdigit() or int(length) != asset.size_bytes):
                                raise ValueError("Release Content-Length does not match the trusted size")
                            with candidate.file(asset.file_name, "wb") as stream:
                                for chunk in response.iter_bytes(chunk_size=chunk_size):
                                    if not chunk:
                                        continue
                                    downloaded += len(chunk)
                                    if downloaded > asset.size_bytes:
                                        raise ValueError("Release exceeds the trusted size")
                                    digest.update(chunk)
                                    stream.write(chunk)
                                    queue.put(DownloadProgressUpdate(downloaded_bytes=downloaded,
                                              total_bytes=asset.size_bytes, source=url).to_message())
                                stream.flush()
                                os.fsync(stream.fileno())
                    if downloaded != asset.size_bytes or digest.hexdigest() != asset.sha256:
                        raise ValueError("Release size or SHA256 does not match the trusted artifact")
                    parent.assert_current()
                    LlamaCppBackend._extract_archive(staging_dir / asset.file_name, staging_dir, trusted_asset=asset)
                    health = LlamaCppBackend._check_staged_health(candidate, asset)
                    license_manifest = reviewed_license_manifest(asset)
                    server = "llama-server.exe" if asset.os_name == "windows" else "llama-server"
                    candidate.assert_current()
                    license_result = run_command([str(candidate.path / server), "--license"],
                        timeout=15, cwd=str(candidate.path), check=False,
                        env=LlamaCppBackend._staged_process_environment())
                    if license_result.returncode != 0:
                        raise ValueError("Staged llama-server license check failed")
                    # stderr contains platform diagnostics. stdout is complete,
                    # untrimmed text; its fixed expected digest grants admission.
                    notice = license_result.stdout.encode("utf8")
                    validate_candidate_files(candidate, asset)
                    write_notices(candidate, asset, license_manifest, notice)
                    write_receipt(candidate, asset, nonce, health=health, license_manifest=license_manifest)
                    validate_receipt(candidate, asset, nonce=nonce, license_manifest=license_manifest)
                    parent.assert_current()
            queue.put(DownloadTaskResult(status=DownloadTaskStatus.COMPLETED,
                                        local_path=str(staging_dir)).to_message())
        except Exception as exc:
            error_message = LlamaCppBackend._format_download_error(exc, url)
            queue.put(DownloadTaskResult(status=DownloadTaskStatus.FAILED, error=error_message).to_message())
            logger.warning("llama.cpp download failed for %s: %s", url, error_message)

    @staticmethod
    def _format_download_error(exc: Exception, url: str) -> str:
        if isinstance(exc, httpx.HTTPStatusError):
            status_code = exc.response.status_code
            if status_code == 404:
                return (
                    "llama.cpp download package was not found (HTTP 404). "
                    "The requested version may not exist, is no longer "
                    "available, or your hardware or operating system "
                    "version is not supported."
                )
            if status_code in {401, 403}:
                return (
                    "llama.cpp download address is unavailable or access is "
                    f"denied (HTTP {status_code}). Please verify the "
                    "requested version, or check whether your hardware or "
                    "operating system version is supported."
                )
            if status_code >= 500:
                return (
                    "llama.cpp download server is temporarily unavailable "
                    f"(HTTP {status_code}). Please try again later."
                )
            return (
                "llama.cpp download failed with an unexpected server "
                f"response (HTTP {status_code})."
            )

        if isinstance(exc, httpx.RequestError):
            return (
                "Unable to connect to the llama.cpp download server. "
                f"Request URL: {url}."
            )

        return f"llama.cpp download failed: {exc}"

    @staticmethod
    def _find_free_port(host: str = "127.0.0.1") -> int:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.bind((host, 0))
            sock.listen(1)
            return int(sock.getsockname()[1])

    @staticmethod
    def _is_port_available(port: int, host: str = "127.0.0.1") -> bool:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            if system_info.get_os_name() == "windows" and hasattr(
                socket,
                "SO_EXCLUSIVEADDRUSE",
            ):
                sock.setsockopt(
                    socket.SOL_SOCKET,
                    socket.SO_EXCLUSIVEADDRUSE,
                    1,
                )
            try:
                sock.bind((host, port))
            except OSError:
                return False
        return True

    def _resolve_server_port(self, requested_port: int | None) -> int:
        if requested_port is None:
            return self._find_free_port()
        if not self._is_port_available(requested_port):
            raise ValueError(
                f"Requested llama.cpp port is unavailable: {requested_port}",
            )
        return requested_port

    async def server_ready(
        self,
        timeout: float = 120.0,
    ) -> bool:
        """Check if the llama.cpp server is ready."""
        if not self._server_process or self._server_port is None:
            raise RuntimeError("llama.cpp server process was not created")

        health_url = f"http://127.0.0.1:{self._server_port}/health"
        deadline = asyncio.get_running_loop().time() + timeout
        async with httpx.AsyncClient(timeout=2.0, trust_env=False) as client:
            while asyncio.get_running_loop().time() < deadline:
                if self._server_process.returncode is not None:
                    raise RuntimeError(
                        "llama.cpp server exited before becoming ready",
                    )
                try:
                    response = await client.get(health_url)
                    if response.status_code < 500:
                        return True

                    logger.info(
                        "llama.cpp health check returned %s while "
                        "waiting for %s",
                        response.status_code,
                        health_url,
                    )
                except httpx.HTTPError as exc:
                    logger.info(
                        "llama.cpp health check failed for %s: %s",
                        health_url,
                        exc,
                    )

                await asyncio.sleep(1)
        raise RuntimeError("Timed out waiting for llama.cpp server to start")

    async def _drain_server_logs(self) -> None:
        if not self._server_process or not self._server_process.stdout:
            return

        while True:
            line = await self._server_process.stdout.readline()
            if not line:
                break
            logger.debug(
                "llama-server: %s",
                line.decode("utf-8", errors="replace").rstrip(),
            )

    async def _cancel_server_log_task(self) -> None:
        task = self._server_log_task
        if task and not task.done():
            task.cancel()
            with suppress(asyncio.CancelledError):
                await task

    def _cancel_server_log_task_nowait(self) -> None:
        task = self._server_log_task
        if task and not task.done():
            task.cancel()

    @contextmanager
    def _server_shutdown_context(self) -> Iterator[ManagedProcess | None]:
        self._server_transitioning = True
        process = self._server_process
        try:
            yield process
        finally:
            self._reset_server_state()

    def _reset_server_state(self) -> None:
        self._server_process = None
        self._server_log_task = None
        self._server_port = None
        self._server_model_name = None
        self._server_transitioning = False

    def _shutdown_server_at_exit(self) -> None:
        with suppress(Exception):
            self.shutdown_server_sync()

    @staticmethod
    def _extract_archive(archive_path: Path, dest_dir: Path, *,
                         trusted_asset: TrustedLlamaCppAsset | None = None) -> None:
        extract_archive(archive_path, dest_dir, trusted_asset=trusted_asset)

    @staticmethod
    def _cleanup_download_path(path: Path, ownership: dict[str, Any],
                               staging_identity: tuple[int, int]) -> None:
        try:
            with DirectoryAnchor(path.parent, expected=ownership) as parent:
                if not re.fullmatch(r"\.llamacpp-[0-9a-f]{32}", path.name):
                    raise ValueError("Cleanup path is not an owned download stage")
                if parent.info(path.name) is not None:
                    parent.remove_dir(path.name, staging_identity)
        except FileNotFoundError:
            pass

    @property
    def _download_headers(self) -> dict[str, str]:
        return {
            "User-Agent": (
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/135.0.0.0 Safari/537.36"
            ),
            "Accept": "*/*",
        }

    def _resolve_os_name(self) -> str:
        os_name = system_info.get_os_name()
        if os_name in ("windows", "macos", "linux"):
            return os_name
        raise RuntimeError(f"Unsupported OS: {os_name}")

    def _resolve_arch(self) -> str:
        arch = system_info.get_architecture()
        if arch in ("x64", "arm64"):
            return arch
        raise RuntimeError(f"Unsupported architecture: {arch}")

    def _ensure_supported_macos_version(self) -> tuple[bool, str]:
        if self.os_name != "macos":
            return True, ""
        macos_version = system_info.get_macos_version()
        if macos_version is None:
            logger.warning("Unable to determine macOS version for llama.cpp")
            return False, "Unknown macOS version"

        if macos_version < self._MIN_MACOS_VERSION:
            current_version = ".".join(str(part) for part in macos_version)
            min_version = ".".join(
                str(part) for part in self._MIN_MACOS_VERSION
            )
            return (
                False,
                f"Unsupported macOS version: {current_version} "
                f"(requires {min_version} or later)",
            )
        return True, ""

    def _resolve_backend(self) -> str:
        # On macOS and Linux, only CPU backend is supported
        if self.os_name in ("macos", "linux"):
            return "cpu"

        # On Windows, check for CUDA support
        if self.cuda_version is not None:
            return "cuda"
        return "cpu"

    def _resolve_cuda_version(self) -> Optional[str]:
        if self.os_name != "windows":
            return None

        cuda_version = system_info.get_cuda_version()
        if cuda_version is None:
            return None

        parts = cuda_version.split(".")
        major = parts[0]
        minor = int(parts[1]) if len(parts) > 1 and parts[1].isdigit() else 0

        if major == "12":
            return "12.4" if minor >= 4 else None
        if major == "13":
            return "13.1"
        return None

    def _build_filename(self, tag: str) -> str:
        if self.os_name == "macos":
            return f"llama-{tag}-bin-macos-{self.arch}.tar.gz"

        if self.os_name == "linux":
            return f"llama-{tag}-bin-ubuntu-{self.arch}.tar.gz"

        if self.os_name == "windows":
            if self.backend == "cuda":
                if self.arch != "x64":
                    raise RuntimeError(
                        "Windows CUDA package is only supported for x64.",
                    )
                return (
                    f"llama-{tag}-bin-win-cuda-"
                    f"{self.cuda_version}-{self.arch}.zip"
                )
            return f"llama-{tag}-bin-win-cpu-{self.arch}.zip"

        raise RuntimeError(f"Unsupported OS: {self.os_name}")
