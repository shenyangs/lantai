# -*- coding: utf-8 -*-
"""Chat management API."""
from __future__ import annotations
import asyncio
from collections import OrderedDict
from contextlib import closing
from copy import deepcopy
import hashlib
import json
import logging
from decimal import Decimal, InvalidOperation
from typing import Any, Literal, Optional
from uuid import uuid4
from datetime import datetime, timezone
from pathlib import Path
from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request
from starlette.responses import StreamingResponse
from pydantic import BaseModel

from .session import SafeJSONSession
from .manager import ChatManager
from .conversation_identity import (
    LEGACY_RECOVERY_PROVENANCE_META_KEY,
    LEGACY_SOURCE_REFERENCE_META_KEY,
    SESSION_STATE_ID_META_KEY,
    STATE_MIGRATION_STATE_META_KEY,
    canonical_chat_session_state_id,
    new_chat_meta,
    resolve_chat_session_state_id,
    session_state_id_from_known_chats,
)
from .task_tracker import read_control_snapshot
from .models import (
    AnalysisCleanupJob,
    ChatSpec,
    ChatUpdate,
    ChatHistory,
)
from .runtime_state import (
    build_chat_runtime_state,
    pending_turn_dict,
    pending_turn_text,
    resolve_canonical_chat_runtime_identity,
)
from .stall_watchdog import (
    mark_chat_stalled_if_needed,
    reconcile_pending_chat_stall,
    settle_orphaned_pending_turn_on_stop,
)
from .title_generator import _legacy_semantic_title_repair
from ...analysis import AnalysisError, AnalysisService
from ...config.context import WorkspaceContext, current_workspace_context
from ..workspace_context import WorkspaceContextError, resolve_workspace_context
from ...runtime.converters import (
    legacy_empty_plan_notebook_state,
    legacy_msg_to_chat_messages,
    legacy_memory_state_to_chat_messages,
    runtime_event_to_legacy_stream_item,
)
from ...runtime.events import RuntimeEvent
from ...runtime.public_reasoning import (
    PublicReasoningProjector,
    is_runtime_public_progress,
    without_runtime_public_progress_claims,
)
from ..experience import (
    build_runtime_events_response,
    iter_runtime_events_through_seq,
    build_turn_event_preview,
    record_chat_turn_started,
    record_chat_turn_stop_requested,
    resolve_effective_workspace_dir_from_meta,
    runtime_event_stream_generation,
    wait_for_runtime_event_append,
)
from ..channels.schema import DEFAULT_CHANNEL
from ..channels.runtime_schema import (
    Message,
    MessageType,
    Role,
    RunStatus,
    TextContent,
)
from ..workspace_grants import (
    WorkspaceBinding,
    WorkspaceGrantError,
    WorkspaceGrantRegistry,
    default_workspace_grant_registry,
    workspace_grant_http_exception,
)
from ..ququ_continuations import (
    completed_ququ_continuations,
    copy_ququ_continuations_for_branch,
    without_ququ_continuation_storage,
)


router = APIRouter(prefix="/chats", tags=["chats"])
logger = logging.getLogger(__name__)


def _public_chat_spec(spec: ChatSpec) -> ChatSpec:
    """Remove server-owned continuation state from general chat responses."""

    return spec.model_copy(
        update={"meta": without_ququ_continuation_storage(spec.meta)},
    )


class ChatSearchHit(BaseModel):
    """A stable user-visible chat search result."""

    id: str
    kind: Literal["title", "message"]
    chat_id: str
    chat_name: str
    message_id: str | None = None
    role: str = ""
    excerpt: str
    timestamp: datetime | None = None
    archived: bool = False
    coding: bool = False


ChatSearchDocument = tuple[str, str, str, datetime | None]
ChatSearchCacheKey = tuple[str, str, str]
_CHAT_SEARCH_INDEX: OrderedDict[
    ChatSearchCacheKey, list[ChatSearchDocument]
] = OrderedDict()
_CHAT_SEARCH_INFLIGHT: dict[
    ChatSearchCacheKey, asyncio.Task[list[ChatSearchDocument]]
] = {}
_CHAT_SEARCH_INDEX_LOCK = asyncio.Lock()
_CHAT_SEARCH_INDEX_MAX_ENTRIES = 128
_CHAT_SEARCH_INDEX_MAX_CHARACTERS = 16_000_000

TOPIC_WORKSPACES_DIR = "topic-workspaces"
TOPIC_WORKSPACE_META_KEY = "topic_workspace_dir"
MANUAL_WORKSPACE_META_KEY = "manual_workspace_dir"
WORKSPACE_GRANT_META_KEY = "workspace_grant_id"
WORKSPACE_REAUTH_REQUIRED_META_KEY = "workspace_reauthorization_required"
WORKSPACE_REAUTH_NAME_META_KEY = "workspace_reauthorization_name"
WORKSPACE_LEGACY_FINGERPRINT_META_KEY = "workspace_legacy_path_fingerprint"
EXTERNAL_ATTACHMENT_PATHS_META_KEY = "external_attachment_paths"
CODING_CHAT_SESSION_PREFIX = "console:coding:"
_TERMINAL_LIST_PENDING_STATUSES = {
    "cancelled",
    "canceled",
    "completed",
    "failed",
    "finished",
    "interrupted",
    "stalled",
}
_BLANK_CHAT_NAMES = {"", "new chat", "新会话", "chat.newsessiontitle"}
RUNTIME_EVENT_STREAM_HEARTBEAT_SECONDS = 15.0
BRANCH_RUNTIME_EVENT_LINEAGE_META_KEY = "_branch_runtime_event_lineage_v1"
_BRANCH_RUNTIME_EVENT_LINEAGE_MAX_DEPTH = 16


def _is_truthy_coding_mode(value: object) -> bool:
    if value is True:
        return True
    if isinstance(value, str):
        return value.strip().lower() in {
            "1",
            "true",
            "yes",
            "y",
            "on",
            "coding",
            "coding_mode",
        }
    return False


def is_coding_chat_spec(chat: ChatSpec) -> bool:
    """Return whether a chat belongs to the Coding surface."""
    meta = chat.meta or {}
    return (
        _is_truthy_coding_mode(meta.get("coding_mode"))
        or chat.session_id.startswith(CODING_CHAT_SESSION_PREFIX)
        or (
            isinstance(meta.get("coding_context_snapshot"), str)
            and bool(str(meta.get("coding_context_snapshot")).strip())
        )
        or (chat.name or "").startswith("Coding ·")
    )


def _needs_list_stall_probe(chat: ChatSpec) -> bool:
    """Return whether list hydration must inspect this chat's event ledger."""

    pending = pending_turn_dict(chat)
    if not pending:
        return False
    status = str(pending.get("status") or "").strip().lower()
    if status in _TERMINAL_LIST_PENDING_STATUSES:
        return False
    return bool(pending_turn_text(pending) or pending.get("run_id"))


def _is_blank_chat(chat: ChatSpec) -> bool:
    """Return whether a persisted chat has never accepted a user turn.

    Topic workspaces can be allocated before the first send. Those rows are
    implementation details, not useful history entries, so the normal list
    hides them without deleting data. Explicitly named or pinned chats remain
    visible even before their first turn.
    """

    if chat.pinned or is_coding_chat_spec(chat):
        return False
    if str(chat.name or "").strip().lower() not in _BLANK_CHAT_NAMES:
        return False
    meta = chat.meta if isinstance(chat.meta, dict) else {}
    durable_turn_markers = (
        "last_turn_text",
        "last_result_summary",
        "last_turn_status",
        "last_turn_run_id",
        "pending_turn",
    )
    return not any(meta.get(key) for key in durable_turn_markers)


async def _load_turn_event_preview(
    chat_spec: ChatSpec,
    *,
    root_workspace_dir: Path | str | None = None,
):
    effective_workspace_dir = resolve_effective_workspace_dir_from_meta(
        chat_spec.meta or {},
        root_workspace_dir=root_workspace_dir,
    )
    if effective_workspace_dir is None:
        return None
    return await asyncio.to_thread(
        build_turn_event_preview,
        effective_workspace_dir,
        chat_spec.id,
    )


def _message_control_identity(message: Any) -> tuple[str, str] | None:
    message_type = str(getattr(message, "type", "") or "")
    phase = {
        "plugin_call": "tool.call.started",
        "plugin_call_output": "tool.call.completed",
    }.get(message_type)
    if phase is None:
        return None
    for content in getattr(message, "content", None) or []:
        data = getattr(content, "data", None)
        if not isinstance(data, dict):
            continue
        call_id = str(data.get("call_id") or data.get("id") or "").strip()
        if call_id:
            return phase, call_id
    return None


def _message_timestamp(value: Any) -> float | None:
    if isinstance(value, (int, float)):
        return float(value)
    text = str(value or "").strip()
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.astimezone()
    return parsed.timestamp()


def _message_client_turn_id(message: Any) -> str:
    """Read the logical turn id through the legacy API metadata envelope."""

    metadata = getattr(message, "metadata", None)
    if not isinstance(metadata, dict):
        return ""
    for container in (metadata, metadata.get("metadata")):
        if not isinstance(container, dict):
            continue
        value = str(container.get("client_turn_id") or "").strip()
        if value:
            return value
    return ""


def _attach_latest_user_client_turn_id(
    messages: list[Any],
    latest_client_turn_id: str,
) -> list[Any]:
    """Restore accepted turn identities omitted by legacy user memory.

    Native assistant rows and the event ledger retain ``client_turn_id``, but
    older user-memory rows do not. Backfill each user from the first identified
    response in its causal segment, then use chat metadata as a fallback for
    the latest accepted turn. This lets the renderer reconcile optimistic
    prompts with durable history instead of displaying them twice.
    """

    for index, message in enumerate(messages):
        if (
            str(getattr(message, "role", "")) != "user"
            or _message_client_turn_id(message)
        ):
            continue
        causal_turn_id = ""
        for following in messages[index + 1 :]:
            if str(getattr(following, "role", "")) == "user":
                break
            causal_turn_id = _message_client_turn_id(following)
            if causal_turn_id:
                break
        if causal_turn_id:
            message.metadata = {
                **dict(getattr(message, "metadata", None) or {}),
                "client_turn_id": causal_turn_id,
            }

    turn_id = latest_client_turn_id.strip()
    if not turn_id or any(
        str(getattr(message, "role", "")) == "user"
        and _message_client_turn_id(message) == turn_id
        for message in messages
    ):
        return messages

    first_turn_index = next(
        (
            index
            for index, message in enumerate(messages)
            if _message_client_turn_id(message) == turn_id
        ),
        len(messages),
    )
    target = next(
        (
            message
            for message in reversed(messages[:first_turn_index])
            if str(getattr(message, "role", "")) == "user"
        ),
        None,
    )
    if target is None:
        target = next(
            (
                message
                for message in reversed(messages)
                if str(getattr(message, "role", "")) == "user"
            ),
            None,
        )
    if target is None:
        return messages

    target.metadata = {
        **dict(getattr(target, "metadata", None) or {}),
        "client_turn_id": turn_id,
    }
    return messages


def _message_source_id(message: Any) -> str:
    """Read the provider message identity retained by legacy conversion."""

    metadata = getattr(message, "metadata", None)
    if not isinstance(metadata, dict):
        return ""
    containers = tuple(
        container
        for container in (metadata, metadata.get("metadata"))
        if isinstance(container, dict)
    )
    # The channel-facing ``original_id`` may be a reasoning/message-suffixed
    # rendering id. Prefer Runtime's explicit provider source across both
    # metadata layers before falling back to that transport identity.
    for key in ("source_message_id", "runtime_item_id", "original_id"):
        for container in containers:
            value = str(container.get(key) or "").strip()
            if value:
                return value
    return ""


def _message_plain_text(message: Any) -> str:
    """Return one projected row's visible text without inspecting internals."""

    return "\n".join(
        str(getattr(content, "text", "") or "").strip()
        for content in (getattr(message, "content", None) or [])
        if str(getattr(content, "text", "") or "").strip()
    ).strip()


def _base_runtime_message_source_id(value: Any) -> str:
    """Normalize channel-suffixed Native message ids to provider identity."""

    normalized = str(value or "").strip()
    for suffix in (":message", ":reasoning"):
        if normalized.endswith(suffix):
            return normalized.removesuffix(suffix)
    return normalized


def _project_missing_completed_narration(
    memory_messages: list[Any],
    runtime_events: list[RuntimeEvent],
) -> list[tuple[RuntimeEvent, list[Any]]]:
    """Restore completed ordinary assistant rows absent from legacy memory.

    The direct stream and Runtime ledger can contain narration between
    reasoning and tool calls while the legacy memory snapshot only flushes the
    final answer.  A completed row is durable conversation history, not an
    ephemeral loading label.  Match existing rows by provider source first and
    fall back to turn plus visible text for older snapshots that lack source
    metadata; unmatched rows are projected back in ledger order.
    """

    consumed_memory_indices: set[int] = set()
    projected: list[tuple[RuntimeEvent, list[Any]]] = []
    for event in sorted(runtime_events, key=lambda item: (item.seq, item.id)):
        if event.type != "message.completed":
            continue
        legacy_item = runtime_event_to_legacy_stream_item(event)
        if legacy_item is None:
            continue
        legacy_msg = legacy_item[0]
        legacy_msg.timestamp = event.created_at
        event_messages = [
            message
            for message in legacy_msg_to_chat_messages(legacy_msg)
            if str(getattr(message, "type", "") or "") == "message"
            and str(getattr(message, "role", "") or "") == "assistant"
            and _message_plain_text(message)
        ]
        if not event_messages:
            continue

        for projected_message in event_messages:
            projected_source_id = _base_runtime_message_source_id(
                _message_source_id(projected_message)
            )
            projected_turn_id = _message_client_turn_id(
                projected_message
            ) or _runtime_event_client_turn_id(event)
            projected_text = _message_plain_text(projected_message)

            exact_source_match = next(
                (
                    index
                    for index, memory_message in enumerate(memory_messages)
                    if index not in consumed_memory_indices
                    and str(getattr(memory_message, "type", "") or "")
                    == "message"
                    and str(getattr(memory_message, "role", "") or "")
                    == "assistant"
                    and projected_source_id
                    and _base_runtime_message_source_id(
                        _message_source_id(memory_message)
                    )
                    == projected_source_id
                ),
                None,
            )
            fallback_match = next(
                (
                    index
                    for index, memory_message in enumerate(memory_messages)
                    if index not in consumed_memory_indices
                    and str(getattr(memory_message, "type", "") or "")
                    == "message"
                    and str(getattr(memory_message, "role", "") or "")
                    == "assistant"
                    and _message_plain_text(memory_message) == projected_text
                    and (
                        not projected_turn_id
                        or not _message_client_turn_id(memory_message)
                        or _message_client_turn_id(memory_message)
                        == projected_turn_id
                    )
                ),
                None,
            )
            represented_index = (
                exact_source_match
                if exact_source_match is not None
                else fallback_match
            )
            if represented_index is not None:
                consumed_memory_indices.add(represented_index)
                continue

            projected_message.metadata = dict(
                getattr(projected_message, "metadata", None) or {}
            )
            projected_message.metadata.update(
                {
                    "runtime_event_id": event.id,
                    "runtime_event_type": event.type,
                    "runtime_turn_id": event.turn_id,
                    "runtime_item_id": event.item_id,
                    "source_message_id": projected_source_id,
                    "client_turn_id": projected_turn_id,
                    "canonical_history_timeline": True,
                }
            )
            projected.append((event, [projected_message]))
    return projected


