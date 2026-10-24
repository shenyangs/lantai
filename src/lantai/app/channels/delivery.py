# -*- coding: utf-8 -*-
"""Durable ingress ledger for idempotency, retry and dead letters."""

from __future__ import annotations

import hashlib
import json
import sqlite3
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional
from uuid import uuid4


_EPHEMERAL_KEYS = {
    "reply_future",
    "reply_loop",
    "incoming_message",
    "websocket",
    "client",
}
_EXTERNAL_ID_KEYS = (
    "message_id",
    "msg_id",
    "event_id",
    "update_id",
    "id",
)
_ACCOUNT_ID_KEYS = ("account_id", "workspace_id", "bot_id", "app_id")


def _jsonable(value: Any) -> Any:
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, dict):
        return {
            str(key): _jsonable(item)
            for key, item in value.items()
            if str(key) not in _EPHEMERAL_KEYS
        }
    if isinstance(value, (list, tuple, set, frozenset)):
        return [_jsonable(item) for item in value]
    model_dump = getattr(value, "model_dump", None)
    if callable(model_dump):
        try:
            return _jsonable(model_dump(mode="json"))
        except TypeError:
            return _jsonable(model_dump())
    # Unknown SDK objects are deliberately omitted from crash replay. They
    # usually represent an active socket, future or native message handle.
    return None


def _read_identity_value(payload: Any, keys: tuple[str, ...]) -> str:
    candidates: list[Any] = []
    if isinstance(payload, dict):
        candidates.append(payload)
        meta = payload.get("meta")
        if isinstance(meta, dict):
            candidates.append(meta)
    else:
        candidates.append(payload)
        meta = getattr(payload, "channel_meta", None)
        if isinstance(meta, dict):
            candidates.append(meta)
    for candidate in candidates:
        for key in keys:
            if isinstance(candidate, dict):
                value = candidate.get(key)
            else:
                value = getattr(candidate, key, None)
            if value not in (None, ""):
                return str(value)
    return ""


@dataclass(frozen=True)
class DeliveryRegistration:
    delivery_id: str
    accepted: bool
    status: str


@dataclass(frozen=True)
class DeliveryRecord:
    delivery_id: str
    channel_id: str
    session_id: str
    priority_level: int
    payload: Any
    status: str
    attempts: int
    last_error: str
    created_at: float
    updated_at: float


