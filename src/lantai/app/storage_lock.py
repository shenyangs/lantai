# -*- coding: utf-8 -*-
"""Cross-thread and cross-process locks for file-backed workspace stores."""

from __future__ import annotations

import threading
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

try:  # pragma: no cover - Windows uses the in-process lock fallback.
    import fcntl
except ImportError:  # pragma: no cover
    fcntl = None  # type: ignore[assignment]


_PATH_LOCKS: dict[str, threading.RLock] = {}
_PATH_LOCKS_GUARD = threading.Lock()


def _lock_for_path(path: Path) -> threading.RLock:
    key = str(path.expanduser().resolve(strict=False))
    with _PATH_LOCKS_GUARD:
        return _PATH_LOCKS.setdefault(key, threading.RLock())


@contextmanager
def exclusive_path_lock(path: str | Path) -> Iterator[None]:
    """Serialize a complete read-modify-write cycle for one storage file."""

    target = Path(path).expanduser()
    target.parent.mkdir(parents=True, exist_ok=True)
    with _lock_for_path(target):
        if fcntl is None:
            yield
            return
        lock_path = target.with_suffix(target.suffix + ".lock")
        with lock_path.open("a+", encoding="utf-8") as handle:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