def _project_missing_terminal_answers(
    memory_messages: list[Any],
    runtime_events: list[RuntimeEvent],
) -> list[tuple[RuntimeEvent, list[Any]]]:
    """Restore the canonical terminal reply recorded by turn finalization.

    Legacy memory and the Runtime event ledger are persisted independently.
    If the final memory flush loses a race with session reload, the transcript
    can otherwise stop at progress narration even though the run is complete.
    The finalizer's event is the durable authority for that last visible row.
    """

    projected: list[tuple[RuntimeEvent, list[Any]]] = []
    ordered_events = sorted(runtime_events, key=lambda item: (item.seq, item.id))
    for event in ordered_events:
        payload = event.payload if isinstance(event.payload, dict) else {}
        if (
            str(payload.get("legacy_kind") or "") != "chat.final_answer"
            or str(event.status or "") != "completed"
        ):
            continue
        client_turn_id = _runtime_event_client_turn_id(event)
        terminal_text = str(payload.get("text") or "").strip()
        terminal_source_id = ""
        if not terminal_text:
            preceding_events = [
                candidate
                for candidate in ordered_events
                if candidate.seq < event.seq
                and (
                    not event.turn_id
                    or not candidate.turn_id
                    or candidate.turn_id == event.turn_id
                )
            ]
            last_tool_seq = max(
                (
                    candidate.seq
                    for candidate in preceding_events
                    if candidate.type
                    in {"tool.call.started", "tool.call.completed"}
                ),
                default=0,
            )
            for candidate in reversed(preceding_events):
                if candidate.type != "message.completed":
                    continue
                candidate_turn_id = _runtime_event_client_turn_id(candidate)
                if (
                    client_turn_id
                    and candidate_turn_id
                    and candidate_turn_id != client_turn_id
                ):
                    continue
                legacy_item = runtime_event_to_legacy_stream_item(candidate)
                if legacy_item is None:
                    continue
                candidate_messages = legacy_msg_to_chat_messages(legacy_item[0])
                candidate_text = next(
                    (
                        _message_plain_text(message)
                        for message in candidate_messages
                        if str(getattr(message, "type", "") or "") == "message"
                        and str(getattr(message, "role", "") or "")
                        == "assistant"
                        and _message_plain_text(message)
                        not in {"", "回复完成", "Response completed"}
                    ),
                    "",
                )
                # Artifact turns often emit progress before their last tool.
                # Only a message after that effect can be the terminal reply.
                if not candidate_text or candidate.seq <= last_tool_seq:
                    continue
                terminal_text = candidate_text
                terminal_source_id = _base_runtime_message_source_id(
                    _message_source_id(candidate_messages[0])
                )
                client_turn_id = client_turn_id or candidate_turn_id
                break
        represented = None
        if terminal_text:
            represented = next(
                (
                    message
                    for message in memory_messages
                    if str(getattr(message, "type", "") or "") == "message"
                    and str(getattr(message, "role", "") or "") == "assistant"
                    and (
                        _message_plain_text(message) == terminal_text
                        or (
                            terminal_source_id
                            and _base_runtime_message_source_id(
                                _message_source_id(message)
                            )
                            == terminal_source_id
                        )
                    )
                    and (
                        not client_turn_id
                        or not _message_client_turn_id(message)
                        or _message_client_turn_id(message) == client_turn_id
                    )
                ),
                None,
            )
        if not terminal_text:
            continue
        terminal_authority_metadata = {
            "canonical_terminal_event_id": event.id,
            "canonical_terminal_event_type": event.type,
            "client_turn_id": client_turn_id,
            "canonical_history_timeline": True,
            "canonical_terminal_reply": True,
        }
        if represented is not None:
            # The finalizer proves that this existing message is the terminal
            # reply, but it does not change when that message was emitted.
            # Replacing the source message's runtime position with the later
            # finalizer event makes accepted in-run guidance jump ahead of an
            # answer that was already visible.  Keep authority and chronology
            # as separate facts.
            represented.metadata = {
                **dict(getattr(represented, "metadata", None) or {}),
                **terminal_authority_metadata,
            }
            continue
        terminal_metadata = {
            "runtime_event_id": event.id,
            "runtime_event_type": event.type,
            "runtime_turn_id": event.turn_id,
            "runtime_item_id": event.item_id,
            **terminal_authority_metadata,
        }
        terminal_message = Message(
            id=f"runtime-terminal:{event.id}",
            type=MessageType.MESSAGE,
            role=Role.ASSISTANT,
            status=RunStatus.Completed,
            created_at=event.created_at,
            content=[
                TextContent(
                    text=terminal_text,
                )
            ],
            metadata=terminal_metadata,
        )
        projected.append((event, [terminal_message]))
    return projected


def _runtime_event_client_turn_id(event: RuntimeEvent) -> str:
    """Read a logical turn id from every supported event envelope."""

    payload = event.payload if isinstance(event.payload, dict) else {}
    candidates: list[Any] = [
        payload,
        payload.get("details"),
        payload.get("task_contract"),
    ]
    raw_message = payload.get("message")
    if isinstance(raw_message, dict):
        candidates.extend(
            [
                raw_message.get("metadata"),
                raw_message.get("metadata", {}).get("metadata"),
            ]
            if isinstance(raw_message.get("metadata"), dict)
            else []
        )
    for candidate in candidates:
        if not isinstance(candidate, dict):
            continue
        value = str(candidate.get("client_turn_id") or "").strip()
        if value:
            return value
    return ""


def _message_runtime_seq(
    message: Any,
    runtime_events: list[RuntimeEvent],
) -> int | None:
    """Resolve one persisted message channel to its authoritative ledger seq."""

    source_id = _message_source_id(message)
    if not source_id:
        return None
    message_type = str(getattr(message, "type", "") or "")
    channel = "reasoning" if message_type == "reasoning" else "message"
    expected_item_id = f"{source_id}:{channel}"
    expected_block_type = "thinking" if channel == "reasoning" else "text"
    for event in runtime_events:
        if event.type != "message.completed":
            continue
        item_id = str(event.item_id or "").strip()
        payload = event.payload if isinstance(event.payload, dict) else {}
        event_source_id = str(payload.get("source_message_id") or "").strip()
        raw_message = payload.get("message")
        raw_content = (
            raw_message.get("content")
            if isinstance(raw_message, dict)
            and isinstance(raw_message.get("content"), list)
            else []
        )
        has_channel = any(
            isinstance(block, dict)
            and str(block.get("type") or "").strip().lower()
            == expected_block_type
            for block in raw_content
        )
        exact_channel = item_id == expected_item_id
        shared_message = (
            item_id == source_id or event_source_id == source_id
        ) and has_channel
        suffixed_source = event_source_id == source_id and item_id.endswith(
            f":{channel}"
        )
        if exact_channel or shared_message or suffixed_source:
            return event.seq
    return None


def _runtime_event_sequence_by_id(
    runtime_events: list[RuntimeEvent],
) -> dict[str, int]:
    """Index the durable event ledger without relying on wall-clock time."""

    return {
        str(event.id): event.seq
        for event in runtime_events
        if str(event.id).strip()
    }


def _projected_message_runtime_seq(
    message: Any,
    runtime_events: list[RuntimeEvent],
    *,
    event_sequence_by_id: dict[str, int],
) -> int | None:
    """Read a projected row's sequence from its ledger event or source row."""

    metadata = getattr(message, "metadata", None)
    if isinstance(metadata, dict):
        for container in (metadata, metadata.get("metadata")):
            if not isinstance(container, dict):
                continue
            event_id = str(container.get("runtime_event_id") or "").strip()
            if event_id and event_id in event_sequence_by_id:
                return event_sequence_by_id[event_id]
    return _message_runtime_seq(message, runtime_events)


def _restore_persisted_steer_message_positions(
    messages: list[Any],
    runtime_events: list[RuntimeEvent],
) -> list[Any]:
    """Place accepted in-run guidance at its recorded runtime boundary.

    Native execution persists a steer prompt once the Run Inbox consumes it.
    Depending on which write wins the legacy-memory race, that user row can
    otherwise be restored before all earlier tool/process output.  The Runtime
    ledger already records the exact consumption sequence, so repair only rows
    identified by that causal key; ordinary historical user messages stay in
    their stored order.
    """

    steer_event_seq_by_turn_id: dict[str, int] = {}
    for event in runtime_events:
        payload = event.payload if isinstance(event.payload, dict) else {}
        if (
            event.type != "runtime.warning"
            or str(payload.get("kind") or "").strip() != "run_inbox_consumed"
        ):
            continue
        client_turn_id = str(payload.get("client_turn_id") or "").strip()
        if client_turn_id:
            steer_event_seq_by_turn_id[client_turn_id] = event.seq
    if not steer_event_seq_by_turn_id:
        return messages

    steer_rows: list[tuple[int, int, Any]] = []
    remaining: list[Any] = []
    for index, message in enumerate(messages):
        client_turn_id = _message_client_turn_id(message)
        steer_seq = steer_event_seq_by_turn_id.get(client_turn_id)
        if (
            str(getattr(message, "role", "")) == "user"
            and steer_seq is not None
        ):
            steer_rows.append((steer_seq, index, message))
        else:
            remaining.append(message)
    if not steer_rows:
        return messages

    event_sequence_by_id = _runtime_event_sequence_by_id(runtime_events)
    restored = list(remaining)
    for steer_seq, _original_index, steer_message in sorted(steer_rows):
        insertion_index = next(
            (
                index
                for index, message in enumerate(restored)
                if (
                    message_seq := _projected_message_runtime_seq(
                        message,
                        runtime_events,
                        event_sequence_by_id=event_sequence_by_id,
                    )
                )
                is not None
                and message_seq > steer_seq
            ),
            len(restored),
        )
        restored.insert(insertion_index, steer_message)
    return restored


def _merge_persisted_runtime_control_messages(
    memory_messages: list[Any],
    runtime_events: list[RuntimeEvent],
    *,
    latest_client_turn_id: str = "",
) -> list[Any]:
    """Restore the durable assistant timeline without breaking turn order.

    A tool-start event can be appended before the accepted user message is
    flushed to legacy memory. Wall-clock sorting would then put the tool card
    above the prompt that caused it. ``client_turn_id`` is the causal order:
    completed narration and controls for a known turn follow ledger sequence,
    regardless of persistence latency or timezone representation.
    """

    existing = {
        identity
        for message in memory_messages
        if (identity := _message_control_identity(message)) is not None
    }
    projected_events = _project_missing_completed_narration(
        memory_messages,
        runtime_events,
    )
    projected_events.extend(
        _project_missing_terminal_answers(memory_messages, runtime_events)
    )
    for event in runtime_events:
        if event.type not in {"tool.call.started", "tool.call.completed"}:
            continue
        call_id = str(
            event.payload.get("id")
            or event.payload.get("call_id")
            or event.item_id
            or ""
        ).strip()
        identity = (event.type, call_id)
        if not call_id or identity in existing:
            continue
        legacy_item = runtime_event_to_legacy_stream_item(event)
        if legacy_item is None:
            continue
        legacy_msg = legacy_item[0]
        legacy_msg.id = f"runtime-control:{event.id}"
        legacy_msg.timestamp = event.created_at
        legacy_msg.metadata = {
            **dict(getattr(legacy_msg, "metadata", None) or {}),
            "runtime_event_id": event.id,
            "runtime_event_type": event.type,
            "runtime_turn_id": event.turn_id,
            "runtime_item_id": event.item_id,
        }
        projected_events.append(
            (event, legacy_msg_to_chat_messages(legacy_msg)),
        )
        existing.add(identity)

    normalized_latest_client_turn_id = latest_client_turn_id.strip()
    anchored: dict[str, list[tuple[RuntimeEvent, list[Any]]]] = {}
    unanchored: list[Any] = []
    memory_turn_ids = {
        turn_id
        for message in memory_messages
        if (turn_id := _message_client_turn_id(message))
    }
    for event, event_messages in projected_events:
        client_turn_id = _runtime_event_client_turn_id(event)
        if client_turn_id and (
            client_turn_id in memory_turn_ids
            or client_turn_id == normalized_latest_client_turn_id
        ):
            anchored.setdefault(client_turn_id, []).append(
                (event, event_messages),
            )
        else:
            unanchored.extend(event_messages)

    combined = [*memory_messages, *unanchored]
    decorated = [
        (
            index,
            message,
            _message_timestamp(getattr(message, "created_at", None)),
        )
        for index, message in enumerate(combined)
    ]
    decorated.sort(
        key=lambda row: (
            row[2] if row[2] is not None else float("inf"),
            row[0],
        )
    )
    restored = [message for _, message, _ in decorated]

    for client_turn_id, event_groups in anchored.items():
        turn_indices = [
            index
            for index, message in enumerate(restored)
            if _message_client_turn_id(message) == client_turn_id
        ]
        if turn_indices:
            target_index = turn_indices[0]
            turn_messages = [restored[index] for index in turn_indices]
            for index in reversed(turn_indices):
                del restored[index]

            message_rows = [
                (_message_runtime_seq(message, runtime_events), index, message)
                for index, message in enumerate(turn_messages)
            ]
            if all(seq is not None for seq, _, _ in message_rows):
                timeline: list[tuple[int, int, list[Any]]] = [
                    (int(seq), index, [message])
                    for seq, index, message in message_rows
                    if seq is not None
                ]
                timeline.extend(
                    (event.seq, len(turn_messages) + index, event_messages)
                    for index, (event, event_messages) in enumerate(
                        event_groups
                    )
                )
                replacement = [
                    message
                    for _, _, messages in sorted(
                        timeline,
                        key=lambda row: (row[0], row[1]),
                    )
                    for message in messages
                ]
            else:
                # Old ledgers may not contain message.completed rows. Keep the
                # causal user anchor and the historical controls-before-answer
                # fallback instead of guessing a finer sequence.
                early_event_groups = [
                    pair
                    for pair in event_groups
                    if str(
                        (pair[0].payload or {}).get("legacy_kind") or ""
                    )
                    != "chat.final_answer"
                ]
                terminal_event_groups = [
                    pair
                    for pair in event_groups
                    if str(
                        (pair[0].payload or {}).get("legacy_kind") or ""
                    )
                    == "chat.final_answer"
                ]
                replacement = [
                    message
                    for _, event_messages in sorted(
                        early_event_groups,
                        key=lambda pair: pair[0].seq,
                    )
                    for message in event_messages
                ] + turn_messages + [
                    message
                    for _, event_messages in sorted(
                        terminal_event_groups,
                        key=lambda pair: pair[0].seq,
                    )
                    for message in event_messages
                ]
            restored[target_index:target_index] = replacement
            continue

        target_index = next(
            (
                index + 1
                for index in range(len(restored) - 1, -1, -1)
                if str(getattr(restored[index], "role", "")) == "user"
            ),
            0,
        )
        controls = [
            message
            for _, event_messages in sorted(
                event_groups,
                key=lambda pair: pair[0].seq,
            )
            for message in event_messages
        ]
        restored[target_index:target_index] = controls
    restored = _restore_persisted_steer_message_positions(
        restored, runtime_events
    )
    restored = _project_persisted_public_reasoning(restored, runtime_events)
    return _attach_latest_user_client_turn_id(
        restored,
        normalized_latest_client_turn_id,
    )