class ChannelDeliveryLedger:
    """Small SQLite ledger owned by one Agent workspace."""

    def __init__(
        self,
        path: str | Path | None = None,
        *,
        max_attempts: int = 3,
    ) -> None:
        self.path = str(Path(path).expanduser()) if path else ":memory:"
        if self.path != ":memory:":
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self.max_attempts = max(1, int(max_attempts))
        self._lock = threading.RLock()
        self._connection = sqlite3.connect(
            self.path,
            check_same_thread=False,
        )
        self._connection.row_factory = sqlite3.Row
        with self._connection:
            self._connection.execute("PRAGMA journal_mode=WAL")
            self._connection.execute("PRAGMA synchronous=NORMAL")
            self._connection.execute(
                """
                CREATE TABLE IF NOT EXISTS channel_deliveries (
                    delivery_id TEXT PRIMARY KEY,
                    channel_id TEXT NOT NULL,
                    session_id TEXT NOT NULL,
                    priority_level INTEGER NOT NULL,
                    payload_json TEXT,
                    status TEXT NOT NULL,
                    attempts INTEGER NOT NULL DEFAULT 0,
                    last_error TEXT NOT NULL DEFAULT '',
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL
                )
                """,
            )
            self._connection.execute(
                "CREATE TABLE IF NOT EXISTS channel_delivery_notices ("
                "delivery_id TEXT PRIMARY KEY, status TEXT NOT NULL, "
                "updated_at REAL NOT NULL)",
            )
            self._connection.execute(
                "CREATE INDEX IF NOT EXISTS idx_channel_delivery_status "
                "ON channel_deliveries(status, updated_at)",
            )

    def claim_notice(self, delivery_id: str) -> bool:
        """Durably fence one send attempt; an unknown send is never replayed."""
        with self._lock, self._connection:
            cursor = self._connection.execute(
                "INSERT OR IGNORE INTO channel_delivery_notices "
                "(delivery_id, status, updated_at) "
                "SELECT delivery_id, 'unconfirmed', ? FROM channel_deliveries "
                "WHERE delivery_id = ?",
                (time.time(), delivery_id),
            )
        return cursor.rowcount == 1

    def notice_status(self, delivery_id: str) -> str | None:
        with self._lock:
            row = self._connection.execute(
                "SELECT status FROM channel_delivery_notices "
                "WHERE delivery_id = ?", (delivery_id,),
            ).fetchone()
        return str(row["status"]) if row is not None else None

    @staticmethod
    def _delivery_id(channel_id: str, payload: Any) -> str:
        external_id = _read_identity_value(payload, _EXTERNAL_ID_KEYS)
        if not external_id:
            return "delivery_" + uuid4().hex
        account_id = _read_identity_value(payload, _ACCOUNT_ID_KEYS)
        raw = f"{channel_id}:{account_id}:{external_id}".encode("utf-8")
        return "delivery_" + hashlib.sha256(raw).hexdigest()

    @staticmethod
    def _encode_payload(payload: Any) -> Optional[str]:
        try:
            return json.dumps(
                _jsonable(payload),
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            )
        except (TypeError, ValueError, UnicodeError):
            return None

    @staticmethod
    def _row_to_record(row: sqlite3.Row) -> DeliveryRecord:
        payload = None
        if row["payload_json"]:
            try:
                payload = json.loads(row["payload_json"])
            except json.JSONDecodeError:
                payload = None
        return DeliveryRecord(
            delivery_id=row["delivery_id"],
            channel_id=row["channel_id"],
            session_id=row["session_id"],
            priority_level=int(row["priority_level"]),
            payload=payload,
            status=row["status"],
            attempts=int(row["attempts"]),
            last_error=row["last_error"] or "",
            created_at=float(row["created_at"]),
            updated_at=float(row["updated_at"]),
        )

    def register(
        self,
        channel_id: str,
        session_id: str,
        priority_level: int,
        payload: Any,
        *,
        rejection_reason: str | None = None,
    ) -> DeliveryRegistration:
        delivery_id = self._delivery_id(channel_id, payload)
        now = time.time()
        encoded = self._encode_payload(payload)
        initial_status = "dead" if rejection_reason is not None else "pending"
        with self._lock, self._connection:
            existing = self._connection.execute(
                "SELECT status FROM channel_deliveries WHERE delivery_id = ?",
                (delivery_id,),
            ).fetchone()
            if existing is not None:
                return DeliveryRegistration(
                    delivery_id=delivery_id,
                    accepted=False,
                    status=str(existing["status"]),
                )
            self._connection.execute(
                """
                INSERT INTO channel_deliveries (
                    delivery_id, channel_id, session_id, priority_level,
                    payload_json, status, attempts, last_error, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, 0, ?, ?, ?)
                """,
                (
                    delivery_id,
                    channel_id,
                    session_id,
                    int(priority_level),
                    encoded,
                    initial_status,
                    (rejection_reason or "")[:2000],
                    now,
                    now,
                ),
            )
        return DeliveryRegistration(delivery_id, True, initial_status)

    def mark_delivered(self, delivery_id: str) -> None:
        with self._lock, self._connection:
            self._connection.execute(
                "UPDATE channel_deliveries SET status = 'delivered', "
                "last_error = '', updated_at = ? WHERE delivery_id = ?",
                (time.time(), delivery_id),
            )

    def record_failure(self, delivery_id: str, error: str) -> str:
        with self._lock, self._connection:
            row = self._connection.execute(
                "SELECT attempts FROM channel_deliveries "
                "WHERE delivery_id = ?",
                (delivery_id,),
            ).fetchone()
            if row is None:
                raise KeyError(delivery_id)
            attempts = int(row["attempts"]) + 1
            status = "dead" if attempts >= self.max_attempts else "pending"
            self._connection.execute(
                "UPDATE channel_deliveries SET attempts = ?, status = ?, "
                "last_error = ?, updated_at = ? WHERE delivery_id = ?",
                (attempts, status, str(error)[:2000], time.time(), delivery_id),
            )
        return status

    def _list_status(self, status: str) -> list[DeliveryRecord]:
        with self._lock:
            rows = self._connection.execute(
                "SELECT * FROM channel_deliveries WHERE status = ? "
                "ORDER BY created_at ASC",
                (status,),
            ).fetchall()
        return [self._row_to_record(row) for row in rows]

    def list_pending(self) -> list[DeliveryRecord]:
        return self._list_status("pending")

    def list_dead_letters(self) -> list[DeliveryRecord]:
        return self._list_status("dead")

    def requeue(self, delivery_id: str) -> DeliveryRecord:
        with self._lock, self._connection:
            self._connection.execute(
                "UPDATE channel_deliveries SET status = 'pending', attempts = 0, "
                "last_error = '', updated_at = ? WHERE delivery_id = ?",
                (time.time(), delivery_id),
            )
            row = self._connection.execute(
                "SELECT * FROM channel_deliveries WHERE delivery_id = ?",
                (delivery_id,),
            ).fetchone()
        if row is None:
            raise KeyError(delivery_id)
        return self._row_to_record(row)

    def prune_delivered(self, older_than_seconds: float = 7 * 86400) -> int:
        cutoff = time.time() - max(0.0, float(older_than_seconds))
        with self._lock, self._connection:
            self._connection.execute(
                "DELETE FROM channel_delivery_notices WHERE delivery_id IN "
                "(SELECT delivery_id FROM channel_deliveries "
                "WHERE status = 'delivered' AND updated_at < ?)",
                (cutoff,),
            )
            cursor = self._connection.execute(
                "DELETE FROM channel_deliveries "
                "WHERE status = 'delivered' AND updated_at < ?",
                (cutoff,),
            )
        return max(0, int(cursor.rowcount))

    def close(self) -> None:
        with self._lock:
            self._connection.close()
