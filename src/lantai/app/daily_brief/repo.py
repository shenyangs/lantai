# -*- coding: utf-8 -*-
from __future__ import annotations

import asyncio
import errno
import hashlib
import json
import os
import tempfile
from contextlib import ExitStack, asynccontextmanager
from pathlib import Path
from typing import Any

from ..reminders.models import RemindersFile
from ..reminders.repo import _exclusive_path_lock
from .models import DailyBriefStore


class DailyBriefRepository:
    """One workspace-scoped, atomic authority for all brief state."""

    def __init__(self, workspace_dir: str | Path):
        self.workspace_dir = Path(workspace_dir).expanduser()
        self.root = self.workspace_dir / "daily_brief"
        self.path = self.root / "daily_brief.json"

    @asynccontextmanager
    async def activation_guard(self, action_id: str):
        """Serialize an action before discovery without blocking the event loop.

        The OS releases this lock after a crash. Keep the lock file in place:
        unlinking it would allow another process to lock a different inode.
        """
        self.root.mkdir(parents=True, exist_ok=True)
        digest = hashlib.sha256(action_id.encode("utf-8")).hexdigest()
        descriptor = os.open(
            self.root / f".activation-{digest}.lock", os.O_RDWR | os.O_CREAT, 0o600
        )
        acquired = False
        try:
            if os.name == "nt":
                import msvcrt
                if os.fstat(descriptor).st_size == 0:
                    os.write(descriptor, b"\0")
                def acquire():
                    os.lseek(descriptor, 0, os.SEEK_SET)
                    msvcrt.locking(descriptor, msvcrt.LK_NBLCK, 1)
                def release():
                    os.lseek(descriptor, 0, os.SEEK_SET)
                    msvcrt.locking(descriptor, msvcrt.LK_UNLCK, 1)
            else:
                import fcntl
                def acquire():
                    fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
                def release():
                    fcntl.flock(descriptor, fcntl.LOCK_UN)
            while not acquired:
                try:
                    acquire()
                    acquired = True
                except OSError as exc:
                    if exc.errno not in {errno.EACCES, errno.EAGAIN, errno.EDEADLK}:
                        raise
                    await asyncio.sleep(0.05)
            yield
        finally:
            if acquired:
                release()
            os.close(descriptor)

    @staticmethod
    def _read_model(path: Path) -> DailyBriefStore:
        if not path.exists():
            return DailyBriefStore()
        model = DailyBriefStore.model_validate_json(path.read_text(encoding="utf-8"))
        if model.version > 2:
            raise ValueError("daily brief data was written by a newer version")
        return model

    @staticmethod
    def _upgrade_for_write(path: Path, model: DailyBriefStore) -> DailyBriefStore:
        if model.version >= 2:
            return model
        # Reads remain side-effect free. The first successful mutation upgrades
        # under the authority lock and keeps the exact original for rollback.
        backup = path.with_name("daily_brief.v1.backup.json")
        if path.exists():
            original = path.read_bytes()
            checksum_path = path.with_name("daily_brief.v1.backup.sha256")
            digest = hashlib.sha256(original).hexdigest()
            if backup.exists():
                if backup.read_bytes() != original:
                    raise ValueError("daily brief migration backup conflicts with v1 source")
            else:
                DailyBriefRepository._write_bytes(backup, original)
            if checksum_path.exists():
                checksum = checksum_path.read_text(encoding="ascii").split()
                if not checksum or checksum[0] != digest:
                    raise ValueError("daily brief migration backup checksum mismatch")
            else:
                DailyBriefRepository._write_bytes(checksum_path,
                    f"{digest}  {backup.name}\n".encode("ascii"))
        return DailyBriefRepository._restore_preview_identities(model).model_copy(
            update={"version": 2}
        )

    @staticmethod
    def _restore_preview_identities(model: DailyBriefStore) -> DailyBriefStore:
        """Split v1 previews that accidentally shared one edition identity.

        Keep the latest identity for links without a revision. Exact older links
        retain an alias, and stored references follow their original revision.
        """
        groups: dict[str, list[Any]] = {}
        for edition in model.editions:
            if edition.subscription_id is None:
                groups.setdefault(edition.id, []).append(edition)
        replacements: dict[tuple[str, int], str] = {}
        restored: set[tuple[str, int]] = set()
        for old_id, editions in groups.items():
            grant_ids = {edition.grant_id for edition in editions}
            if len(grant_ids) < 2:
                continue
            latest_grant = max(editions, key=lambda item: item.revision).grant_id
            for grant_id in grant_ids:
                same_grant = [edition for edition in editions if edition.grant_id == grant_id]
                newest = max(same_grant, key=lambda item: item.revision)
                restored.add((old_id, newest.revision))
                if grant_id == latest_grant:
                    continue
                digest = hashlib.sha256(f"{old_id}\0{grant_id}".encode()).hexdigest()[:16]
                for edition in same_grant:
                    replacements[(old_id, edition.revision)] = f"{old_id}-legacy-{digest}"
        if not replacements:
            return model

        def identity(edition_id: str, revision: int) -> str:
            return replacements.get((edition_id, revision), edition_id)

        editions = [edition.model_copy(update={
            "id": identity(edition.id, edition.revision),
            "status": "published" if (edition.id, edition.revision) in restored
                and edition.status == "superseded" else edition.status,
        }) for edition in model.editions]
        by_cycle = {edition.cycle_id: edition.id for edition in editions}
        read_revisions = dict(model.read_revisions)
        for (old_id, revision), new_id in replacements.items():
            if read_revisions.get(old_id, 0) >= revision:
                read_revisions[new_id] = max(read_revisions.get(new_id, 0), revision)
        feedback = []
        for entry in model.feedback:
            matches = {edition.id for edition in editions if entry.item_id
                and any(item.id == entry.item_id for item in edition.items)}
            feedback.append(entry.model_copy(update={"edition_id": next(iter(matches))})
                if len(matches) == 1 else entry)
        return model.model_copy(update={
            "editions": editions,
            "edition_aliases": {**model.edition_aliases, **{
                f"{old_id}:{revision}": new_id
                for (old_id, revision), new_id in replacements.items()}},
            "cycles": [cycle.model_copy(update={"edition_id": by_cycle[cycle.id]})
                if cycle.id in by_cycle else cycle for cycle in model.cycles],
            "discussion_drafts": [draft.model_copy(update={"context_ref":
                draft.context_ref.model_copy(update={"edition_id": identity(
                    draft.context_ref.edition_id, draft.context_ref.edition_revision)})})
                for draft in model.discussion_drafts],
            "delivery_receipts": [receipt.model_copy(update={"edition_id": identity(
                receipt.edition_id, receipt.edition_revision)}) for receipt in model.delivery_receipts],
            "saved_items": [saved.model_copy(update={"edition_id": identity(
                saved.edition_id, saved.edition_revision)}) for saved in model.saved_items],
            "feedback": feedback,
            "read_revisions": read_revisions,
        })

    @staticmethod
    def _write_bytes(path: Path, content: bytes) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        descriptor, tmp_name = tempfile.mkstemp(
            prefix=f".{path.name}.", suffix=".tmp", dir=path.parent
        )
        tmp_path = Path(tmp_name)
        try:
            with os.fdopen(descriptor, "wb") as handle:
                handle.write(content)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(tmp_path, path)
        finally:
            tmp_path.unlink(missing_ok=True)

    @staticmethod
    def _write_model(path: Path, model: DailyBriefStore) -> None:
        content = json.dumps(model.model_dump(mode="json"), ensure_ascii=False,
            indent=2, sort_keys=True).encode("utf-8")
        DailyBriefRepository._write_bytes(path, content)

    @classmethod
    def _update_model(cls, path: Path, updater: Any) -> DailyBriefStore:
        with _exclusive_path_lock(path):
            data = cls._read_model(path)
            next_data = updater(data)
            next_data = cls._upgrade_for_write(path, next_data)
            cls._write_model(path, next_data)
            return next_data

    @classmethod
    def _update_with_topics(
        cls,
        path: Path,
        reminders_path: Path,
        updater: Any,
    ) -> DailyBriefStore:
        """Commit while the current WatchTopic authority is held stable."""

        paths = sorted([path, reminders_path], key=lambda item: str(item.resolve()))
        with ExitStack() as stack:
            for lock_path in paths:
                stack.enter_context(_exclusive_path_lock(lock_path))
            data = cls._read_model(path)
            reminders = (
                RemindersFile.model_validate_json(
                    reminders_path.read_text(encoding="utf-8")
                )
                if reminders_path.exists()
                else RemindersFile()
            )
            next_data = updater(data, reminders)
            next_data = cls._upgrade_for_write(path, next_data)
            cls._write_model(path, next_data)
            return next_data

    @classmethod
    def _load_with_topics(
        cls,
        path: Path,
        reminders_path: Path,
    ) -> tuple[DailyBriefStore, RemindersFile]:
        """Read brief and WatchTopic authorities from one stable lock window."""

        paths = sorted([path, reminders_path], key=lambda item: str(item.resolve()))
        with ExitStack() as stack:
            for lock_path in paths:
                stack.enter_context(_exclusive_path_lock(lock_path))
            data = cls._read_model(path)
            reminders = (
                RemindersFile.model_validate_json(
                    reminders_path.read_text(encoding="utf-8")
                )
                if reminders_path.exists()
                else RemindersFile()
            )
            return data, reminders

    async def load(self) -> DailyBriefStore:
        return await asyncio.to_thread(self._read_model, self.path)

    async def update(self, updater: Any) -> DailyBriefStore:
        return await asyncio.to_thread(self._update_model, self.path, updater)

    async def update_with_topics(
        self,
        reminders_path: Path,
        updater: Any,
    ) -> DailyBriefStore:
        return await asyncio.to_thread(
            self._update_with_topics,
            self.path,
            reminders_path,
            updater,
        )

    async def load_with_topics(
        self,
        reminders_path: Path,
    ) -> tuple[DailyBriefStore, RemindersFile]:
        return await asyncio.to_thread(
            self._load_with_topics,
            self.path,
            reminders_path,
        )