def _projected_reasoning_status(event: RuntimeEvent) -> str:
    payload = event.payload if isinstance(event.payload, dict) else {}
    raw_message = payload.get("message")
    if isinstance(raw_message, dict):
        for block in raw_message.get("content") or []:
            if (
                isinstance(block, dict)
                and block.get("type") == "thinking"
                and isinstance(block.get("text"), str)
            ):
                return block["text"].strip()
    if payload.get("content_type") == "thinking":
        return str(payload.get("delta") or "").strip()
    return ""


def _reasoning_event_source_id(event: RuntimeEvent) -> str:
    payload = event.payload if isinstance(event.payload, dict) else {}
    source_id = str(payload.get("source_message_id") or "").strip()
    if source_id:
        return source_id.removesuffix(":reasoning")
    item_id = str(event.item_id or "").strip()
    return item_id.removesuffix(":reasoning")


def _project_persisted_public_reasoning(
    messages: list[Any],
    runtime_events: list[RuntimeEvent],
) -> list[Any]:
    """Restore historical reasoning from the authoritative event ledger.

    Provider reasoning remains verbatim except for the projector's narrow hard
    leak guard. Explicit public-progress events keep their own stable identity;
    raw provider rows keep provider source identity and original event order.
    """

    projector = PublicReasoningProjector()
    records_by_source: dict[str, list[dict[str, Any]]] = {}
    ordered_records: list[dict[str, Any]] = []
    seen_progress: set[str] = set()
    for event in sorted(runtime_events, key=lambda item: (item.seq, item.id)):
        projected = projector.project(event)
        if projected is None:
            continue
        status = _projected_reasoning_status(projected)
        if not status:
            continue
        public_progress = projected.payload.get("public_progress")
        progress = (
            public_progress
            if is_runtime_public_progress(public_progress)
            else {}
        )
        progress_id = str(progress.get("id") or event.id)
        if progress_id in seen_progress:
            continue
        seen_progress.add(progress_id)
        record = {
            "text": status,
            "id": progress_id,
            "revision": progress.get("revision"),
            "public_projection": bool(progress),
            "text_origin": str(progress.get("text_origin") or "").strip(),
            "run_id": str(event.turn_id or "").strip(),
            "client_turn_id": _runtime_event_client_turn_id(event),
            "tool_call_ids": list(progress.get("tool_call_ids") or []),
        }
        source_id = _reasoning_event_source_id(event)
        if source_id:
            records_by_source.setdefault(source_id, []).append(record)
        ordered_records.append(record)

    projected_messages: list[Any] = []
    fallback_index = 0
    source_offsets: dict[str, int] = {}
    reasoning_messages_by_source: dict[str, int] = {}
    for message in messages:
        if str(getattr(message, "type", "") or "") != "reasoning":
            continue
        source_id = _message_source_id(message)
        if source_id:
            reasoning_messages_by_source[source_id] = (
                reasoning_messages_by_source.get(source_id, 0) + 1
            )
    source_skips = {
        source_id: max(0, count - len(records_by_source.get(source_id, [])))
        for source_id, count in reasoning_messages_by_source.items()
    }
    source_seen: dict[str, int] = {}
    for message in messages:
        if str(getattr(message, "type", "") or "") != "reasoning":
            projected_messages.append(message)
            continue
        source_id = _message_source_id(message)
        source_records = records_by_source.get(source_id, [])
        if source_id:
            seen = source_seen.get(source_id, 0)
            source_seen[source_id] = seen + 1
            if seen < source_skips.get(source_id, 0):
                continue
        source_offset = source_offsets.get(source_id, 0)
        record = (
            source_records[source_offset]
            if source_offset < len(source_records)
            else None
        )
        if record is not None:
            source_offsets[source_id] = source_offset + 1
        if (
            not source_id
            and record is None
            and fallback_index < len(ordered_records)
        ):
            record = ordered_records[fallback_index]
        if not source_id:
            fallback_index += 1
        if record is None:
            continue
        projected = deepcopy(message)
        for content in getattr(projected, "content", []) or []:
            if hasattr(content, "text"):
                content.text = record["text"]
        projected.metadata = without_runtime_public_progress_claims(
            dict(getattr(projected, "metadata", None) or {})
        )
        if record["public_projection"]:
            projected.metadata.update(
                {
                    "public_projection": "runtime_progress_v2",
                    "source": "runtime_facts",
                    "private_reasoning_exposed": False,
                    "public_progress_id": record["id"],
                    "public_progress_revision": record["revision"],
                    **(
                        {"tool_call_ids": record["tool_call_ids"]}
                        if record["tool_call_ids"]
                        else {}
                    ),
                    **(
                        {"text_origin": record["text_origin"]}
                        if record["text_origin"]
                        else {}
                    ),
                    **(
                        {
                            "run_id": record["run_id"],
                            "runtime_turn_id": record["run_id"],
                        }
                        if record["run_id"]
                        else {}
                    ),
                    **(
                        {"client_turn_id": record["client_turn_id"]}
                        if record["client_turn_id"]
                        else {}
                    ),
                }
            )
        else:
            projected.metadata.update(
                {
                    "reasoning_source": "provider",
                    "source_message_id": source_id,
                }
            )
        projected_messages.append(projected)
    return projected_messages


async def _load_persisted_runtime_control_events(
    chat_spec: ChatSpec,
    *,
    root_workspace_dir: Path | str | None = None,
) -> list[RuntimeEvent]:
    effective_workspace_dir = resolve_effective_workspace_dir_from_meta(
        chat_spec.meta or {},
        root_workspace_dir=root_workspace_dir,
    )
    if effective_workspace_dir is None:
        return []
    try:
        lineage = _validated_branch_runtime_event_lineage(chat_spec.meta or {})
        requests = [
            asyncio.to_thread(
                build_runtime_events_response,
                effective_workspace_dir,
                segment["chat_id"],
            )
            for segment in lineage
        ]
        requests.append(
            asyncio.to_thread(
                build_runtime_events_response,
                effective_workspace_dir,
                chat_spec.id,
            )
        )
        responses = await asyncio.gather(*requests)
    except Exception:  # pylint: disable=broad-except
        logger.warning(
            "Failed to restore runtime control history for chat %s",
            chat_spec.id,
            exc_info=True,
        )
        return []
    events_by_id: dict[str, RuntimeEvent] = {}
    for index, response in enumerate(responses):
        max_seq = (
            int(lineage[index]["max_seq"]) if index < len(lineage) else None
        )
        for event in response.events:
            if max_seq is not None and event.seq > max_seq:
                continue
            events_by_id.setdefault(event.id, event)
    return sorted(events_by_id.values(), key=lambda event: event.seq)


def _validated_branch_runtime_event_lineage(
    meta: dict[str, Any],
) -> list[dict[str, int | str]]:
    """Return a small, validated UI-only parent event lineage."""

    raw_lineage = meta.get(BRANCH_RUNTIME_EVENT_LINEAGE_META_KEY)
    if not isinstance(raw_lineage, list):
        return []
    lineage: list[dict[str, int | str]] = []
    seen: set[str] = set()
    for raw_segment in raw_lineage[:_BRANCH_RUNTIME_EVENT_LINEAGE_MAX_DEPTH]:
        if not isinstance(raw_segment, dict):
            continue
        chat_id = str(raw_segment.get("chat_id") or "").strip()
        try:
            max_seq = int(raw_segment.get("max_seq") or 0)
        except (TypeError, ValueError):
            continue
        if not chat_id or chat_id in seen or max_seq <= 0:
            continue
        lineage.append({"chat_id": chat_id, "max_seq": max_seq})
        seen.add(chat_id)
    return lineage


class ChatWorkspaceRequest(BaseModel):
    """Request to ensure a per-chat topic workspace exists."""

    research_project_id: str | None = None

    chat_id: str | None = None
    session_id: str
    user_id: str = "default"
    channel: str = DEFAULT_CHANNEL
    name: str = "新会话"
    manual_workspace_dir: str | None = None
    reset_manual_workspace: bool = False
    coding_mode: bool | str | None = None
    coding_workspace_dir: str | None = None
    workspace_grant_id: str | None = None
    knowledge_source: dict[str, str] | None = None
    knowledge_sources: list[dict[str, str]] | None = None
    knowledge_scope_mode: Literal["exact", "expanded"] = "exact"


class ChatWorkspaceResponse(BaseModel):
    """Workspace binding for one chat session."""

    chat: ChatSpec
    topic_workspace_dir: str
    manual_workspace_dir: str | None = None
    effective_workspace_dir: str
    state_workspace_dir: str
    project_workspace_dir: str | None = None
    workspace_grant_id: str | None = None
    workspace_access_mode: Literal["read_only", "read_write"] | None = None
    workspace_trust_state: Literal["trusted", "restricted"] | None = None
    workspace_reauthorization_required: bool = False


class ChatBranchRequest(BaseModel):
    """Create a new chat whose memory stops before one user message."""

    user_message_id: str
    name: str | None = None


def _stable_json_sha256(value: Any) -> str:
    """Return a deterministic digest suitable for recovery provenance."""

    encoded = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


class ChatActivity(BaseModel):
    """Minimal active-run projection for global desktop behavior polling."""

    id: str
    name: str
    status: Literal["running"] = "running"
    kind: Literal["cron"] | None = None


def _branch_memory_state(
    memory_state: dict[str, Any],
    *,
    user_message_id: str,
) -> dict[str, Any]:
    """Return a detached memory state ending before ``user_message_id``."""

    content = memory_state.get("content")
    if not isinstance(content, list):
        raise HTTPException(
            status_code=404,
            detail="The selected message is not available in chat memory",
        )

    target_index = -1
    for index, row in enumerate(content):
        message = row[0] if isinstance(row, (list, tuple)) and row else row
        if not isinstance(message, dict):
            continue
        if str(message.get("id") or "") != user_message_id:
            continue
        if str(message.get("role") or "") != "user":
            raise HTTPException(
                status_code=400,
                detail="Only user messages can start a chat branch",
            )
        target_index = index
        break

    if target_index < 0:
        raise HTTPException(
            status_code=404,
            detail="The selected user message was not found",
        )

    branched = deepcopy(memory_state)
    branched["content"] = deepcopy(content[:target_index])
    # A summary may describe turns after the branch point, so it cannot be
    # inherited even when the visible message prefix is valid.
    branched["_compressed_summary"] = ""
    return branched


def _branch_target_client_turn_id(
    memory_state: dict[str, Any],
    *,
    user_message_id: str,
) -> str:
    """Resolve the runtime turn caused by the selected user message."""

    content = memory_state.get("content")
    if not isinstance(content, list):
        return ""
    target_seen = False
    for row in content:
        message = row[0] if isinstance(row, (list, tuple)) and row else row
        if not isinstance(message, dict):
            continue
        if not target_seen:
            target_seen = str(message.get("id") or "") == user_message_id
            continue
        if str(message.get("role") or "") != "assistant":
            continue
        metadata = message.get("metadata")
        for container in (
            metadata,
            metadata.get("metadata") if isinstance(metadata, dict) else None,
        ):
            if not isinstance(container, dict):
                continue
            client_turn_id = str(container.get("client_turn_id") or "").strip()
            if client_turn_id:
                return client_turn_id
        break
    return ""


