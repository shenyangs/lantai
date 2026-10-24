# -*- coding: utf-8 -*-
"""Agent statistics service."""

from __future__ import annotations

import asyncio
import logging
import os
import shutil
from collections import defaultdict
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import aiofiles
import orjson

from ..app.experience import ChatExperienceEvent, collect_workspace_events
from ..app.runner.repo import JsonChatRepository
from ..app.runner.session import sanitize_filename
from ..constant import BACKUP_DIR, WORKING_DIR
from ..token_usage import get_token_usage_manager
from .models import (
    AgentStatsSummary,
    ChannelStats,
    DailyStats,
    ModelUsageStats,
    ProviderUsageStats,
    StorageCategoryStats,
    StorageSummary,
    ToolHealthSummary,
    ToolUsageStats,
)

logger = logging.getLogger(__name__)

_STORAGE_CATEGORY_ORDER = (
    "backups",
    "logs_cache",
    "runtime",
    "conversations",
    "memory",
    "media",
    "skills_config",
    "workspace_files",
)
_STORAGE_CATEGORY_LABELS = {
    "backups": "备份与快照",
    "logs_cache": "日志与缓存",
    "runtime": "运行记录",
    "conversations": "会话与任务",
    "memory": "记忆与索引",
    "media": "图片与媒体",
    "skills_config": "配置、技能与模型",
    "workspace_files": "工作区文件",
}
_STORAGE_SCAN_ENTRY_LIMIT = 150_000
_SESSION_FILE_SCAN_LIMIT = 50_000
_TOOL_FINISHED_EVENT_KINDS = {
    "chat.tool_call.finished",
    "chat.tool_result",
}
_TOOL_STARTED_EVENT_KINDS = {
    "chat.tool_call.started",
}


def _event_date_in_range(
    event: ChatExperienceEvent,
    start_date_str: str,
    end_date_str: str,
) -> bool:
    event_date = str(event.ts or "")[:10]
    return start_date_str <= event_date <= end_date_str


def _tool_name_for_event(event: ChatExperienceEvent) -> str:
    payload = event.payload or {}
    for value in (
        payload.get("tool_name"),
        payload.get("name"),
    ):
        if isinstance(value, str) and value.strip():
            return value.strip()
    tool_call = payload.get("tool_call")
    if isinstance(tool_call, dict):
        value = tool_call.get("name")
        if isinstance(value, str) and value.strip():
            return value.strip()
    return "unknown"


def _tool_identity_for_event(event: ChatExperienceEvent) -> str:
    payload = event.payload or {}
    for value in (
        event.item_id,
        payload.get("tool_call_id"),
        payload.get("call_id"),
        payload.get("id"),
    ):
        if isinstance(value, str) and value.strip():
            scope = event.run_id or f"{event.chat_id}:{event.session_id}"
            return f"{scope}:{value.strip()}"
    tool_call = payload.get("tool_call")
    if isinstance(tool_call, dict):
        value = tool_call.get("id") or tool_call.get("tool_call_id")
        if isinstance(value, str) and value.strip():
            scope = event.run_id or f"{event.chat_id}:{event.session_id}"
            return f"{scope}:{value.strip()}"
    return event.id


def _number_from_mapping(
    mapping: dict[str, Any],
    *keys: str,
) -> int:
    for key in keys:
        value = mapping.get(key)
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            return max(0, int(value))
    return 0


def _usage_for_event(event: ChatExperienceEvent) -> dict[str, Any]:
    if isinstance(event.usage, dict):
        return event.usage
    payload = event.payload or {}
    for key in ("usage", "token_usage", "model_usage"):
        value = payload.get(key)
        if isinstance(value, dict):
            return value
    return {}


def _deduplicate_events(
    events: list[ChatExperienceEvent],
) -> list[ChatExperienceEvent]:
    unique: dict[str, ChatExperienceEvent] = {}
    for event in events:
        unique.setdefault(event.id, event)
    return sorted(
        unique.values(),
        key=lambda event: (str(event.ts or ""), event.seq, event.id),
    )


