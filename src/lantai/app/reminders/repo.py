# -*- coding: utf-8 -*-
from __future__ import annotations

import asyncio
import json
import os
import tempfile
import threading
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

try:  # pragma: no cover - platform-specific locking.
    import fcntl
except ImportError:  # pragma: no cover
    fcntl = None  # type: ignore[assignment]

from .models import RemindersFile


_PATH_LOCKS: dict[str, threading.RLock] = {}
_PATH_LOCKS_GUARD = threading.Lock()


def _lock_for_path(path: Path) -> threading.RLock:
    key = str(path.expanduser().resolve())
    with _PATH_LOCKS_GUARD:
        return _PATH_LOCKS.setdefault(key, threading.RLock())


@contextmanager
def _exclusive_path_lock(path: Path) -> Iterator[None]:
    """Serialize read-modify-write cycles for a workspace across repositories.

    API requests and the reminder delivery worker intentionally use separate
    repository instances.  An instance-local asyncio lock cannot protect that
    arrangement, so lock by storage path and, on POSIX, also take an advisory
    file lock for a second desktop process.
    """

    path.parent.mkdir(parents=True, exist_ok=True)
    with _lock_for_path(path):
        lock_path = path.with_suffix(path.suffix + ".lock")
        with lock_path.open("a+", encoding="utf-8") as handle:
            if fcntl is not None:
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            else:  # pragma: no cover - Windows also needs a cross-process lock.
                import msvcrt
                handle.seek(0)
                if not handle.read(1):
                    handle.write(" ")
                    handle.flush()
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_LOCK, 1)
            try:
                yield
            finally:
                if fcntl is not None:
                    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
                else:  # pragma: no cover
                    handle.seek(0)
                    msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)


class ReminderRepository:
    """File-backed reminder repository scoped to one agent workspace."""

    def __init__(self, workspace_dir: str | Path):
        self.workspace_dir = Path(workspace_dir).expanduser()
        self.root = self.workspace_dir / "reminders"
        self.path = self.root / "reminders.json"

    @staticmethod
    def _read_model(path: Path) -> RemindersFile:
        if not path.exists():
            return RemindersFile()
        payload = json.loads(path.read_text(encoding="utf-8"))
        return RemindersFile.model_validate(payload)

    @staticmethod
    def _write_model(path: Path, model: RemindersFile) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        descriptor, tmp_name = tempfile.mkstemp(
            prefix=f".{path.name}.",
            suffix=".tmp",
            dir=path.parent,
        )
        tmp_path = Path(tmp_name)
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as handle:
                handle.write(
                    json.dumps(
                        model.model_dump(mode="json"),
                        ensure_ascii=False,
                        indent=2,
                        sort_keys=True,
                    )
                )
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(tmp_path, path)
            if os.name == "posix":
                directory = os.open(path.parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
                try:
                    os.fsync(directory)
                finally:
                    os.close(directory)
        finally:
            tmp_path.unlink(missing_ok=True)

    @classmethod
    def _save_model(cls, path: Path, model: RemindersFile) -> RemindersFile:
        with _exclusive_path_lock(path):
            cls._write_model(path, model)
        return model

    @classmethod
    def _update_model(cls, path: Path, updater: Any) -> RemindersFile:
        with _exclusive_path_lock(path):
            data = cls._read_model(path)
            next_data = updater(data)
            if next_data != data:
                cls._write_model(path, next_data)
        return next_data

    async def load(self) -> RemindersFile:
        return await asyncio.to_thread(self._read_model, self.path)

    async def save(self, data: RemindersFile) -> RemindersFile:
        return await self._complete_write(self._save_model, self.path, data)

    async def update(self, updater: Any) -> RemindersFile:
        return await self._complete_write(self._update_model, self.path, updater)

    @staticmethod
    async def _complete_write(operation, *args):
        task = asyncio.create_task(asyncio.to_thread(operation, *args))
        try:
            return await asyncio.shield(task)
        except asyncio.CancelledError:
            while not task.done():
                try:
                    await asyncio.shield(task)
                except asyncio.CancelledError:
                    continue
            task.result()
            raise