async def _derive_branch_runtime_event_lineage(
    source: ChatSpec,
    source_memory: dict[str, Any],
    *,
    user_message_id: str,
    root_workspace_dir: Path | str | None,
) -> tuple[list[dict[str, int | str]], list[RuntimeEvent]]:
    """Build the display-only parent prefix inherited by a branch.

    The returned events are used only to restore the visible execution trace.
    They are never written into the branched agent memory or model context.
    """

    target_client_turn_id = _branch_target_client_turn_id(
        source_memory,
        user_message_id=user_message_id,
    )
    if not target_client_turn_id:
        return [], []
    source_events = await _load_persisted_runtime_control_events(
        source,
        root_workspace_dir=root_workspace_dir,
    )
    target_events = [
        event
        for event in source_events
        if event.type == "turn.started"
        and _runtime_event_client_turn_id(event) == target_client_turn_id
    ]
    if not target_events:
        return [], []
    max_seq = min(event.seq for event in target_events) - 1
    if max_seq <= 0:
        return [], []

    lineage = []
    for segment in _validated_branch_runtime_event_lineage(source.meta or {}):
        inherited_max_seq = min(int(segment["max_seq"]), max_seq)
        if inherited_max_seq > 0:
            lineage.append(
                {
                    "chat_id": str(segment["chat_id"]),
                    "max_seq": inherited_max_seq,
                }
            )
    if source.id not in {str(segment["chat_id"]) for segment in lineage}:
        lineage.append({"chat_id": source.id, "max_seq": max_seq})
    return lineage, [event for event in source_events if event.seq <= max_seq]


async def _ensure_branch_runtime_event_lineage(
    chat_spec: ChatSpec,
    *,
    mgr: ChatManager,
    session: SafeJSONSession,
    root_workspace_dir: Path | str | None,
) -> ChatSpec:
    """Repair old branches that predate persisted runtime lineage metadata."""

    meta = chat_spec.meta or {}
    if BRANCH_RUNTIME_EVENT_LINEAGE_META_KEY in meta:
        return chat_spec
    source_chat_id = str(meta.get("branched_from_chat_id") or "").strip()
    user_message_id = str(
        meta.get("branched_from_user_message_id") or ""
    ).strip()
    if (
        not source_chat_id
        or not user_message_id
        or source_chat_id == chat_spec.id
    ):
        return chat_spec
    source = await mgr.get_chat(source_chat_id)
    if source is None:
        return chat_spec
    source_state = await session.get_session_state_dict(
        await resolve_chat_session_state_id(mgr, source),
        source.user_id,
        source.channel,
        allow_not_exist=True,
    )
    source_memory = source_state.get("agent", {}).get("memory", {})
    if not isinstance(source_memory, dict):
        return chat_spec
    lineage, _events = await _derive_branch_runtime_event_lineage(
        source,
        source_memory,
        user_message_id=user_message_id,
        root_workspace_dir=root_workspace_dir,
    )
    if not lineage:
        return chat_spec
    updated = await mgr.merge_chat_meta(
        chat_spec.id,
        {BRANCH_RUNTIME_EVENT_LINEAGE_META_KEY: lineage},
    )
    return updated or chat_spec


def _topic_workspace_name(chat: ChatSpec) -> str:
    created = chat.created_at or datetime.now(timezone.utc)
    if created.tzinfo is None:
        created = created.replace(tzinfo=timezone.utc)
    stamp = created.astimezone(timezone.utc).strftime("%Y%m%d-%H%M%S")
    suffix = (chat.id or "chat")[:8]
    return f"{stamp}-{suffix}"


def _write_topic_workspace_marker(
    workspace_dir: Path,
    *,
    root_workspace_dir: Path,
    chat: ChatSpec,
) -> None:
    marker = workspace_dir / ".lantai-topic-workspace.json"
    if marker.exists():
        return
    payload = {
        "version": 1,
        "chat_id": chat.id,
        "session_id": chat.session_id,
        "root_workspace_dir": str(root_workspace_dir),
        "created_at": datetime.now(timezone.utc).isoformat(),
    }
    marker.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


async def ensure_chat_topic_workspace(
    workspace,
    mgr: ChatManager,
    chat: ChatSpec,
    *,
    manual_workspace_dir: str | None = None,
    reset_manual_workspace: bool = False,
    workspace_grant_id: str | None = None,
    workspace_binding: WorkspaceBinding | None = None,
    workspace_grant_registry: WorkspaceGrantRegistry | None = None,
    agent_id: str = "default",
    coding_mode: bool | str | None = None,
) -> ChatWorkspaceResponse:
    """Ensure one chat has a durable topic workspace and return effective dir."""
    root_workspace_dir = (
        Path(workspace.workspace_dir)
        .expanduser()
        .resolve(
            strict=False,
        )
    )
    meta = dict(chat.meta or {})
    topic_raw = meta.get(TOPIC_WORKSPACE_META_KEY)

    if isinstance(topic_raw, str) and topic_raw.strip():
        topic_workspace_dir = resolve_effective_workspace_dir_from_meta(
            {TOPIC_WORKSPACE_META_KEY: topic_raw},
            root_workspace_dir=root_workspace_dir,
        )
        if topic_workspace_dir is None:
            topic_workspace_dir = (
                root_workspace_dir
                / TOPIC_WORKSPACES_DIR
                / _topic_workspace_name(chat)
            )
    else:
        topic_workspace_dir = (
            root_workspace_dir
            / TOPIC_WORKSPACES_DIR
            / _topic_workspace_name(chat)
        )

    topic_workspace_dir.mkdir(parents=True, exist_ok=True)
    _write_topic_workspace_marker(
        topic_workspace_dir,
        root_workspace_dir=root_workspace_dir,
        chat=chat,
    )

    updates = {
        TOPIC_WORKSPACE_META_KEY: str(topic_workspace_dir),
    }
    remove_keys: list[str] = []
    if coding_mode is not None:
        updates["coding_mode"] = coding_mode

    manual_dir: Path | None = None
    grant_binding = workspace_binding
    requested_grant_id = str(workspace_grant_id or "").strip() or None
    if requested_grant_id and grant_binding is None:
        registry = workspace_grant_registry or default_workspace_grant_registry
        try:
            grant_binding = registry.resolve(
                requested_grant_id,
                agent_id=agent_id,
                operation="read",
            )
        except WorkspaceGrantError as error:
            raise workspace_grant_http_exception(error) from error
    elif grant_binding is None:
        existing_grant_id = meta.get(WORKSPACE_GRANT_META_KEY)
        if isinstance(existing_grant_id, str) and existing_grant_id.strip():
            registry = (
                workspace_grant_registry or default_workspace_grant_registry
            )
            try:
                grant_binding = registry.resolve(
                    existing_grant_id.strip(),
                    agent_id=agent_id,
                    operation="read",
                )
            except WorkspaceGrantError:
                updates[WORKSPACE_REAUTH_REQUIRED_META_KEY] = True
                updates[WORKSPACE_REAUTH_NAME_META_KEY] = "外部工作区"
                updates[WORKSPACE_LEGACY_FINGERPRINT_META_KEY] = (
                    "grant:"
                    + hashlib.sha256(
                        existing_grant_id.strip().encode("utf-8"),
                    ).hexdigest()
                )
                remove_keys.extend(
                    [
                        WORKSPACE_GRANT_META_KEY,
                        MANUAL_WORKSPACE_META_KEY,
                        "coding_workspace_dir",
                        "workspace_dir",
                    ],
                )

    if grant_binding is not None:
        updates[WORKSPACE_GRANT_META_KEY] = grant_binding.grant_id
        remove_keys.extend(
            [
                MANUAL_WORKSPACE_META_KEY,
                "coding_workspace_dir",
                WORKSPACE_REAUTH_REQUIRED_META_KEY,
                WORKSPACE_REAUTH_NAME_META_KEY,
                WORKSPACE_LEGACY_FINGERPRINT_META_KEY,
            ],
        )
    elif reset_manual_workspace:
        remove_keys.extend(
            [
                MANUAL_WORKSPACE_META_KEY,
                WORKSPACE_GRANT_META_KEY,
                WORKSPACE_REAUTH_REQUIRED_META_KEY,
                WORKSPACE_REAUTH_NAME_META_KEY,
                WORKSPACE_LEGACY_FINGERPRINT_META_KEY,
            ],
        )
    elif manual_workspace_dir is not None:
        manual_text = manual_workspace_dir.strip()
        if manual_text:
            manual_dir = resolve_effective_workspace_dir_from_meta(
                {MANUAL_WORKSPACE_META_KEY: manual_text},
                root_workspace_dir=root_workspace_dir,
            )
            if manual_dir is None:
                raise HTTPException(
                    status_code=403,
                    detail={
                        "code": "workspace_grant_required",
                        "message": (
                            "An external workspace must be selected and "
                            "authorized first."
                        ),
                    },
                )
            manual_dir.mkdir(parents=True, exist_ok=True)
            updates[MANUAL_WORKSPACE_META_KEY] = str(manual_dir)
            remove_keys.extend(
                [
                    WORKSPACE_REAUTH_REQUIRED_META_KEY,
                    WORKSPACE_REAUTH_NAME_META_KEY,
                    WORKSPACE_LEGACY_FINGERPRINT_META_KEY,
                ],
            )
        else:
            remove_keys.append(MANUAL_WORKSPACE_META_KEY)
    else:
        existing_manual = (
            meta.get(MANUAL_WORKSPACE_META_KEY)
            or meta.get("coding_workspace_dir")
            or meta.get("workspace_dir")
        )
        if isinstance(existing_manual, str) and existing_manual.strip():
            manual_dir = resolve_effective_workspace_dir_from_meta(
                {MANUAL_WORKSPACE_META_KEY: existing_manual},
                root_workspace_dir=root_workspace_dir,
            )
            if manual_dir is None:
                legacy_path = existing_manual.strip()
                updates[WORKSPACE_REAUTH_REQUIRED_META_KEY] = True
                updates[WORKSPACE_REAUTH_NAME_META_KEY] = (
                    Path(legacy_path).expanduser().name or "外部工作区"
                )
                updates[WORKSPACE_LEGACY_FINGERPRINT_META_KEY] = (
                    "sha256:"
                    + hashlib.sha256(legacy_path.encode("utf-8")).hexdigest()
                )
                remove_keys.extend(
                    [
                        MANUAL_WORKSPACE_META_KEY,
                        "coding_workspace_dir",
                        "workspace_dir",
                        WORKSPACE_GRANT_META_KEY,
                    ],
                )

    updated = await mgr.merge_chat_meta(
        chat.id,
        updates,
        remove_keys=remove_keys,
    )
    if updated is not None:
        chat = updated

    final_meta = dict(chat.meta or {})
    final_manual = final_meta.get(MANUAL_WORKSPACE_META_KEY)
    if isinstance(final_manual, str) and final_manual.strip():
        manual_dir = resolve_effective_workspace_dir_from_meta(
            {MANUAL_WORKSPACE_META_KEY: final_manual},
            root_workspace_dir=root_workspace_dir,
        )
    elif reset_manual_workspace or remove_keys:
        manual_dir = None

    project_workspace_dir = (
        grant_binding.project_root if grant_binding is not None else manual_dir
    )
    effective_workspace_dir = project_workspace_dir or topic_workspace_dir
    effective_workspace_dir.mkdir(parents=True, exist_ok=True)

    return ChatWorkspaceResponse(
        chat=chat,
        topic_workspace_dir=str(topic_workspace_dir),
        manual_workspace_dir=str(manual_dir) if manual_dir else None,
        effective_workspace_dir=str(effective_workspace_dir),
        state_workspace_dir=str(topic_workspace_dir),
        project_workspace_dir=(
            str(project_workspace_dir) if project_workspace_dir else None
        ),
        workspace_grant_id=(
            grant_binding.grant_id if grant_binding is not None else None
        ),
        workspace_access_mode=(
            grant_binding.access_mode if grant_binding is not None else None
        ),
        workspace_trust_state=(
            grant_binding.trust_state if grant_binding is not None else None
        ),
        workspace_reauthorization_required=bool(
            (chat.meta or {}).get(WORKSPACE_REAUTH_REQUIRED_META_KEY),
        ),
    )


async def get_workspace(request: Request):
    """Get the workspace for the active agent."""
    from ..agent_context import get_agent_for_request

    return await get_agent_for_request(request)


def get_workspace_grant_registry() -> WorkspaceGrantRegistry:
    """Dependency seam for persistent project authorization."""

    return default_workspace_grant_registry


def _context_profile_payload(
    workspace: Any, request: Request
) -> dict[str, Any]:
    """Build UI diagnostics from the currently effective model selection."""

    from ...model_context_profile import ModelContextProfile
    from ...providers.provider_manager import ProviderManager

    config = workspace.config
    manager = getattr(request.app.state, "provider_manager", None)
    manager = manager or ProviderManager.get_instance()
    slot = getattr(config, "active_model", None) or manager.get_active_model()
    provider_id = str(getattr(slot, "provider_id", "") or "")
    model_id = str(getattr(slot, "model", "") or "")
    local_window = None
    if provider_id in {"lantai-local", "copaw-local"}:
        try:
            from ...local_models.manager import LocalModelManager

            local_window = (
                LocalModelManager.get_instance()
                .get_config()
                .max_context_length
            )
        except Exception:
            logger.debug("Unable to read local context window", exc_info=True)
    profile = ModelContextProfile.from_provider_selection(
        provider_manager=manager,
        provider_id=provider_id,
        model_id=model_id,
        user_window=getattr(config.running, "max_input_length", None),
        local_runtime_window=local_window,
    )
    policy = profile.policy(
        str(
            getattr(config.running, "context_policy_mode", "smart") or "smart",
        ),
    )
    return {
        "requested_window": profile.user_window.tokens,
        "model_window": profile.provider_window.tokens,
        "effective_window": profile.effective_window,
        # Actual is populated by provider usage when it reports a context cap;
        # do not invent it from the agent setting.
        "actual_window": None,
        "context_profile_status": profile.status,
        "context_profile": profile.as_dict(),
        "context_policy": {
            "mode": policy.mode,
            "hard_window": policy.hard_window,
            "usable_input_tokens": policy.usable_input_tokens,
            "target_input_tokens": policy.target_input_tokens,
            "output_reserve": policy.output_reserve,
            "safety_margin": policy.safety_margin,
            "used_unknown_fallback": policy.used_unknown_fallback,
        },
    }


