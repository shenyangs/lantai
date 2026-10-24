"""One process-safe lock outside memory roots replaced by adoption or clearing."""

import os
import threading
from contextlib import contextmanager
from functools import wraps
from pathlib import Path

from ...utils.authoritative_store import exclusive_store_lock
from ...utils.path_transaction import recover_path_transaction
from ...utils.restore_fence import restore_read_fence

_DEPTH = threading.local()


def memory_transaction_journal(root: Path) -> Path:
    return root / ".memory-change.pending.json"


def memory_state_lock_path(working_dir: str | Path) -> Path:
    root = Path(working_dir).resolve()
    # A disaster restore can replace the entire workspace, including every
    # category and lock inside it. The sibling lock retains one inode.
    return root.parent / f".{root.name}.memory-state.lock"


@contextmanager
def memory_state_lock(working_dir: str | Path):
    root = Path(working_dir).resolve()
    key = (os.getpid(), str(root))
    with restore_read_fence(), exclusive_store_lock(memory_state_lock_path(root)):
        depths = getattr(_DEPTH, "depths", None)
        if depths is None:
            depths = _DEPTH.depths = {}
        if key not in depths:
            recover_path_transaction(memory_transaction_journal(root), root=root)
        depths[key] = depths.get(key, 0) + 1
        try:
            yield
        finally:
            depths[key] -= 1
            if not depths[key]:
                del depths[key]


def locked_memory_method(method):
    """Keep a public read or read/modify/write under the workspace fence."""
    @wraps(method)
    def locked(self, *args, **kwargs):
        with memory_state_lock(self.working_dir):
            return method(self, *args, **kwargs)
    return locked