def _runtime_event_rollup(
    events: list[ChatExperienceEvent],
    start_date_str: str,
    end_date_str: str,
) -> tuple[
    dict[str, ChatExperienceEvent],
    dict[str, ChatExperienceEvent],
    int,
]:
    cache_tokens = 0
    started_by_identity: dict[str, ChatExperienceEvent] = {}
    finished_by_identity: dict[str, ChatExperienceEvent] = {}

    for event in _deduplicate_events(events):
        identity = _tool_identity_for_event(event)
        if event.kind in _TOOL_STARTED_EVENT_KINDS:
            started_by_identity[identity] = event

        if not _event_date_in_range(event, start_date_str, end_date_str):
            continue
        usage = _usage_for_event(event)
        cache_tokens += _number_from_mapping(
            usage,
            "cache_tokens",
            "cached_tokens",
            "cache_read_input_tokens",
        )
        if event.kind not in _TOOL_FINISHED_EVENT_KINDS:
            continue
        previous = finished_by_identity.get(identity)
        if previous is None or (
            previous.kind == "chat.tool_result"
            and event.kind == "chat.tool_call.finished"
        ):
            finished_by_identity[identity] = event

    return finished_by_identity, started_by_identity, cache_tokens


def _parse_event_timestamp(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    normalized = value.strip()
    if normalized.endswith("Z"):
        normalized = f"{normalized[:-1]}+00:00"
    try:
        return datetime.fromisoformat(normalized)
    except ValueError:
        return None


def _duration_ms_for_event(
    event: ChatExperienceEvent,
    started_by_identity: dict[str, ChatExperienceEvent],
) -> int | None:
    payload = event.payload or {}
    for mapping in (payload, payload.get("metadata")):
        if not isinstance(mapping, dict):
            continue
        value = mapping.get("duration_ms")
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            return max(0, round(value))

    started = started_by_identity.get(_tool_identity_for_event(event))
    if started is None:
        return None
    started_at = _parse_event_timestamp(started.ts)
    finished_at = _parse_event_timestamp(event.ts)
    if started_at is None or finished_at is None:
        return None
    try:
        elapsed_ms = round((finished_at - started_at).total_seconds() * 1000)
    except TypeError:
        return None
    return max(0, elapsed_ms)


def _runtime_tool_calls_by_date(
    events: list[ChatExperienceEvent],
    start_date_str: str,
    end_date_str: str,
) -> dict[str, int]:
    finished, _, _ = _runtime_event_rollup(
        events,
        start_date_str,
        end_date_str,
    )
    by_date: dict[str, int] = defaultdict(int)
    for event in finished.values():
        date_str = str(event.ts or "")[:10]
        if start_date_str <= date_str <= end_date_str:
            by_date[date_str] += 1
    return dict(by_date)


def _runtime_health_from_events(
    events: list[ChatExperienceEvent],
    start_date_str: str,
    end_date_str: str,
) -> tuple[ToolHealthSummary, int]:
    (
        finished_by_identity,
        started_by_identity,
        cache_tokens,
    ) = _runtime_event_rollup(events, start_date_str, end_date_str)

    rows: dict[str, dict[str, Any]] = defaultdict(
        lambda: {
            "total_calls": 0,
            "completed_calls": 0,
            "failed_calls": 0,
            "interrupted_calls": 0,
            "duration_total": 0,
            "duration_count": 0,
            "last_used_at": None,
            "last_error": None,
        }
    )
    total_duration = 0
    duration_count = 0
    for event in finished_by_identity.values():
        name = _tool_name_for_event(event)
        row = rows[name]
        row["total_calls"] += 1
        payload = event.payload or {}
        is_failed = event.status in {"failed", "error"} or payload.get(
            "ok"
        ) is False
        is_interrupted = event.status in {
            "interrupted",
            "cancelled",
            "canceled",
        }
        if is_interrupted:
            row["interrupted_calls"] += 1
        elif is_failed:
            row["failed_calls"] += 1
        else:
            row["completed_calls"] += 1

        duration_ms = _duration_ms_for_event(event, started_by_identity)
        if duration_ms is not None:
            row["duration_total"] += duration_ms
            row["duration_count"] += 1
            total_duration += duration_ms
            duration_count += 1

        if row["last_used_at"] is None or event.ts > row["last_used_at"]:
            row["last_used_at"] = event.ts
            error = payload.get("error")
            if isinstance(error, str) and error.strip():
                row["last_error"] = error.strip()[:240]

    by_tool = [
        ToolUsageStats(
            name=name,
            total_calls=row["total_calls"],
            completed_calls=row["completed_calls"],
            failed_calls=row["failed_calls"],
            interrupted_calls=row["interrupted_calls"],
            average_duration_ms=(
                round(row["duration_total"] / row["duration_count"])
                if row["duration_count"]
                else 0
            ),
            last_used_at=row["last_used_at"],
            last_error=row["last_error"],
        )
        for name, row in rows.items()
    ]
    by_tool.sort(
        key=lambda item: (
            -item.failed_calls,
            -item.total_calls,
            item.name.lower(),
        )
    )
    completed = sum(item.completed_calls for item in by_tool)
    failed = sum(item.failed_calls for item in by_tool)
    interrupted = sum(item.interrupted_calls for item in by_tool)
    total = sum(item.total_calls for item in by_tool)
    success_rate = round((completed / total) * 100, 1) if total else 0
    return (
        ToolHealthSummary(
            total_calls=total,
            completed_calls=completed,
            failed_calls=failed,
            interrupted_calls=interrupted,
            success_rate=success_rate,
            average_duration_ms=(
                round(total_duration / duration_count)
                if duration_count
                else 0
            ),
            by_tool=by_tool,
        ),
        cache_tokens,
    )


def _safe_workspace_path(root: Path, candidate: Path | str) -> Path | None:
    try:
        resolved_root = root.expanduser().resolve(strict=False)
        path = Path(candidate).expanduser()
        if not path.is_absolute():
            path = resolved_root / path
        resolved = path.resolve(strict=False)
    except (OSError, RuntimeError):
        return None
    if resolved != resolved_root and resolved_root not in resolved.parents:
        return None
    return resolved if resolved.is_dir() else None


def _stats_workspace_dirs(
    workspace_dir: Path,
    chats: list[Any],
) -> list[Path]:
    root = workspace_dir.expanduser().resolve(strict=False)
    paths: set[Path] = {root}

    topic_root = root / "topic-workspaces"
    if topic_root.is_dir():
        try:
            candidates = list(topic_root.iterdir())
        except OSError:
            candidates = []
        for candidate in candidates:
            safe = _safe_workspace_path(root, candidate)
            if safe is not None:
                paths.add(safe)

    for chat in chats:
        meta = getattr(chat, "meta", None)
        if not isinstance(meta, dict):
            continue
        for key in ("topic_workspace_dir", "manual_workspace_dir"):
            raw = meta.get(key)
            if not isinstance(raw, str) or not raw.strip():
                continue
            safe = _safe_workspace_path(root, raw.strip())
            if safe is not None:
                paths.add(safe)

    return [root, *sorted(paths - {root}, key=str)]


def _collect_stats_runtime_events(
    workspace_dir: Path,
    chats: list[Any],
) -> list[ChatExperienceEvent]:
    events: list[ChatExperienceEvent] = []
    for event_workspace in _stats_workspace_dirs(workspace_dir, chats):
        try:
            events.extend(collect_workspace_events(event_workspace))
        except Exception as error:  # pragma: no cover - defensive isolation
            logger.warning(
                "Failed to load runtime statistics from %s: %s",
                event_workspace,
                error,
            )
    return _deduplicate_events(events)


def _discover_session_files(sessions_dir: Path) -> list[Path]:
    root = sessions_dir.expanduser().resolve(strict=False)
    discovered: list[Path] = []
    for current_dir, _, filenames in os.walk(root, followlinks=False):
        for filename in filenames:
            if not filename.endswith(".json"):
                continue
            candidate = Path(current_dir) / filename
            try:
                resolved = candidate.resolve(strict=False)
            except (OSError, RuntimeError):
                continue
            if resolved != root and root not in resolved.parents:
                continue
            discovered.append(resolved)
            if len(discovered) >= _SESSION_FILE_SCAN_LIMIT:
                logger.warning(
                    "Session statistics scan reached the %s file limit",
                    _SESSION_FILE_SCAN_LIMIT,
                )
                return sorted(discovered, key=str)
    return sorted(discovered, key=str)


def _storage_category_for_path(
    relative_path: Path,
    *,
    backup_root: bool = False,
) -> str:
    if backup_root:
        return "backups"
    parts = [part.lower() for part in relative_path.parts]
    if not parts:
        return "workspace_files"
    if any(part in {"logs", "log", "cache", "caches", "tmp"} for part in parts):
        return "logs_cache"
    if any(
        part in {".lantai", "experience", "runtime", "runtime_runs"}
        for part in parts
    ):
        return "runtime"
    if any(part in {"sessions", "tasks", "chats"} for part in parts) or (
        relative_path.name.lower() in {"chats.json", "jobs.json"}
    ):
        return "conversations"
    if any(
        part in {"memory", "memories", "knowledge", "wiki", "indexes"}
        for part in parts
    ):
        return "memory"
    if any(
        part in {"media", "images", "uploads", "attachments"}
        for part in parts
    ) or relative_path.suffix.lower() in {
        ".png",
        ".jpg",
        ".jpeg",
        ".gif",
        ".webp",
        ".svg",
        ".mp3",
        ".wav",
        ".m4a",
        ".mp4",
        ".mov",
    }:
        return "media"
    if any(
        part
        in {
            "skills",
            "plugins",
            "models",
            "local_models",
            "custom_channels",
            "config",
        }
        for part in parts
    ):
        return "skills_config"
    return "workspace_files"


def _scan_storage(
    workspace_dir: Path,
) -> StorageSummary:
    buckets: dict[str, dict[str, int]] = {
        key: {"bytes": 0, "file_count": 0, "directory_count": 0}
        for key in _STORAGE_CATEGORY_ORDER
    }
    entry_count = 0
    truncated = False
    roots = [(WORKING_DIR, False)]
    try:
        backup_is_inside_root = BACKUP_DIR.is_relative_to(WORKING_DIR)
    except (OSError, ValueError):
        backup_is_inside_root = False
    if not backup_is_inside_root:
        roots.append((BACKUP_DIR, True))

    for root, backup_root in roots:
        if not root.exists() or not root.is_dir():
            continue
        for current_root, dir_names, file_names in os.walk(
            root,
            followlinks=False,
        ):
            current_path = Path(current_root)
            dir_names[:] = [
                name
                for name in dir_names
                if not (current_path / name).is_symlink()
            ]
            for directory_name in dir_names:
                relative = (current_path / directory_name).relative_to(root)
                category = _storage_category_for_path(
                    relative,
                    backup_root=backup_root,
                )
                buckets[category]["directory_count"] += 1
                entry_count += 1
                if entry_count >= _STORAGE_SCAN_ENTRY_LIMIT:
                    truncated = True
                    break
            if truncated:
                break
            for file_name in file_names:
                path = current_path / file_name
                if path.is_symlink():
                    continue
                relative = path.relative_to(root)
                category = _storage_category_for_path(
                    relative,
                    backup_root=backup_root,
                )
                try:
                    size = path.stat().st_size
                except OSError:
                    continue
                buckets[category]["bytes"] += size
                buckets[category]["file_count"] += 1
                entry_count += 1
                if entry_count >= _STORAGE_SCAN_ENTRY_LIMIT:
                    truncated = True
                    break
            if truncated:
                break
        if truncated:
            break

    disk_probe = WORKING_DIR if WORKING_DIR.exists() else WORKING_DIR.parent
    try:
        disk = shutil.disk_usage(disk_probe)
        disk_total_bytes = disk.total
        disk_free_bytes = disk.free
    except OSError:
        disk_total_bytes = 0
        disk_free_bytes = 0
    categories = [
        StorageCategoryStats(
            key=key,
            label=_STORAGE_CATEGORY_LABELS[key],
            bytes=buckets[key]["bytes"],
            file_count=buckets[key]["file_count"],
            directory_count=buckets[key]["directory_count"],
            cleanable=key == "logs_cache",
        )
        for key in _STORAGE_CATEGORY_ORDER
    ]
    return StorageSummary(
        root_path=str(WORKING_DIR),
        workspace_path=str(workspace_dir),
        used_bytes=sum(item.bytes for item in categories),
        disk_total_bytes=disk_total_bytes,
        disk_free_bytes=disk_free_bytes,
        file_count=sum(item.file_count for item in categories),
        directory_count=sum(item.directory_count for item in categories),
        scanned_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
        truncated=truncated,
        categories=categories,
    )


# pylint: disable=unused-argument
def _should_skip_by_mtime(
    session_file: Path,
    start_date: date,
    end_date: date,
) -> bool:
    try:
        mtime = session_file.stat().st_mtime
        mtime_date = date.fromtimestamp(mtime)
        if mtime_date < start_date:
            logger.debug(
                "Skipping %s by mtime (%s) before start date %s",
                session_file.name,
                mtime_date.isoformat(),
                start_date.isoformat(),
            )
            return True
    except OSError:
        pass
    return False


def _should_skip_by_content_range(
    session_data: dict,
    start_date_str: str,
    end_date_str: str,
) -> bool:
    memories = (
        session_data.get("agent", {}).get("memory", {}).get("memories")
    ) or session_data.get("agent", {}).get("memory", {}).get("content", [])

    if not memories:
        return True

    timestamps: list[str] = []
    for msg_item in memories:
        if isinstance(msg_item, list) and len(msg_item) > 0:
            msg_data = msg_item[0]
        elif isinstance(msg_item, dict):
            msg_data = msg_item
        else:
            continue

        if not isinstance(msg_data, dict):
            continue

        timestamp = msg_data.get("timestamp")
        if timestamp:
            timestamps.append(str(timestamp)[:10])

    if not timestamps:
        return True

    first_date = min(timestamps)
    last_date = max(timestamps)

    if last_date < start_date_str or first_date > end_date_str:
        logger.debug(
            "Skipping session by content range [%s, %s] "
            "outside target [%s, %s]",
            first_date,
            last_date,
            start_date_str,
            end_date_str,
        )
        return True

    return False


# pylint:disable=too-many-statements,too-many-branches
def _process_session_file(
    session_data: dict,
    start_date_str: str,
    end_date_str: str,
    daily_stats: dict[str, dict],
    channel_stats: dict[str, dict],
    channel: str,
    session_stem: str,
    active_sessions: dict[str, set[str]],
) -> tuple[int, bool]:
    tool_call_count = 0
    has_messages_in_range = False
    try:
        memories = (
            session_data.get("agent", {}).get("memory", {}).get("memories")
        ) or session_data.get("agent", {}).get("memory", {}).get("content", [])

        stats = channel_stats.setdefault(
            channel,
            {
                "session_count": 0,
                "user_messages": 0,
                "assistant_messages": 0,
                "total_messages": 0,
            },
        )

        for msg_item in memories:
            if isinstance(msg_item, list) and len(msg_item) > 0:
                msg_data = msg_item[0]
            elif isinstance(msg_item, dict):
                msg_data = msg_item
            else:
                continue

            if not isinstance(msg_data, dict):
                continue

            timestamp = msg_data.get("timestamp")
            if not timestamp:
                continue

            date_str = str(timestamp)[:10]
            if date_str < start_date_str or date_str > end_date_str:
                continue

            has_messages_in_range = True
            active_sessions.setdefault(date_str, set()).add(session_stem)

            ds = daily_stats[date_str]
            role = msg_data.get("role", "")
            content = msg_data.get("content", [])

            if role == "user":
                ds["user_messages"] += 1
                ds["total_messages"] += 1
                stats["user_messages"] += 1
                stats["total_messages"] += 1
            elif role == "assistant":
                ds["assistant_messages"] += 1
                ds["total_messages"] += 1
                stats["assistant_messages"] += 1
                stats["total_messages"] += 1

            if isinstance(content, list):
                for block in content:
                    if (
                        isinstance(block, dict)
                        and block.get("type") == "tool_use"
                    ):
                        ds["tool_calls"] += 1
                        tool_call_count += 1

    except Exception as e:
        logger.debug("Failed to count messages in session: %s", e)

    if has_messages_in_range and channel in channel_stats:
        channel_stats[channel]["session_count"] += 1

    return tool_call_count, has_messages_in_range


class AgentStatsService:
    """Service for computing agent statistics."""

    # pylint: disable=R0912,R0915
    async def get_summary(
        self,
        workspace_dir: Path,
        start_date: date,
        end_date: date,
    ) -> AgentStatsSummary:
        chats_file = workspace_dir / "chats.json"
        sessions_dir = workspace_dir / "sessions"

        daily_stats: dict[str, dict] = {}
        days = (end_date - start_date).days + 1
        for i in range(days):
            date_str = (start_date + timedelta(days=i)).isoformat()
            daily_stats[date_str] = {
                "date": date_str,
                "chats": 0,
                "active_sessions": 0,
                "user_messages": 0,
                "assistant_messages": 0,
                "total_messages": 0,
                "prompt_tokens": 0,
                "completion_tokens": 0,
                "llm_calls": 0,
                "tool_calls": 0,
            }

        start_date_str = start_date.isoformat()
        end_date_str = end_date.isoformat()

        channel_stats: dict[str, dict] = {}
        total_tool_calls = 0
        active_sessions: dict[str, set[str]] = {}
        total_active_sessions = 0

        session_file_to_channel: dict[str, str] = {}
        chats: list[Any] = []
        if chats_file.exists():
            try:
                repo = JsonChatRepository(chats_file)
                chats = await repo.list_chats()
                for chat in chats:
                    ch = chat.channel or "console"
                    safe_sid = sanitize_filename(chat.session_id)
                    safe_uid = (
                        sanitize_filename(chat.user_id) if chat.user_id else ""
                    )
                    session_file_to_channel[safe_sid] = ch
                    if safe_uid:
                        session_file_to_channel[f"{safe_uid}_{safe_sid}"] = ch
                    if chat.created_at is None:
                        continue
                    chat_date = chat.created_at.date()
                    if start_date <= chat_date <= end_date:
                        date_str = chat_date.isoformat()
                        daily_stats[date_str]["chats"] += 1
            except Exception as e:
                logger.warning("Failed to load chat statistics: %s", e)

        if sessions_dir.exists():
            try:
                session_files = await asyncio.to_thread(
                    _discover_session_files,
                    sessions_dir,
                )

                session_fd_sem = asyncio.Semaphore((os.cpu_count() or 4) * 2)

                async def _process_one(session_file: Path) -> tuple[int, bool]:
                    async with session_fd_sem:
                        if _should_skip_by_mtime(
                            session_file,
                            start_date,
                            end_date,
                        ):
                            return 0, False

                        try:
                            async with aiofiles.open(
                                session_file,
                                "r",
                                encoding="utf-8",
                            ) as f:
                                session_data = orjson.loads(await f.read())
                        except Exception as e:
                            logger.debug(
                                "Failed to read session file %s: %s",
                                session_file,
                                e,
                            )
                            return 0, False

                        if _should_skip_by_content_range(
                            session_data,
                            start_date_str,
                            end_date_str,
                        ):
                            return 0, False

                        stem = session_file.stem
                        channel = session_file_to_channel.get(stem, "console")
                        try:
                            session_identity = str(
                                session_file.relative_to(sessions_dir).with_suffix(
                                    ""
                                )
                            )
                        except ValueError:
                            session_identity = str(session_file)

                        return _process_session_file(
                            session_data,
                            start_date_str,
                            end_date_str,
                            daily_stats,
                            channel_stats,
                            channel,
                            session_identity,
                            active_sessions,
                        )

                tasks = [_process_one(sf) for sf in session_files]
                results = await asyncio.gather(*tasks, return_exceptions=True)
                for result in results:
                    if isinstance(result, tuple) and len(result) == 2:
                        tool_calls, has_messages = result
                        total_tool_calls += tool_calls
                        if has_messages:
                            total_active_sessions += 1
                    elif isinstance(result, Exception):
                        logger.debug("Failed to process session: %s", result)
            except Exception as e:
                logger.warning("Failed to load message statistics: %s", e)

        token_manager = get_token_usage_manager()
        (
            token_summary,
            token_details,
            runtime_events,
            storage_summary,
        ) = await asyncio.gather(
            token_manager.get_summary(
                start_date=start_date,
                end_date=end_date,
            ),
            token_manager.get_details(
                start_date=start_date,
                end_date=end_date,
            ),
            asyncio.to_thread(
                _collect_stats_runtime_events,
                workspace_dir,
                chats,
            ),
            asyncio.to_thread(_scan_storage, workspace_dir),
        )
        tool_health, total_cache_tokens = _runtime_health_from_events(
            runtime_events,
            start_date_str,
            end_date_str,
        )
        total_tool_calls = max(total_tool_calls, tool_health.total_calls)
        for date_str, count in _runtime_tool_calls_by_date(
            runtime_events,
            start_date_str,
            end_date_str,
        ).items():
            if date_str in daily_stats:
                daily_stats[date_str]["tool_calls"] = max(
                    daily_stats[date_str]["tool_calls"],
                    count,
                )

        provider_rows: dict[str, dict[str, Any]] = defaultdict(
            lambda: {
                "models": set(),
                "prompt_tokens": 0,
                "completion_tokens": 0,
                "call_count": 0,
            }
        )
        model_rows: dict[tuple[str, str], dict[str, int]] = defaultdict(
            lambda: {
                "prompt_tokens": 0,
                "completion_tokens": 0,
                "call_count": 0,
            }
        )
        for record in token_details:
            provider_id = record.provider_id or "unknown"
            provider_row = provider_rows[provider_id]
            provider_row["models"].add(record.model)
            provider_row["prompt_tokens"] += record.prompt_tokens
            provider_row["completion_tokens"] += record.completion_tokens
            provider_row["call_count"] += record.call_count

            model_row = model_rows[(provider_id, record.model)]
            model_row["prompt_tokens"] += record.prompt_tokens
            model_row["completion_tokens"] += record.completion_tokens
            model_row["call_count"] += record.call_count

        provider_stats = [
            ProviderUsageStats(
                provider_id=provider_id,
                model_count=len(row["models"]),
                prompt_tokens=row["prompt_tokens"],
                completion_tokens=row["completion_tokens"],
                total_tokens=(
                    row["prompt_tokens"] + row["completion_tokens"]
                ),
                call_count=row["call_count"],
            )
            for provider_id, row in provider_rows.items()
        ]
        provider_stats.sort(
            key=lambda item: (-item.total_tokens, item.provider_id)
        )
        model_stats = [
            ModelUsageStats(
                provider_id=provider_id,
                model=model,
                prompt_tokens=row["prompt_tokens"],
                completion_tokens=row["completion_tokens"],
                total_tokens=(
                    row["prompt_tokens"] + row["completion_tokens"]
                ),
                call_count=row["call_count"],
            )
            for (provider_id, model), row in model_rows.items()
        ]
        model_stats.sort(
            key=lambda item: (
                -item.total_tokens,
                item.provider_id,
                item.model,
            )
        )
        for date_str, ts in token_summary.by_date.items():
            if date_str in daily_stats:
                daily_stats[date_str]["prompt_tokens"] = ts.prompt_tokens
                daily_stats[date_str][
                    "completion_tokens"
                ] = ts.completion_tokens
                daily_stats[date_str]["llm_calls"] = ts.call_count

        for date_str, session_set in active_sessions.items():
            if date_str in daily_stats:
                daily_stats[date_str]["active_sessions"] = len(session_set)

        by_date = [daily_stats[d] for d in sorted(daily_stats.keys())]

        total_user_messages = sum(ds["user_messages"] for ds in by_date)
        total_assistant_messages = sum(
            ds["assistant_messages"] for ds in by_date
        )
        total_messages = total_user_messages + total_assistant_messages
        cache_denominator = (
            token_summary.total_prompt_tokens + total_cache_tokens
        )

        return AgentStatsSummary(
            total_active_sessions=total_active_sessions,
            total_messages=total_messages,
            total_user_messages=total_user_messages,
            total_assistant_messages=total_assistant_messages,
            total_prompt_tokens=token_summary.total_prompt_tokens,
            total_completion_tokens=token_summary.total_completion_tokens,
            total_llm_calls=token_summary.total_calls,
            total_tool_calls=total_tool_calls,
            total_cache_tokens=total_cache_tokens,
            cache_reuse_rate=(
                round((total_cache_tokens / cache_denominator) * 100, 1)
                if cache_denominator
                else 0
            ),
            provider_stats=provider_stats,
            model_stats=model_stats,
            tool_health=tool_health,
            storage=storage_summary,
            by_date=[DailyStats.model_validate(ds) for ds in by_date],
            channel_stats=[
                ChannelStats(
                    channel=ch,
                    session_count=cnts["session_count"],
                    user_messages=cnts["user_messages"],
                    assistant_messages=cnts["assistant_messages"],
                    total_messages=cnts["total_messages"],
                )
                for ch, cnts in sorted(channel_stats.items())
            ],
            start_date=start_date_str,
            end_date=end_date_str,
        )


_agent_stats_service: AgentStatsService | None = None


def get_agent_stats_service() -> AgentStatsService:
    global _agent_stats_service
    if _agent_stats_service is None:
        _agent_stats_service = AgentStatsService()
    return _agent_stats_service