async def get_chat_manager(
    request: Request,
) -> ChatManager:
    """Get the chat manager for the active agent.

    Args:
        request: FastAPI request object

    Returns:
        ChatManager instance for the specified agent

    Raises:
        HTTPException: If manager is not initialized
    """
    workspace = await get_workspace(request)
    chat_manager = getattr(workspace, "chat_manager", None)
    if chat_manager is None:
        raise HTTPException(
            status_code=503,
            detail="Chat manager is not ready",
        )
    return chat_manager


async def get_session(
    request: Request,
) -> SafeJSONSession:
    """Get the session for the active agent.

    Args:
        request: FastAPI request object

    Returns:
        SafeJSONSession instance for the specified agent

    Raises:
        HTTPException: If session is not initialized
    """
    workspace = await get_workspace(request)
    runner = getattr(workspace, "runner", None)
    session = getattr(runner, "session", None)
    if session is None:
        raise HTTPException(
            status_code=503,
            detail="Runner session store is not ready",
        )
    return session


@router.get("", response_model=list[ChatSpec])
async def list_chats(
    user_id: Optional[str] = Query(None, description="Filter by user ID"),
    channel: Optional[str] = Query(None, description="Filter by channel"),
    mode: Literal["all", "chat", "coding"] = Query(
        "all",
        description="Filter ordinary chat sessions or Coding sessions.",
    ),
    include_archived: bool = Query(
        False,
        description="Include chats archived by the user.",
    ),
    include_blank: bool = Query(
        False,
        description="Include persisted chats that never accepted a user turn.",
    ),
    mgr: ChatManager = Depends(get_chat_manager),
    workspace=Depends(get_workspace),
):
    """List all chats with optional filters.

    Args:
        user_id: Optional user ID to filter chats
        channel: Optional channel name to filter chats
        mgr: Chat manager dependency
    """
    await _retry_pending_analysis_cleanup(mgr, workspace)
    chats = await mgr.list_chats(user_id=user_id, channel=channel)
    tracker = workspace.task_tracker
    active_chat_ids = set(await tracker.list_active_tasks())
    title_repairs: list[tuple[str, str, str]] = []
    for spec in chats:
        if spec.archived and not include_archived:
            continue
        if _is_blank_chat(spec) and not include_blank:
            continue
        recorded_prompt = (
            spec.meta.get("last_turn_text")
            if isinstance(spec.meta, dict)
            else ""
        )
        semantic_name = _legacy_semantic_title_repair(
            spec.name,
            recorded_prompt,
        )
        if semantic_name:
            title_repairs.append((spec.id, spec.name, semantic_name))
    repaired_titles = await mgr.repair_auto_titles_if_names_match(
        title_repairs
    )
    result = []
    for spec in chats:
        if spec.archived and not include_archived:
            continue
        if _is_blank_chat(spec) and not include_blank:
            continue
        spec = repaired_titles.get(spec.id, spec)
        is_coding = is_coding_chat_spec(spec)
        if mode == "chat" and is_coding:
            continue
        if mode == "coding" and not is_coding:
            continue
        status = "running" if spec.id in active_chat_ids else "idle"
        # Event ledgers can grow to tens of megabytes. The list only needs to
        # inspect one when a persisted pending turn has lost its active task;
        # idle and actively-running chats already have an authoritative status.
        if status == "idle" and _needs_list_stall_probe(spec):
            turn_event_preview = await _load_turn_event_preview(
                spec,
                root_workspace_dir=workspace.workspace_dir,
            )
            spec, _ = await mark_chat_stalled_if_needed(
                mgr=mgr,
                workspace=workspace,
                chat_spec=spec,
                tracker_status=status,
                active_run=None,
                turn_event_preview=turn_event_preview,
            )
        if (
            str(pending_turn_dict(spec).get("status") or "").lower()
            == "stalled"
        ):
            status = "stalled"
        result.append(
            _public_chat_spec(spec.model_copy(update={"status": status}))
        )
    return result


@router.get(
    "/activity", response_model=list[ChatActivity], response_model_exclude_none=True,
)
async def list_chat_activity(
    mgr: ChatManager = Depends(get_chat_manager),
    workspace=Depends(get_workspace),
) -> list[ChatActivity]:
    """Project chat and Cron activity without hydrating conversation histories."""

    result: list[ChatActivity] = []
    for chat_id in await workspace.task_tracker.list_active_tasks():
        snapshot = await read_control_snapshot(workspace.task_tracker, chat_id)
        if snapshot and snapshot.get("activity_kind") == "cron":
            # Cron run keys are opaque invocation identities, not ChatSpec IDs.
            # Keep them independent even when their root session belongs to a chat.
            result.append(ChatActivity(
                id=chat_id, kind="cron",
                name=str(snapshot.get("activity_name") or "定时任务"),
            ))
            continue
        spec = await mgr.get_chat(chat_id)
        if spec is None:
            continue
        result.append(
            ChatActivity(
                id=spec.id,
                name=(spec.name or "未命名任务").strip() or "未命名任务",
            )
        )
    return result


async def _hydrate_chat_search_documents(
    chat: ChatSpec,
    *,
    session: SafeJSONSession,
    session_state_id: str,
) -> list[ChatSearchDocument]:
    state = await session.get_session_state_dict(
        session_state_id,
        chat.user_id,
        chat.channel,
    )
    messages = (
        await legacy_memory_state_to_chat_messages(
            state.get("agent", {}).get("memory", {}),
        )
        if state
        else []
    )
    documents: list[ChatSearchDocument] = []
    seen: set[tuple[str, str]] = set()
    for index, message in enumerate(messages[:800]):
        text = _message_plain_text(message)[:20_000]
        normalized = " ".join(text.split())
        role = str(getattr(message, "role", "") or "")
        if not normalized or (role, normalized) in seen:
            continue
        seen.add((role, normalized))
        message_id = (
            str(getattr(message, "id", "") or "").strip()
            or _message_source_id(message)
            or f"{chat.id}:message:{index}"
        )
        message_timestamp = _message_timestamp(
            getattr(message, "created_at", None)
        )
        documents.append(
            (
                message_id,
                role,
                normalized,
                datetime.fromtimestamp(message_timestamp, timezone.utc)
                if message_timestamp is not None
                else None,
            )
        )
    return documents


def _chat_search_cache_characters() -> int:
    return sum(
        len(message_id) + len(role) + len(text)
        for documents in _CHAT_SEARCH_INDEX.values()
        for message_id, role, text, _timestamp in documents
    )


async def _chat_search_documents(
    chat: ChatSpec,
    *,
    session: SafeJSONSession,
    workspace_dir: str | Path,
    session_state_id: str,
) -> list[ChatSearchDocument]:
    """Return an LRU-cached message projection for one chat revision."""

    workspace_key = str(Path(workspace_dir).resolve())
    revision = chat.updated_at.isoformat()
    cache_key = (workspace_key, chat.id, revision)
    created_task = False
    async with _CHAT_SEARCH_INDEX_LOCK:
        cached = _CHAT_SEARCH_INDEX.get(cache_key)
        if cached is not None:
            _CHAT_SEARCH_INDEX.move_to_end(cache_key)
            return cached
        task = _CHAT_SEARCH_INFLIGHT.get(cache_key)
        if task is None:
            task = asyncio.create_task(
                _hydrate_chat_search_documents(
                    chat,
                    session=session,
                    session_state_id=session_state_id,
                )
            )
            _CHAT_SEARCH_INFLIGHT[cache_key] = task
            created_task = True

    try:
        documents = await task
    except BaseException:
        if created_task:
            async with _CHAT_SEARCH_INDEX_LOCK:
                _CHAT_SEARCH_INFLIGHT.pop(cache_key, None)
        raise

    if not created_task:
        return documents

    async with _CHAT_SEARCH_INDEX_LOCK:
        _CHAT_SEARCH_INFLIGHT.pop(cache_key, None)
        # A chat update creates a new revision key. Drop its stale projection,
        # then evict the global least-recently-used entries by count and text
        # volume so long-lived multi-agent processes cannot grow unbounded.
        for key in tuple(_CHAT_SEARCH_INDEX):
            if key[:2] == (workspace_key, chat.id) and key != cache_key:
                _CHAT_SEARCH_INDEX.pop(key, None)
        _CHAT_SEARCH_INDEX[cache_key] = documents
        _CHAT_SEARCH_INDEX.move_to_end(cache_key)
        while _CHAT_SEARCH_INDEX and (
            len(_CHAT_SEARCH_INDEX) > _CHAT_SEARCH_INDEX_MAX_ENTRIES
            or _chat_search_cache_characters()
            > _CHAT_SEARCH_INDEX_MAX_CHARACTERS
        ):
            _CHAT_SEARCH_INDEX.popitem(last=False)
    return documents


def _search_excerpt(text: str, query: str, radius: int = 90) -> str:
    index = text.casefold().find(query.casefold())
    if index < 0:
        return text[: radius * 2]
    start = max(0, index - radius)
    end = min(len(text), index + len(query) + radius)
    prefix = "…" if start else ""
    suffix = "…" if end < len(text) else ""
    return f"{prefix}{text[start:end]}{suffix}"


@router.get("/search", response_model=list[ChatSearchHit])
async def search_chats(
    q: str = Query(min_length=1, max_length=200),
    limit: int = Query(default=60, ge=1, le=100),
    mgr: ChatManager = Depends(get_chat_manager),
    session: SafeJSONSession = Depends(get_session),
    workspace=Depends(get_workspace),
) -> list[ChatSearchHit]:
    """Search titles and visible messages through a reusable bounded index."""

    query = " ".join(q.split()).strip()
    if not query:
        return []
    normalized_query = query.casefold()
    chats = sorted(
        await mgr.list_chats(),
        key=lambda chat: chat.updated_at,
        reverse=True,
    )
    known_chats = tuple(chats)
    semaphore = asyncio.Semaphore(6)

    async def build(chat: ChatSpec) -> list[ChatSearchHit]:
        hits: list[ChatSearchHit] = []
        if normalized_query in (chat.name or "").casefold():
            hits.append(
                ChatSearchHit(
                    id=f"{chat.id}:title",
                    kind="title",
                    chat_id=chat.id,
                    chat_name=chat.name or "新会话",
                    excerpt=chat.name or "新会话",
                    timestamp=chat.updated_at,
                    archived=chat.archived,
                    coding=is_coding_chat_spec(chat),
                )
            )
        async with semaphore:
            documents = await _chat_search_documents(
                chat,
                session=session,
                workspace_dir=workspace.workspace_dir,
                session_state_id=session_state_id_from_known_chats(
                    chat,
                    known_chats,
                ),
            )
        for message_id, role, text, message_timestamp in documents:
            if normalized_query not in text.casefold():
                continue
            hits.append(
                ChatSearchHit(
                    id=f"{chat.id}:{message_id}",
                    kind="message",
                    chat_id=chat.id,
                    chat_name=chat.name or "新会话",
                    message_id=message_id,
                    role=role,
                    excerpt=_search_excerpt(text, query),
                    timestamp=message_timestamp,
                    archived=chat.archived,
                    coding=is_coding_chat_spec(chat),
                )
            )
            if len(hits) >= 5:
                break
        return hits

    results: list[ChatSearchHit] = []
    for start in range(0, len(chats), 18):
        grouped = await asyncio.gather(
            *(build(chat) for chat in chats[start : start + 18]),
        )
        for hits in grouped:
            results.extend(hits)
            if len(results) >= limit:
                return results[:limit]
    return results[:limit]


@router.post("", response_model=ChatSpec)
async def create_chat(
    request: ChatSpec,
    mgr: ChatManager = Depends(get_chat_manager),
):
    """Create a new chat.

    Server generates chat_id (UUID) automatically.

    Args:
        request: Chat creation request
        mgr: Chat manager dependency

    Returns:
        Created chat spec with UUID
    """
    chat_id = str(uuid4())
    request_meta = without_ququ_continuation_storage(request.meta)
    request_meta.pop(BRANCH_RUNTIME_EVENT_LINEAGE_META_KEY, None)
    request_meta.pop(SESSION_STATE_ID_META_KEY, None)
    request_meta.pop(EXTERNAL_ATTACHMENT_PATHS_META_KEY, None)
    spec = ChatSpec(
        id=chat_id,
        name=request.name,
        session_id=request.session_id,
        user_id=request.user_id,
        channel=request.channel,
        meta=new_chat_meta(
            request_meta,
            chat_id=chat_id,
            channel=request.channel,
        ),
    )
    return _public_chat_spec(await mgr.create_chat(spec))


