"""Consistent SQLite snapshots for the existing workspace backup pipeline."""

from contextlib import closing, contextmanager
from pathlib import Path
import sqlite3
import tempfile


@contextmanager
def reading_snapshot(source: Path, stop_event=None):
    with tempfile.TemporaryDirectory(prefix="lantai-reading-backup-") as temporary:
        target = Path(temporary) / "library.sqlite3"

        def progress(_status, _remaining, _total):
            if stop_event and stop_event.is_set():
                raise InterruptedError("Reading backup cancelled")

        with closing(
            sqlite3.connect(source.as_uri() + "?mode=ro", uri=True, timeout=10)
        ) as original:
            with closing(sqlite3.connect(target)) as destination:
                original.backup(destination, pages=256, progress=progress, sleep=0.01)
        target.chmod(0o600)
        yield target