@router.post("/topic-workspace", response_model=ChatWorkspaceResponse)
async def ensure_topic_workspace(
    request: ChatWorkspaceRequest,
    mgr: ChatManager = Depends(get_chat_manager),
    workspace=Depends(get_workspace),
    grant_registry: WorkspaceGrantRegistry = Depends(
        get_workspace_grant_registry,
    ),
    agent_id: str = Header(default="default", alias="X-Agent-Id"),
    workspace_grant_header: str
    | None = Header(
        default=None,
        alias="X-Workspace-Grant-Id",
    ),
) -> ChatWorkspaceResponse:
    """Create or return the topic workspace bound to one chat session."""
    if request.research_project_id is not None:
        from ...research.chat import ResearchChatBinding

        def validate_research_binding():
            from ...research.projects import ResearchService

            if not request.research_project_id.strip():
                raise ValueError("Invalid research project association")
            ResearchChatBinding(
                ResearchService(workspace.workspace_dir),
                request.research_project_id,
            ).current()

        try:
            await asyncio.to_thread(validate_research_binding)
        except (ValueError, FileNotFoundError) as error:
            raise HTTPException(status_code=400, detail=str(error)) from error

    grant_binding: WorkspaceBinding | None = None
    requested_grant_id = str(
        request.workspace_grant_id or workspace_grant_header or "",
    ).strip()
    if requested_grant_id:
        try:
            grant_binding = grant_registry.resolve(
                requested_grant_id,
                agent_id=agent_id,
                operation="read",
            )
        except WorkspaceGrantError as error:
            raise workspace_grant_http_exception(error) from error

    chat = None
    if request.chat_id:
        # A direct /chat/<id> route can render before its session list finishes
        # hydrating. Keep the existing backend chat authoritative so a quick
        # first send cannot create a second chat and stream events elsewhere.
        chat = await mgr.get_chat(request.chat_id)
    if chat is None:
        chat = await mgr.get_or_create_chat(
            request.session_id,
            request.user_id,
            request.channel,
            name=request.name or "新会话",
        )
    response = await ensure_chat_topic_workspace(
        workspace,
        mgr,
        chat,
        manual_workspace_dir=request.manual_workspace_dir,
        reset_manual_workspace=request.reset_manual_workspace,
        workspace_grant_id=requested_grant_id or None,
        workspace_binding=grant_binding,
        workspace_grant_registry=grant_registry,
        agent_id=agent_id,
        coding_mode=request.coding_mode,
    )
    meta_updates: dict[str, object] = {}
    if request.research_project_id is not None:
        existing_project = (chat.meta or {}).get("research_project_id")
        if (
            existing_project is not None
            and existing_project != request.research_project_id
        ):
            raise HTTPException(
                status_code=409,
                detail="Chat already belongs to another research project",
            )
        meta_updates["research_project_id"] = request.research_project_id

    raw_knowledge_sources = request.knowledge_sources
    if raw_knowledge_sources is None and request.knowledge_source is not None:
        raw_knowledge_sources = [request.knowledge_source]
    if raw_knowledge_sources is not None:
        if not raw_knowledge_sources or len(raw_knowledge_sources) > 20:
            raise HTTPException(
                status_code=400, detail="Invalid knowledge sources"
            )
        source_root = (
            Path(workspace.workspace_dir).expanduser().resolve(strict=False)
        )
        knowledge_roots = (
            (source_root / ".wiki" / "raw" / "sources").resolve(strict=False),
            (source_root / ".wiki" / "sources").resolve(strict=False),
        )
        validated_sources: list[dict[str, str]] = []
        seen_source_paths: set[str] = set()
        for reference in raw_knowledge_sources:
            source_path = (
                str(reference.get("path") or "").strip().replace("\\", "/")
            )
            source_filename = str(reference.get("filename") or "").strip()
            relative_source = Path(source_path)
            resolved_source = (source_root / relative_source).resolve(
                strict=False
            )
            if (
                not source_path
                or not source_filename
                or source_path in seen_source_paths
                or relative_source.is_absolute()
                or any(
                    part in {"", ".", ".."} for part in relative_source.parts
                )
                or not resolved_source.is_relative_to(source_root)
                or not any(
                    resolved_source.is_relative_to(knowledge_root)
                    for knowledge_root in knowledge_roots
                )
                or not resolved_source.is_file()
            ):
                raise HTTPException(
                    status_code=400, detail="Invalid knowledge source"
                )
            seen_source_paths.add(source_path)
            validated_sources.append(
                {
                    "path": source_path,
                    "filename": source_filename[:240],
                    "kind": str(reference.get("kind") or "")[:40],
                }
            )
        meta_updates["knowledge_sources"] = validated_sources
        meta_updates["knowledge_scope_mode"] = request.knowledge_scope_mode
        meta_updates["knowledge_source"] = (
            validated_sources[0] if len(validated_sources) == 1 else None
        )
    if request.coding_mode is not None and grant_binding is None:
        meta_updates["coding_mode"] = request.coding_mode
    if request.coding_workspace_dir and request.coding_workspace_dir.strip():
        coding_workspace_dir = resolve_effective_workspace_dir_from_meta(
            {"manual_workspace_dir": request.coding_workspace_dir},
            root_workspace_dir=workspace.workspace_dir,
        )
        if coding_workspace_dir is None:
            raise HTTPException(
                status_code=400,
                detail="Coding workspace must stay inside the agent workspace",
            )
        meta_updates["coding_workspace_dir"] = str(coding_workspace_dir)
    elif request.coding_mode and grant_binding is None:
        meta_updates["coding_workspace_dir"] = (
            response.manual_workspace_dir or response.effective_workspace_dir
        )

    if meta_updates:
        if request.research_project_id is not None:
            def bind_research_meta(meta, _chat):
                existing_project = meta.get("research_project_id")
                if (
                    existing_project is not None
                    and existing_project != request.research_project_id
                ):
                    raise HTTPException(
                        status_code=409,
                        detail="Chat already belongs to another research project",
                    )
                meta.update(meta_updates)
                return meta, None

            updated, _ = await mgr.mutate_chat_meta(
                response.chat.id, bind_research_meta, touch_updated_at=True,
            )
        else:
            updated = await mgr.merge_chat_meta(response.chat.id, meta_updates)
        if updated is not None:
            response.chat = updated

    return response


def _runtime_event_sse_payload(response) -> str:
    event_id = (
        response.cursor or f"{response.stream_epoch}:{response.latest_seq}"
    )
    return (
        f"id: {event_id}\n"
        + "event: runtime\n"
        + "data: "
        + json.dumps(response.model_dump(mode="json"), ensure_ascii=False)
        + "\n\n"
    )


def _runtime_stream_cursor_from_last_event_id(
    value: str | None,
) -> tuple[str, int]:
    raw = str(value or "").strip()
    if not raw or ":" not in raw:
        return "", 0
    epoch, raw_seq = raw.rsplit(":", 1)
    try:
        seq = max(0, int(raw_seq))
    except ValueError:
        return "", 0
    return epoch.strip(), seq


def _current_turn_identifiers(chat_spec: ChatSpec) -> set[str]:
    pending_turn = pending_turn_dict(chat_spec)
    values = (
        pending_turn.get("turn_id"),
        pending_turn.get("run_id"),
        pending_turn.get("last_turn_run_id"),
    )
    return {
        str(value).strip()
        for value in values
        if isinstance(value, str) and value.strip()
    }


def _project_public_runtime_event_response(
    response: Any,
    projector: PublicReasoningProjector | None = None,
) -> Any:
    """Apply the private/public boundary to both replay and live event reads."""

    public_projector = projector or PublicReasoningProjector()
    events = [
        projected
        for event in response.events
        if (projected := public_projector.project(event)) is not None
    ]
    return response.model_copy(update={"events": events})


def _public_reasoning_projector_at_cursor(
    workspace_dir: Path | str,
    chat_id: str,
    cursor: int,
) -> PublicReasoningProjector:
    """Restore public-progress state before projecting a paged/reconnected slice.

    A projector created at a non-zero cursor must know the tool and progress
    facts that occurred earlier in the same ledger. Otherwise a reconnect can
    turn "completed one search" back into the initial planning sentence. Raw
    reasoning is still never read to compose public text; replay only rebuilds
    deterministic projector state and discards every projected row.
    """

    projector = PublicReasoningProjector()
    if cursor <= 0:
        return projector
    with closing(
        iter_runtime_events_through_seq(workspace_dir, chat_id, cursor),
    ) as previous:
        for event in previous:
            projector.project(event)
    return projector


def _stale_attempt_error(turn_id: str) -> HTTPException:
    return HTTPException(
        status_code=409,
        detail={
            "code": "stale_attempt",
            "message": "Stop request does not match the active turn.",
            "turn_id": turn_id,
        },
    )


async def _request_stop_for_chat_turn(
    *,
    workspace: Any,
    chat_spec: ChatSpec,
    turn_id: str,
) -> tuple[bool, str]:
    tracker = workspace.task_tracker
    active_snapshot = await read_control_snapshot(tracker, chat_spec.id)
    if active_snapshot is None:
        return False, ""

    current_identifiers = _current_turn_identifiers(chat_spec)
    if current_identifiers and turn_id not in current_identifiers:
        raise _stale_attempt_error(turn_id)

    generation = active_snapshot.get("generation")
    active_attempt_id = str(active_snapshot.get("attempt_id") or "").strip()
    if active_attempt_id and turn_id != active_attempt_id:
        raise _stale_attempt_error(turn_id)
    if isinstance(generation, int):
        stop_if_attempt = getattr(
            tracker,
            "request_stop_if_attempt",
            None,
        )
        if active_attempt_id and callable(stop_if_attempt):
            stopped = await stop_if_attempt(
                chat_spec.id,
                attempt_id=active_attempt_id,
                generation=generation,
            )
            if not stopped:
                raise _stale_attempt_error(turn_id)
            return True, active_attempt_id
        stop_if_generation = getattr(
            tracker,
            "request_stop_if_generation",
            None,
        )
        if callable(stop_if_generation):
            stopped = await stop_if_generation(chat_spec.id, generation)
        else:
            stopped = await tracker.request_stop(chat_spec.id)
        if not stopped:
            raise _stale_attempt_error(turn_id)
        return True, active_attempt_id

    stopped = await tracker.request_stop(chat_spec.id)
    return stopped, active_attempt_id if stopped else ""


async def _chat_workspace_or_404(
    chat_spec: ChatSpec,
    *,
    root_workspace_dir: Path | str | None = None,
) -> Path:
    effective_workspace_dir = resolve_effective_workspace_dir_from_meta(
        chat_spec.meta or {},
        root_workspace_dir=root_workspace_dir,
    )
    if effective_workspace_dir is None:
        raise HTTPException(
            status_code=404,
            detail="Chat runtime event workspace not found",
        )
    return effective_workspace_dir


@router.get("/{chat_id}/events", response_model=dict)
async def get_chat_runtime_events(
    chat_id: str,
    after_seq: int = Query(
        0,
        ge=0,
        description="Only return events with seq greater than this value",
    ),
    limit: int
    | None = Query(
        default=None,
        ge=1,
        le=1000,
        description="Maximum number of events to return",
    ),
    event_type: list[str]
    | None = Query(
        default=None,
        description="Optional RuntimeEvent type allowlist; repeat the query parameter",
    ),
    mgr: ChatManager = Depends(get_chat_manager),
    workspace=Depends(get_workspace),
):
    """Return RuntimeEvent v1 rows for one chat."""
    chat_spec = await mgr.get_chat(chat_id)
    if not chat_spec:
        raise HTTPException(
            status_code=404,
            detail=f"Chat not found: {chat_id}",
        )

    workspace_dir = await _chat_workspace_or_404(
        chat_spec,
        root_workspace_dir=workspace.workspace_dir,
    )
    projector = await asyncio.to_thread(
        _public_reasoning_projector_at_cursor,
        workspace_dir,
        chat_spec.id,
        after_seq,
    )
    response = await asyncio.to_thread(
        build_runtime_events_response,
        workspace_dir,
        chat_spec.id,
        after_seq=after_seq,
        limit=limit,
        event_types=set(event_type or []),
    )
    if response.resync_required:
        projector = PublicReasoningProjector()
    response = _project_public_runtime_event_response(response, projector)
    return response.model_dump(mode="json")


@router.get("/{chat_id}/events/stream")
async def stream_chat_runtime_events(
    request: Request,
    chat_id: str,
    after_seq: int = Query(
        0,
        ge=0,
        description="Replay events after this sequence before tailing",
    ),
    stream_epoch: str
    | None = Query(
        default=None,
        description="Ledger generation paired with after_seq",
    ),
    mgr: ChatManager = Depends(get_chat_manager),
    workspace=Depends(get_workspace),
) -> StreamingResponse:
    """Stream RuntimeEvent v1 rows for one chat via SSE."""
    chat_spec = await mgr.get_chat(chat_id)
    if not chat_spec:
        raise HTTPException(
            status_code=404,
            detail=f"Chat not found: {chat_id}",
        )
    workspace_dir = await _chat_workspace_or_404(
        chat_spec,
        root_workspace_dir=workspace.workspace_dir,
    )

    (
        last_event_epoch,
        last_event_seq,
    ) = _runtime_stream_cursor_from_last_event_id(
        request.headers.get("last-event-id")
    )
    initial_epoch = str(stream_epoch or last_event_epoch or "").strip()
    initial_cursor = after_seq
    if after_seq == 0 and last_event_seq > 0:
        initial_cursor = last_event_seq

    async def event_generator():
        cursor = initial_cursor
        expected_epoch = initial_epoch
        public_projector = await asyncio.to_thread(
            _public_reasoning_projector_at_cursor,
            workspace_dir,
            chat_spec.id,
            initial_cursor,
        )
        observed_generation = runtime_event_stream_generation(workspace_dir)
        while True:
            raw_response = await asyncio.to_thread(
                build_runtime_events_response,
                workspace_dir,
                chat_spec.id,
                after_seq=cursor,
                limit=250,
                expected_stream_epoch=expected_epoch,
            )
            if raw_response.resync_required:
                public_projector = PublicReasoningProjector()
            response = _project_public_runtime_event_response(
                raw_response,
                public_projector,
            )
            if raw_response.events:
                cursor = raw_response.next_after_seq or max(
                    event.seq for event in raw_response.events
                )
                expected_epoch = raw_response.stream_epoch
            if response.events:
                yield _runtime_event_sse_payload(response)
            if raw_response.has_more:
                continue
            if raw_response.resync_required:
                cursor = raw_response.next_after_seq or 0
                expected_epoch = raw_response.stream_epoch
                if not response.events:
                    yield _runtime_event_sse_payload(response)
            if await request.is_disconnected():
                break
            next_generation = await wait_for_runtime_event_append(
                workspace_dir,
                after_generation=observed_generation,
                timeout_seconds=RUNTIME_EVENT_STREAM_HEARTBEAT_SECONDS,
            )
            if next_generation == observed_generation:
                latest_chat = await mgr.get_chat(chat_spec.id)
                if latest_chat is not None:
                    _, marked_stalled = await reconcile_pending_chat_stall(
                        mgr=mgr,
                        workspace=workspace,
                        chat_spec=latest_chat,
                    )
                    if marked_stalled:
                        observed_generation = runtime_event_stream_generation(
                            workspace_dir,
                        )
                        continue
                yield ": heartbeat\n\n"
            observed_generation = next_generation

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache, no-transform",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


@router.post("/{chat_id}/turns/{turn_id}/interrupt", response_model=dict)
async def interrupt_chat_turn(
    chat_id: str,
    turn_id: str,
    mgr: ChatManager = Depends(get_chat_manager),
    workspace=Depends(get_workspace),
) -> dict:
    """Interrupt a running chat turn."""
    chat_spec = await mgr.get_chat(chat_id)
    if not chat_spec:
        raise HTTPException(
            status_code=404,
            detail=f"Chat not found: {chat_id}",
        )
    stopped, stopped_run_id = await _request_stop_for_chat_turn(
        workspace=workspace,
        chat_spec=chat_spec,
        turn_id=turn_id,
    )
    orphan_reconciled = False
    if not stopped:
        _, orphan_reconciled = await settle_orphaned_pending_turn_on_stop(
            mgr=mgr,
            workspace=workspace,
            chat_spec=chat_spec,
            requested_chat_id=turn_id,
        )
        stopped = orphan_reconciled
    if stopped and not orphan_reconciled:
        workspace_dir = await _chat_workspace_or_404(
            chat_spec,
            root_workspace_dir=workspace.workspace_dir,
        )
        await record_chat_turn_stop_requested(
            workspace_dir=workspace_dir,
            agent_id=workspace.agent_id,
            chat_id=chat_spec.id,
            session_id=chat_spec.session_id,
            requested_chat_id=turn_id,
            resolved_chat_id=chat_spec.id,
        )
        if stopped_run_id:
            from ...presentation.jobs import (
                cancel_active_jobs_for_runtime_run,
            )

            await asyncio.to_thread(
                cancel_active_jobs_for_runtime_run,
                workspace_dir,
                stopped_run_id,
            )
    result = {"stopped": stopped, "chat_id": chat_spec.id, "turn_id": turn_id}
    settlement_snapshot = getattr(
        workspace.task_tracker, "get_settlement_snapshot", None,
    )
    if stopped and callable(settlement_snapshot):
        pending = await settlement_snapshot(chat_spec.id)
        if pending is not None:
            result.update(
                settlement_pending=True,
                settlement_status=pending["status"],
            )
    return result


@router.post("/{chat_id}/turns/{turn_id}/retry", response_model=dict)
async def retry_chat_turn(
    chat_id: str,
    turn_id: str,
    mgr: ChatManager = Depends(get_chat_manager),
    workspace=Depends(get_workspace),
) -> dict:
    """Prepare a retry for the latest replayable user text.

    v1 keeps retry non-destructive: it records the retry request and returns
    the text the frontend can resubmit, instead of silently launching a second
    hidden model turn.
    """
    chat_spec = await mgr.get_chat(chat_id)
    if not chat_spec:
        raise HTTPException(
            status_code=404,
            detail=f"Chat not found: {chat_id}",
        )
    meta = dict(chat_spec.meta or {})
    retry_text = str(meta.get("last_turn_text") or "").strip()
    workspace_dir = await _chat_workspace_or_404(
        chat_spec,
        root_workspace_dir=workspace.workspace_dir,
    )
    await record_chat_turn_started(
        workspace_dir=workspace_dir,
        agent_id=workspace.agent_id,
        chat_id=chat_spec.id,
        session_id=chat_spec.session_id,
        text_preview=retry_text or "重试请求",
        effective_workspace_dir=str(workspace_dir),
        run_id=f"retry:{turn_id}",
    )
    return {
        "queued": False,
        "reason": "retry_requires_frontend_resubmit",
        "chat_id": chat_spec.id,
        "turn_id": turn_id,
        "text": retry_text,
    }


def _retire_analysis_cleanup_job(job: AnalysisCleanupJob, workspace: Any) -> None:
    """Forget only the minimal server-bound identity persisted at deletion."""

    state_root = resolve_workspace_context(
        agent_home=workspace.workspace_dir,
        agent_id=workspace.agent_id,
        channel_meta={
            "state_workspace_dir": job.state_workspace_dir,
        },
    ).state_root
    AnalysisService(state_root, scope=job.session_state_id).retire()
    from ...agents.tools.presentation import _clear_analysis_validations_for_session

    result = _clear_analysis_validations_for_session(state_root, job.session_state_id)
    if result["presentation_validations_uninspected"]:
        logger.warning(
            "Analysis deletion left %s unreadable presentation validation records in %s",
            result["presentation_validations_uninspected"], state_root,
        )


async def _retry_pending_analysis_cleanup(
    mgr: ChatManager, workspace: Any, *, only_chat_ids: set[str] | None = None,
) -> list[str]:
    """Drain durable outbox after deletion or when a restarted console lists chats."""

    failures: list[str] = []
    for job in await mgr.pending_analysis_cleanup_jobs():
        if only_chat_ids is not None and job.chat_id not in only_chat_ids:
            continue
        try:
            await asyncio.to_thread(_retire_analysis_cleanup_job, job, workspace)
            await mgr.acknowledge_analysis_cleanup_job(job)
        except (OSError, ValueError) as exc:
            logger.exception("Analysis cleanup remains pending for deleted chat %s", job.chat_id)
            failures.append(job.chat_id)
    return failures


def _bound_chat_analysis_service(
    chat: ChatSpec, workspace: Any, grant_registry: WorkspaceGrantRegistry,
) -> tuple[AnalysisService, WorkspaceContext]:
    """Resolve only the analysis namespace owned by this persisted chat."""

    scope = canonical_chat_session_state_id(chat)
    meta = chat.meta or {}
    if meta.get(SESSION_STATE_ID_META_KEY) != scope:
        raise AnalysisError("Chat has no server-bound analysis identity")
    context = resolve_workspace_context(
        agent_home=workspace.workspace_dir,
        agent_id=workspace.agent_id,
        channel_meta=meta,
        grant_registry=grant_registry,
    )
    return AnalysisService(context.state_root, scope=scope), context


def _authorize_analysis_chart_source(
    service: AnalysisService,
    dataset_id: str,
    workspace_context: WorkspaceContext,
) -> None:
    """Apply the same source-file read boundary as the chat analysis tools."""

    from ...agents.tools.file_io import _resolve_file_path

    token = current_workspace_context.set(workspace_context)
    try:
        source_path = service.get_dataset(dataset_id)["source_path"]
        try:
            _resolve_file_path(source_path, operation="read")
        except ValueError as exc:
            raise AnalysisError("Analysis source is no longer authorized") from exc
    finally:
        current_workspace_context.reset(token)


def _assert_active_analysis_scope(service: AnalysisService) -> None:
    with service.active_scope():
        pass


def _analysis_chart_payload(
    service: AnalysisService,
    analysis_id: str,
    x_column: str,
    y_column: str,
    workspace_context: WorkspaceContext,
) -> dict[str, Any]:
    with service.active_scope():
        result = service.get_result(analysis_id)
        _authorize_analysis_chart_source(
            service, result["dataset_id"], workspace_context,
        )
        rows = result["rows"]
        if not isinstance(rows, list) or not 1 <= len(rows) <= 30:
            raise ValueError("Chart requires 1–30 result rows")
        if not x_column or not y_column or x_column == y_column:
            raise ValueError("Chart requires distinct label and value columns")
        points: list[dict[str, str]] = []
        seen_labels: set[str] = set()
        for row in rows:
            if not isinstance(row, dict) or x_column not in row or y_column not in row:
                raise ValueError("Chart columns must exist in every result row")
            label = row[x_column]
            raw_value = row[y_column]
            if label is None or raw_value is None or isinstance(raw_value, bool):
                raise ValueError("Chart does not allow missing labels or values")
            label_text = str(label)
            if not label_text or label_text in seen_labels:
                raise ValueError("Chart labels must be nonempty and unique")
            try:
                value = Decimal(str(raw_value))
            except (InvalidOperation, ValueError) as exc:
                raise ValueError("Chart values must be numeric") from exc
            if not value.is_finite() or abs(value) > Decimal("1000000000000000"):
                raise ValueError("Chart values are outside the supported visual range")
            seen_labels.add(label_text)
            points.append({"label": label_text, "value": str(raw_value)})
        evidence = result["evidence"]
        return {
            "analysis_id": analysis_id,
            "dataset_id": result["dataset_id"],
            "source_name": Path(evidence["source_path"]).name,
            "source_sha256": evidence["source_sha256"],
            "source_row_count": evidence["source_row_count"],
            "sheet_name": evidence["sheet_name"],
            "analysis_plan": result["plan"],
            "calculated_at": evidence["calculated_at"],
            "source_status": service.source_status(result["dataset_id"])["status"],
            "semantic_review": result["verification"]["semantic_review"]["status"],
            "warnings": result["warnings"],
            "x_column": x_column,
            "y_column": y_column,
            "points": points,
        }


@router.get("/{chat_id}/analysis/{analysis_id}/chart", response_model=dict)
async def get_analysis_chart(
    chat_id: str,
    analysis_id: str,
    x_column: str,
    y_column: str,
    mgr: ChatManager = Depends(get_chat_manager),
    workspace=Depends(get_workspace),
    grant_registry: WorkspaceGrantRegistry = Depends(get_workspace_grant_registry),
) -> dict[str, Any]:
    """Render data from a sealed result, never from model-supplied chart numbers."""

    chat = await mgr.get_chat(chat_id)
    if chat is None:
        raise HTTPException(status_code=404, detail="Chat not found")
    try:
        service, workspace_context = _bound_chat_analysis_service(
            chat, workspace, grant_registry,
        )
        payload = await asyncio.to_thread(
            _analysis_chart_payload,
            service, analysis_id, x_column, y_column, workspace_context,
        )
        # The chat can be deleted or its project grant revoked while the
        # sealed result is read in the worker thread. Revalidate both before
        # the response exposes points.
        current_chat = await mgr.get_chat(chat_id)
        if current_chat is None or current_chat.created_at != chat.created_at:
            raise AnalysisError("Analysis conversation is no longer available")
        current_service, current_context = _bound_chat_analysis_service(
            current_chat, workspace, grant_registry,
        )
        if current_service.root != service.root or current_context != workspace_context:
            raise AnalysisError("Analysis workspace authorization changed")
        await asyncio.to_thread(_assert_active_analysis_scope, current_service)
        return payload
    except (AnalysisError, WorkspaceContextError, OSError) as exc:
        raise HTTPException(status_code=404, detail="Analysis result unavailable") from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


async def _delete_chats_and_retire_analysis(
    chat_ids: list[str], mgr: ChatManager, workspace: Any,
) -> bool:
    deleted = await mgr.delete_chats_with_specs(chat_ids)
    failures = await _retry_pending_analysis_cleanup(
        mgr, workspace, only_chat_ids={chat.id for chat in deleted},
    )
    if failures:
        raise HTTPException(
            status_code=500,
            detail="Chat deleted, but its managed analysis cleanup is pending automatic retry",
        )
    return bool(deleted)


@router.post("/batch-delete", response_model=dict)
async def batch_delete_chats(
    chat_ids: list[str],
    mgr: ChatManager = Depends(get_chat_manager),
    workspace=Depends(get_workspace),
):
    """Delete chats by chat IDs.

    Args:
        chat_ids: List of chat IDs
        mgr: Chat manager dependency
    Returns:
        True if deleted, False if failed

    """
    deleted = await _delete_chats_and_retire_analysis(chat_ids, mgr, workspace)
    return {"deleted": deleted}


@router.get("/{chat_id}", response_model=ChatHistory)
async def get_chat(
    chat_id: str,
    mgr: ChatManager = Depends(get_chat_manager),
    session: SafeJSONSession = Depends(get_session),
    workspace=Depends(get_workspace),
):
    """Get detailed information about a specific chat by UUID.

    Args:
        request: FastAPI request (for agent context)
        chat_id: Chat UUID
        mgr: Chat manager dependency
        session: SafeJSONSession dependency

    Returns:
        ChatHistory with messages and status (idle/running)

    Repeating the same delete is safe: an already-absent chat returns
    ``{"deleted": false}`` instead of surfacing a spurious 404.
    """
    chat_spec = await mgr.get_chat(chat_id)
    if not chat_spec:
        raise HTTPException(
            status_code=404,
            detail=f"Chat not found: {chat_id}",
        )

    chat_spec = await _ensure_branch_runtime_event_lineage(
        chat_spec,
        mgr=mgr,
        session=session,
        root_workspace_dir=workspace.workspace_dir,
    )
    session_state_id = await resolve_chat_session_state_id(mgr, chat_spec)

    # Session memory, tracker state, and the compact event preview live in
    # independent stores. Reading them serially made every history open pay
    # the sum of four I/O latencies, which is especially visible immediately
    # after launch or on large session files.
    state, status, active_run, turn_event_preview = await asyncio.gather(
        session.get_session_state_dict(
            session_state_id,
            chat_spec.user_id,
            chat_spec.channel,
        ),
        workspace.task_tracker.get_status(chat_id),
        workspace.task_tracker.get_snapshot(chat_id),
        _load_turn_event_preview(
            chat_spec,
            root_workspace_dir=workspace.workspace_dir,
        ),
    )
    chat_spec, marked_stalled = await mark_chat_stalled_if_needed(
        mgr=mgr,
        workspace=workspace,
        chat_spec=chat_spec,
        tracker_status=status,
        active_run=active_run,
        turn_event_preview=turn_event_preview,
    )
    if marked_stalled:
        turn_event_preview = await _load_turn_event_preview(
            chat_spec,
            root_workspace_dir=workspace.workspace_dir,
        )
    pending_turn = pending_turn_dict(chat_spec)
    canonical_identity = await resolve_canonical_chat_runtime_identity(
        chat_spec=chat_spec,
        tracker_status=status,
        active_run=active_run,
        run_hint=str((active_run or {}).get("attempt_id") or "").strip() or None,
        agent_id=workspace.agent_id,
        root_workspace_dir=workspace.workspace_dir,
    )
    runtime_state = build_chat_runtime_state(
        chat_spec=chat_spec,
        tracker_status=status,
        active_run=active_run,
        turn_event_preview=turn_event_preview,
        pending_turn=pending_turn,
        canonical_identity=canonical_identity,
    )
    pending_user_message = None
    if runtime_state is not None and runtime_state.status in {
        "running",
        "stalled",
    }:
        pending_user_message = pending_turn_text(pending_turn)
    if str(pending_turn.get("status") or "").lower() == "stalled":
        status = "stalled"
    runtime_control_events = await _load_persisted_runtime_control_events(
        chat_spec,
        root_workspace_dir=workspace.workspace_dir,
    )
    ququ_continuations = completed_ququ_continuations(chat_spec.meta)
    public_chat_meta = without_ququ_continuation_storage(chat_spec.meta)

    if not state:
        return ChatHistory(
            messages=_merge_persisted_runtime_control_messages(
                [],
                runtime_control_events,
                latest_client_turn_id=str(
                    (chat_spec.meta or {}).get("last_turn_client_id") or "",
                ),
            ),
            status=status,
            chat_id=chat_spec.id,
            session_id=chat_spec.session_id,
            name=chat_spec.name,
            user_id=chat_spec.user_id,
            channel=chat_spec.channel,
            meta=public_chat_meta,
            ququ_continuations=ququ_continuations,
            created_at=chat_spec.created_at,
            updated_at=chat_spec.updated_at,
            pinned=chat_spec.pinned,
            archived=chat_spec.archived,
            pending_user_message=pending_user_message,
            active_run=active_run,
            turn_event_preview=turn_event_preview,
            runtime_state=runtime_state,
        )
    messages = await legacy_memory_state_to_chat_messages(
        state.get("agent", {}).get("memory", {}),
    )
    messages = _merge_persisted_runtime_control_messages(
        messages,
        runtime_control_events,
        latest_client_turn_id=str(
            (chat_spec.meta or {}).get("last_turn_client_id") or "",
        ),
    )
    return ChatHistory(
        messages=messages,
        status=status,
        chat_id=chat_spec.id,
        session_id=chat_spec.session_id,
        name=chat_spec.name,
        user_id=chat_spec.user_id,
        channel=chat_spec.channel,
        meta=public_chat_meta,
        ququ_continuations=ququ_continuations,
        created_at=chat_spec.created_at,
        updated_at=chat_spec.updated_at,
        pinned=chat_spec.pinned,
        archived=chat_spec.archived,
        pending_user_message=pending_user_message,
        active_run=active_run,
        turn_event_preview=turn_event_preview,
        runtime_state=runtime_state,
    )


@router.post("/{chat_id}/branch", response_model=ChatHistory)
async def branch_chat(
    chat_id: str,
    request: ChatBranchRequest,
    mgr: ChatManager = Depends(get_chat_manager),
    session: SafeJSONSession = Depends(get_session),
    workspace=Depends(get_workspace),
) -> ChatHistory:
    """Create an independent chat branch before a persisted user message."""

    source = await mgr.get_chat(chat_id)
    if source is None:
        raise HTTPException(
            status_code=404,
            detail=f"Chat not found: {chat_id}",
        )

    tracker_status = await workspace.task_tracker.get_status(chat_id)
    active_run = await workspace.task_tracker.get_snapshot(chat_id)
    if (
        tracker_status == "running"
        or str((active_run or {}).get("status") or "") == "running"
    ):
        raise HTTPException(
            status_code=409,
            detail="Stop the active response before editing or resending a message",
        )

    message_id = request.user_message_id.strip()
    if not message_id:
        raise HTTPException(
            status_code=400,
            detail="user_message_id is required",
        )

    source_state = await session.get_session_state_dict(
        await resolve_chat_session_state_id(mgr, source),
        source.user_id,
        source.channel,
        allow_not_exist=False,
    )
    source_agent = source_state.get("agent")
    if not isinstance(source_agent, dict):
        raise HTTPException(
            status_code=404,
            detail="The selected chat has no restorable agent state",
        )

    source_memory = source_agent.get("memory")
    if not isinstance(source_memory, dict):
        raise HTTPException(
            status_code=404,
            detail="The selected chat has no restorable message history",
        )

    branch_memory = _branch_memory_state(
        source_memory,
        user_message_id=message_id,
    )
    messages = await legacy_memory_state_to_chat_messages(branch_memory)
    branch_agent = deepcopy(source_agent)
    branch_agent["memory"] = branch_memory
    branch_agent["plan_notebook"] = legacy_empty_plan_notebook_state()
    branch_agent["last_context_manifest"] = None

    (
        branch_lineage,
        inherited_runtime_events,
    ) = await _derive_branch_runtime_event_lineage(
        source,
        source_memory,
        user_message_id=message_id,
        root_workspace_dir=getattr(workspace, "workspace_dir", None),
    )

    branch_meta = deepcopy(source.meta or {})
    branch_meta.pop("pending_turn", None)
    branch_meta["branched_from_chat_id"] = source.id
    branch_meta["branched_from_user_message_id"] = message_id
    if branch_lineage:
        branch_meta[BRANCH_RUNTIME_EVENT_LINEAGE_META_KEY] = branch_lineage
    else:
        branch_meta.pop(BRANCH_RUNTIME_EVENT_LINEAGE_META_KEY, None)
    branch_id = str(uuid4())
    branch_meta = new_chat_meta(
        branch_meta,
        chat_id=branch_id,
        channel=source.channel,
    )
    branch_session_id = f"{source.channel}:branch:{branch_id}"
    branch_meta = copy_ququ_continuations_for_branch(
        branch_meta,
        visible_messages=messages,
        branch_chat_id=branch_id,
        branch_session_id=branch_session_id,
        user_id=source.user_id,
        channel=source.channel,
    )
    branch_name = (
        request.name.strip()
        if isinstance(request.name, str) and request.name.strip()
        else f"{source.name or '新会话'} · 分支"
    )
    branch_spec = ChatSpec(
        id=branch_id,
        name=branch_name,
        session_id=branch_session_id,
        user_id=source.user_id,
        channel=source.channel,
        meta=branch_meta,
    )

    created = await mgr.create_chat(branch_spec)
    try:
        await session.update_session_state(
            session_id=await resolve_chat_session_state_id(mgr, created),
            key="agent",
            value=branch_agent,
            user_id=created.user_id,
            channel=created.channel,
        )
    except Exception:
        await mgr.delete_chats([created.id])
        raise

    messages = _merge_persisted_runtime_control_messages(
        messages,
        inherited_runtime_events,
    )
    return ChatHistory(
        messages=messages,
        status="idle",
        chat_id=created.id,
        session_id=created.session_id,
        name=created.name,
        user_id=created.user_id,
        channel=created.channel,
        meta=without_ququ_continuation_storage(created.meta),
        ququ_continuations=completed_ququ_continuations(created.meta),
        created_at=created.created_at,
        updated_at=created.updated_at,
        pinned=created.pinned,
        archived=created.archived,
    )


@router.post("/{chat_id}/legacy-history-recovery", response_model=ChatHistory)
async def recover_legacy_history(
    chat_id: str,
    mgr: ChatManager = Depends(get_chat_manager),
    session: SafeJSONSession = Depends(get_session),
) -> ChatHistory:
    """Copy one explicitly selected quarantined legacy history.

    Ambiguous legacy bytes are never assigned to an existing owner. The user
    selects a quarantined chat, the server verifies the source reference that
    it persisted during isolation, and a new canonical chat receives a copy.
    The shared source and every quarantined chat remain untouched.
    """

    selected = await mgr.get_chat(chat_id)
    if selected is None:
        raise HTTPException(
            status_code=404,
            detail=f"Chat not found: {chat_id}",
        )

    selected_meta = selected.meta or {}
    if (
        selected_meta.get(STATE_MIGRATION_STATE_META_KEY)
        != "needs_manual_recovery"
    ):
        raise HTTPException(
            status_code=409,
            detail="This chat has no quarantined legacy history to recover",
        )

    source_reference = selected_meta.get(LEGACY_SOURCE_REFERENCE_META_KEY)
    if not isinstance(source_reference, dict):
        raise HTTPException(
            status_code=409,
            detail="The quarantined legacy source reference is unavailable",
        )
    source_session_id = str(source_reference.get("session_id") or "").strip()
    source_user_id = str(source_reference.get("user_id") or "").strip()
    source_channel = str(source_reference.get("channel") or "").strip()
    if (
        not source_session_id
        or source_user_id != selected.user_id
        or source_channel != selected.channel
        or source_session_id != selected.session_id
    ):
        raise HTTPException(
            status_code=409,
            detail="The quarantined legacy source no longer matches this chat",
        )

    source_state = await session.get_session_state_dict(
        source_session_id,
        source_user_id,
        source_channel,
        allow_not_exist=False,
    )
    source_agent = source_state.get("agent")
    if not isinstance(source_agent, dict):
        raise HTTPException(
            status_code=404,
            detail="The quarantined legacy source has no restorable agent state",
        )
    source_memory = source_agent.get("memory")
    if not isinstance(source_memory, dict):
        raise HTTPException(
            status_code=404,
            detail="The quarantined legacy source has no restorable message history",
        )

    recovered_agent = deepcopy(source_agent)
    recovered_agent["memory"] = deepcopy(source_memory)
    recovered_agent["plan_notebook"] = legacy_empty_plan_notebook_state()
    recovered_agent["last_context_manifest"] = None

    recovered_id = str(uuid4())
    recovered_meta = new_chat_meta(
        {
            LEGACY_RECOVERY_PROVENANCE_META_KEY: {
                "method": "explicit_copy",
                "selection": "all_preserved_messages",
                "selected_from_chat_id": selected.id,
                "source_session_id": source_session_id,
                "source_user_id": source_user_id,
                "source_channel": source_channel,
                "source_memory_sha256": _stable_json_sha256(source_memory),
                "recovered_at": datetime.now(timezone.utc).isoformat(),
            },
        },
        chat_id=recovered_id,
        channel=selected.channel,
    )
    recovered_spec = ChatSpec(
        id=recovered_id,
        name=f"{selected.name or '旧会话'} · 恢复副本",
        session_id=f"{selected.channel}:legacy-recovery:{recovered_id}",
        user_id=selected.user_id,
        channel=selected.channel,
        meta=recovered_meta,
    )
    created = await mgr.create_chat(recovered_spec)
    try:
        await session.update_session_state(
            session_id=await resolve_chat_session_state_id(mgr, created),
            key="agent",
            value=recovered_agent,
            user_id=created.user_id,
            channel=created.channel,
        )
    except Exception:
        await mgr.delete_chats([created.id])
        raise

    messages = await legacy_memory_state_to_chat_messages(source_memory)
    return ChatHistory(
        messages=messages,
        status="idle",
        chat_id=created.id,
        session_id=created.session_id,
        name=created.name,
        user_id=created.user_id,
        channel=created.channel,
        meta=created.meta,
        created_at=created.created_at,
        updated_at=created.updated_at,
        pinned=created.pinned,
        archived=created.archived,
    )


@router.get("/{chat_id}/context-stats", response_model=dict)
async def get_chat_context_stats(
    chat_id: str,
    request: Request,
    mgr: ChatManager = Depends(get_chat_manager),
    session: SafeJSONSession = Depends(get_session),
    workspace=Depends(get_workspace),
):
    """Estimate current context usage for one chat."""
    chat_spec = await mgr.get_chat(chat_id)
    if not chat_spec:
        raise HTTPException(
            status_code=404,
            detail=f"Chat not found: {chat_id}",
        )

    context_manager = workspace.context_manager
    if context_manager is None:
        raise HTTPException(
            status_code=503,
            detail="Context manager not initialized",
        )

    state = await session.get_session_state_dict(
        session_id=await resolve_chat_session_state_id(mgr, chat_spec),
        user_id=chat_spec.user_id,
        channel=chat_spec.channel,
        allow_not_exist=True,
    )
    memory_state = state.get("agent", {}).get("memory", {})
    memory = context_manager.get_agent_context()
    try:
        memory.load_state_dict(memory_state, strict=False)
    except Exception:
        logger.warning(
            "Failed to load memory state for chat context stats: chat_id=%s",
            chat_id,
            exc_info=True,
        )
        memory.load_state_dict({}, strict=False)

    requested_window = int(
        getattr(workspace.config.running, "max_input_length", 0) or 0,
    )
    profile_payload = _context_profile_payload(workspace, request)
    # The visual usage ratio follows the active product policy target rather
    # than the physical hard limit. This makes the warning and compaction
    # threshold agree after every model switch.
    policy_payload = profile_payload.get("context_policy") or {}
    budget_window = int(
        policy_payload.get("target_input_tokens")
        or profile_payload.get("effective_window")
        or requested_window
        or 0,
    )
    stats = await memory.estimate_tokens(max_input_length=budget_window)
    from ...agents.context.explanation import build_context_explanation

    agent_state = state.get("agent")
    manifest = (
        agent_state.get("last_context_manifest")
        if isinstance(agent_state, dict)
        else None
    )
    explanation = build_context_explanation(
        stats=stats,
        manifest=manifest if isinstance(manifest, dict) else None,
    )
    return {
        "total_messages": int(stats.get("total_messages", 0) or 0),
        "compressed_summary_tokens": int(
            stats.get("compressed_summary_tokens", 0) or 0,
        ),
        "messages_tokens": int(stats.get("messages_tokens", 0) or 0),
        "estimated_tokens": int(stats.get("estimated_tokens", 0) or 0),
        "max_input_length": int(stats.get("max_input_length", 0) or 0),
        "context_usage_ratio": float(
            stats.get("context_usage_ratio", 0.0) or 0.0,
        ),
        **explanation,
        **profile_payload,
    }


@router.put("/{chat_id}", response_model=ChatSpec)
async def update_chat(
    chat_id: str,
    spec: ChatUpdate,
    mgr: ChatManager = Depends(get_chat_manager),
):
    """Update an existing chat.

    Args:
        chat_id: Chat UUID
        spec: Partial chat update payload
        mgr: Chat manager dependency

    Returns:
        Updated chat spec

    Raises:
        HTTPException: If chat not found (404)
    """
    updated = await mgr.patch_chat(chat_id, spec)
    if updated is None:
        raise HTTPException(
            status_code=404,
            detail=f"Chat not found: {chat_id}",
        )
    return _public_chat_spec(updated)


@router.delete("/{chat_id}", response_model=dict)
async def delete_chat(
    chat_id: str,
    mgr: ChatManager = Depends(get_chat_manager),
    workspace=Depends(get_workspace),
):
    """Delete a chat by UUID.

    Note: This only deletes the chat spec (UUID mapping).
    JSONSession state is NOT deleted.

    Args:
        chat_id: Chat UUID
        mgr: Chat manager dependency

    Returns:
        ``deleted`` is true when a stored chat was removed and false when the
        requested chat was already absent. Repeated deletes are intentionally
        idempotent.
    """
    deleted = await _delete_chats_and_retire_analysis([chat_id], mgr, workspace)
    return {"deleted": bool(deleted)}
