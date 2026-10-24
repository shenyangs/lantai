# -*- coding: utf-8 -*-
# pylint: disable=unused-argument too-many-branches too-many-statements
from __future__ import annotations

from lantai.utils.async_iterator import DelegatedAsyncIterator

import asyncio
import hashlib
import json
import logging
import re
import time
from dataclasses import replace
from datetime import datetime
from functools import wraps
from pathlib import Path
from typing import TYPE_CHECKING, Any, AsyncGenerator
from uuid import uuid4

import frontmatter as fm
from dotenv import load_dotenv

from .command_dispatch import (
    _get_last_user_text,
    _is_command,
    run_command_path,
)
from .query_error_dump import write_query_error_dump
from .title_generator import _semantic_fallback_title
from .mission_dispatch import maybe_handle_mission_command
from .session import SafeJSONSession
from .conversation_identity import resolve_chat_session_state_id
from .utils import build_env_context
from ..channels.schema import DEFAULT_CHANNEL
from ..concurrency import get_runtime_concurrency_governor
from ...agents.legacy_lantai_agent import LegacyLantaiAgent
from ...agents.capture_scope import should_capture_global_memory
from ...agents.model_factory import create_model_client
from ...agents.work_edit import (
    trusted_private_editor_binding_from_context as _trusted_private_editor_binding_from_channel_meta,
)
from ...agents.tool_registry import (
    build_lantai_tool_registry,
    build_memory_tool_registry,
    build_mcp_client_registry,
    build_tool_policy_context,
)
from ...exceptions import AgentStateError, convert_model_exception
from ...utils.async_settlement import await_settled
from ...agents.utils.file_handling import (
    read_text_file_with_encoding_fallback,
)
from ...config.config import load_agent_config
from ...constant import BUILTIN_TEAM_AGENT_CONFIGS, WORKING_DIR
from ...runtime import (
    RuntimeEffectRecoveryCapability,
    RuntimeEffectJournal,
    RuntimeEvent,
    RuntimeInput,
    RuntimeMessage,
    RuntimeRequestContext,
    RuntimeRunStore,
    RuntimeRunStoreError,
)
from ...runtime.hosted_collaboration import close_public_room_snapshot
from ...runtime.artifact_effects import RuntimeArtifactEffectJournal
from ...runtime.public_reasoning import SUBAGENT_INTERNAL_ACTIVITY_STATUS
from ...runtime.turn_assembly_oracle import (
    compare_turn_assembly_snapshot,
    log_turn_assembly_oracle_report,
    observe_native_input_boundary,
    observe_prepared_turn_sources,
    unavailable_turn_assembly_oracle_report,
)
from ...runtime.turn_assembly_input import (
    runtime_input_from_turn_assembly_snapshot,
    runtime_inputs_are_exactly_equivalent,
)
from ...runtime.adapters import NativeLantaiRuntime
from ...runtime.adapters.legacy_turn_assembly import (
    capture_shadow_turn_assembly,
)
from ...runtime.converters import (
    LegacyMsg,
    is_legacy_app_base_exception,
    legacy_assistant_text_msg,
    legacy_create_plan_notebook,
    legacy_msg_to_runtime_messages,
    legacy_query_messages_to_runtime_messages,
    legacy_rewrite_last_message_text,
    legacy_stream_item_to_channel_events,
    runtime_message_to_legacy_msg,
    runtime_messages_to_legacy_msgs,
    runtime_event_to_legacy_stream_item,
)
from ...runtime.tools import (
    LantaiToolRegistry,
    ToolOutputSemantics,
    ToolPolicyContext,
    ToolSpec,
    runtime_blocked_tool_names,
)
from ...runtime.runner import RuntimeRunner
from ...runtime.session_turn_lifecycle import post_answer_admission
from ...runtime.work_method_resolution import effective_work_method_id_from_metadata
from ...security.beta_access import AccessBoundary
from ..beta_access.admission import BetaAccessAdmissionGuard
from ..beta_access.wiring import protected_write_denial_reason

if TYPE_CHECKING:
    from ...agents.memory import BaseMemoryManager
    from ...agents.context import BaseContextManager

class _NativeMemoryLoadCancelled(asyncio.CancelledError):
    """Preserve loaded state when cancellation interrupts history admission."""


class _NativeMemoryLoadError(AgentStateError):
    """Abort a turn without persisting a partially prepared agent."""


logger = logging.getLogger(__name__)

_FOREGROUND_MEMORY_RECALL_TIMEOUT_SECONDS = 2.0
_FOREGROUND_MEMORY_LOAD_TIMEOUT_SECONDS = 3.0
_FOREGROUND_CONTEXT_PREFLIGHT_SILENT_TIMEOUT_SECONDS = 3.0
_FOREGROUND_CONTEXT_PREFLIGHT_ACTIVE_TIMEOUT_SECONDS = 90.0
_CONTEXT_PREFLIGHT_STATUS_QUEUE_LIMIT = 64

_RUNTIME_TURN_TERMINAL_TYPES = {
    "turn.completed",
    "turn.failed",
    "turn.interrupted",
    "turn.stalled",
}
_NATIVE_MEMORY_COMMIT_EVENT_TYPES = {
    "message.delta",
    "message.completed",
    "tool.call.started",
    "tool.call.delta",
    "tool.call.completed",
    "tool.started",
    "tool.completed",
    "approval.requested",
    "approval.resolved",
    "memory.updated",
    "plan.updated",
    *_RUNTIME_TURN_TERMINAL_TYPES,
}
_NATIVE_LEDGER_EVENT_KINDS = {
    "tool.call.started": "chat.tool_call.started",
    "tool.call.delta": "chat.tool_call.started",
    "tool.call.completed": "chat.tool_call.finished",
    "approval.requested": "chat.approval.requested",
    "approval.resolved": "chat.approval.resolved",
    "runtime.warning": "chat.system.event",
    "runtime.error": "chat.error",
}
_SUBAGENT_NATIVE_LEDGER_EVENT_KINDS = {
    "turn.started": "chat.turn.started",
    "message.started": "chat.message.started",
    "message.delta": "chat.message.delta",
    "message.completed": "chat.message.completed",
    "turn.completed": "chat.turn.settled",
    "turn.interrupted": "chat.turn.settled",
    "turn.failed": "chat.turn.settled",
    "turn.stalled": "chat.turn.settled",
}
_CHAT_LEDGER_STATUSES = {
    "info",
    "running",
    "interrupted",
    "finished",
    "completed",
    "failed",
    "stalled",
}


async def _runtime_events_with_terminal_boundary(events):
    """Keep the runtime terminal event authoritative at the app boundary."""

    terminal_seen = False
    try:
        async with DelegatedAsyncIterator(
            events
        ) as _owned_events_1:
            async for event in _owned_events_1:
                if event.type in _RUNTIME_TURN_TERMINAL_TYPES:
                    terminal_seen = True
                try:
                    yield event
                except asyncio.CancelledError as _stream_cancel:
                    _owned_events_1.request_cancel(_stream_cancel)
    except Exception as exc:
        if not terminal_seen or _is_provider_content_filter_error(exc):
            raise
        logger.debug(
            "Runtime raised after emitting a terminal event; keeping the "
            "structured terminal result.",
            exc_info=True,
        )


def _bind_request_external_file_grants(channel_meta: object) -> int:
    """Bind only server-issued typed grants to the current async tool context."""

    from ...security.file_access_grants import (
        ExternalFileGrant,
        set_current_external_file_grants,
    )

    raw_grants = (
        channel_meta.get("_external_file_grants")
        if isinstance(channel_meta, dict)
        else None
    )
    grants = tuple(
        grant
        for grant in (raw_grants or ())
        if isinstance(grant, ExternalFileGrant)
    )
    set_current_external_file_grants(grants)
    return len(grants)


def _trusted_current_attachments_from_channel_meta(
    channel_meta: object,
) -> list[dict[str, Any]]:
    """Cross-check the server manifest against typed read grants.

    Channel metadata is otherwise user-shaped input.  Only the Console route's
    in-memory ``ExternalFileGrant`` objects make a canonical local path trusted
    enough to project into the model context.
    """

    from ...security.file_access_grants import ExternalFileGrant

    if not isinstance(channel_meta, dict):
        return []
    grants = {
        grant.path: grant
        for grant in (channel_meta.get("_external_file_grants") or ())
        if isinstance(grant, ExternalFileGrant)
        and "read" in grant.operations
        and grant.expires_at > time.time()
    }
    raw_manifest = channel_meta.get("_trusted_current_attachments")
    if not isinstance(raw_manifest, list) or not grants:
        return []

    trusted: list[dict[str, Any]] = []
    seen: set[str] = set()
    for raw_entry in raw_manifest:
        if not isinstance(raw_entry, dict):
            continue
        path = str(raw_entry.get("canonical_path") or "").strip()
        grant = grants.get(path)
        if grant is None or path in seen:
            continue
        display_name = str(raw_entry.get("display_name") or Path(path).name)
        display_name = re.sub(r"[\x00-\x1f\x7f]", "", display_name).strip()
        media_type = str(
            raw_entry.get("media_type") or "application/octet-stream",
        ).strip()
        entry = {
            "display_name": display_name[:240] or Path(path).name,
            "canonical_path": path,
            "media_type": media_type[:200] or "application/octet-stream",
            "size_bytes": grant.st_size,
            "schema_version": 1,
            "digest": f"sha256:{grant.sha256}" if grant.sha256 else None,
            "version": 1,
        }
        for key in (
            "stored_name",
            "attachment_role",
            "source_id",
            "source_chat_id",
            "source_command_id",
            "grant_ref",
            "grant_state",
        ):
            value = raw_entry.get(key)
            if isinstance(value, str) and value.strip():
                entry[key] = value.strip()
        if raw_entry.get("workspace_staged") is True:
            entry["workspace_staged"] = True
        trusted.append(entry)
        seen.add(path)
    return trusted


def _runtime_attachment_manifest_message(
    request_context: dict[str, Any],
) -> RuntimeMessage | None:
    """Build a provider-neutral, internal attachment handoff message."""

    manifest = request_context.get("trusted_current_attachments")
    if not isinstance(manifest, list) or not manifest:
        return None
    payload = json.dumps(manifest, ensure_ascii=False, separators=(",", ":"))
    return RuntimeMessage.text(
        "system",
        (
            "<lantai_current_turn_attachments>\n"
            f"{payload}\n"
            "These are the files attached to the current user turn. "
            "canonical_path is a server-validated exact local path and is "
            "already authorized for reading. When workspace_staged is true, "
            "the runtime has already copied that file into the current topic "
            "workspace: use canonical_path directly and do not copy it again. "
            "Also, do not search for the file by display name with glob, find, or "
            "directory scans.\n"
            "</lantai_current_turn_attachments>"
        ),
        metadata={
            "runtime_owned": True,
            "visibility": "internal",
            "kind": "trusted_attachment_manifest",
            "instruction_trust": "trusted_system",
            "instruction_origin": "runtime.trusted_attachment_manifest",
        },
    )


def _runtime_tool_event_key(event: RuntimeEvent) -> str:
    payload = event.payload or {}
    return str(
        event.item_id
        or payload.get("id")
        or payload.get("call_id")
        or payload.get("tool_call_id")
        or "",
    ).strip()


def _runtime_approval_event_key(event: RuntimeEvent) -> str:
    payload = event.payload or {}
    return str(
        payload.get("request_id")
        or payload.get("approval_request_id")
        or event.item_id
        or "",
    ).strip()


def _coerce_agent_request(request: Any) -> Any:
    """Accept both AgentRequest objects and dict payloads at runner edges."""
    if isinstance(request, dict):
        from ..channels.runtime_schema import AgentRequest

        return AgentRequest.model_validate(request)
    return request


def _is_provider_content_filter_error(exc: BaseException) -> bool:
    """Detect provider-side prompt/content-filter rejections.

    These are model safety rejections, not application crashes. Treating them
    as ordinary runtime failures makes the chat show a scary red error card.
    """

    parts: list[str] = []
    seen: set[int] = set()
    current: BaseException | None = exc
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        parts.append(type(current).__name__)
        parts.append(str(current))
        for attr in ("body", "details", "message"):
            value = getattr(current, attr, None)
            if value:
                parts.append(str(value))
        current = current.__cause__ or current.__context__

    text = " ".join(parts).lower()
    return (
        "content_filter" in text
        or ("high risk" in text and "prompt" in text)
        or ("request was rejected" in text and "prompt" in text)
    )


def _replace_tool_result_output(block: Any, text: str) -> bool:
    if isinstance(block, dict):
        if block.get("type") != "tool_result":
            return False
        block["output"] = [{"type": "text", "text": text}]
        return True

    if getattr(block, "type", None) != "tool_result":
        return False
    try:
        setattr(block, "output", [{"type": "text", "text": text}])
        return True
    except Exception:
        logger.debug("Failed to sanitize tool_result block", exc_info=True)
        return False


def _sanitize_recent_tool_results_after_content_filter(agent: Any) -> int:
    """Shrink current-turn tool results after provider content filtering.

    The raw tool output is often the thing that trips the next model request.
    Keep the transcript shape, but replace recent tool result bodies with a
    short marker before saving the session state.
    """

    memory = getattr(agent, "memory", None)
    content = getattr(memory, "content", None)
    if not isinstance(content, list):
        return 0

    last_user_index = -1
    for index, item in enumerate(content):
        msg = item[0] if isinstance(item, tuple) and item else None
        if getattr(msg, "role", None) == "user":
            last_user_index = index

    replacement = (
        "[工具结果已收起：模型供应商拒绝了包含高风险内容的原始上下文。"
        "请收窄查询范围或指定更垂直的信源后重试。]"
    )
    sanitized = 0
    for item in content[last_user_index + 1 :]:
        msg = item[0] if isinstance(item, tuple) and item else None
        blocks = getattr(msg, "content", None)
        if not isinstance(blocks, list):
            continue
        for block in blocks:
            if _replace_tool_result_output(block, replacement):
                sanitized += 1

    return sanitized


def _provider_content_filter_fallback_msg() -> Any:
    return legacy_assistant_text_msg(
        (
            "这次不是工具崩了，是模型供应商拒绝了刚才那段包含高风险热榜内容的上下文。"
            "我已经把本轮原始工具结果从会话上下文里收起，避免继续触发同一个拦截。\n\n"
            "如果你是在测试 Guanlan：工具调用是通的，但泛热榜容易混进无关敏感内容。"
            "更稳的测法是直接限定科技信源或具体主题，比如“AI 产品新闻 site:ithome.com”"
            "或“2026 年 5 月 大模型融资 技术进展”。"
        ),
    )


class AgentRunner:
    def __init__(
        self,
        agent_id: str = "default",
        workspace_dir: Path | None = None,
        task_tracker: Any | None = None,
        beta_access_admission_guard: BetaAccessAdmissionGuard | None = None,
    ) -> None:
        self.framework_type = "lantai"
        self.agent_id = agent_id  # Store agent_id for config loading
        self.workspace_dir = (
            workspace_dir  # Store workspace_dir for prompt building
        )
        self._chat_manager = None  # Store chat_manager reference
        self._mcp_manager = None  # MCP client manager for hot-reload
        self._workspace: Any = None  # Workspace instance for control commands
        self.memory_manager: BaseMemoryManager | None = None
        self.context_manager: BaseContextManager | None = None
        self._task_tracker = task_tracker  # Task tracker for background tasks
        self._beta_access_admission_guard = beta_access_admission_guard
        self.session: SafeJSONSession | None = None
        self._started = False

    def set_chat_manager(self, chat_manager):
        """Set chat manager for auto-registration.

        Args:
            chat_manager: ChatManager instance
        """
        self._chat_manager = chat_manager

    def set_mcp_manager(self, mcp_manager):
        """Set MCP client manager for hot-reload support.

        Args:
            mcp_manager: MCPClientManager instance
        """
        self._mcp_manager = mcp_manager

    def set_workspace(self, workspace):
        """Set workspace for control command handlers.

        Args:
            workspace: Workspace instance
        """
        self._workspace = workspace

    @staticmethod
    def _parse_skill_query(
        query: str,
    ) -> tuple[str, str] | None:
        """Parse ``/name [input]`` or ``/[name with spaces] [input]``.

        Bracket form ``/[...]`` handles spaces in skill names and
        bypasses built-in command priority.

        Returns ``(skill_name, user_input)`` or ``None``.
        """
        stripped = query.strip()
        if not stripped.startswith("/"):
            return None

        rest = stripped[1:]  # drop leading /

        # /[skill name] input — bracket form
        if rest.startswith("["):
            close = rest.find("]")
            if close < 0:
                return None
            name = rest[1:close].strip().lower()
            user_input = rest[close + 1 :].strip()
            return (name, user_input) if name else None

        # /name input — plain form
        parts = rest.split(None, 1)
        if not parts:
            return None
        name = parts[0].lower()
        user_input = parts[1] if len(parts) > 1 else ""
        return (name, user_input) if name else None

    @staticmethod
    def _maybe_inject_skill(
        query: str | None,
        msgs: list,
        skills: dict,
    ) -> Any | None:
        """Handle ``/<skill_name> [input]`` or ``/[skill name] [input]``.

        *skills* is ``agent.toolkit.skills`` — already resolved for
        the current channel during agent init.  Hot-reload safe because
        the agent is recreated on every query.

        Returns a legacy assistant message to short-circuit, or ``None``
        to continue to the LLM with rewritten ``msgs``.
        """
        if not query or not query.startswith("/") or not msgs:
            return None

        parsed = AgentRunner._parse_skill_query(query)
        if not parsed:
            return None
        name, user_input = parsed

        # Lookup by folder name
        skill = next(
            (
                s
                for s in skills.values()
                if Path(s["dir"]).name.lower() == name
            ),
            None,
        )
        if not skill:
            return None

        skill_dir = Path(skill["dir"])
        skill_md = skill_dir / "SKILL.md"
        if not skill_md.exists():
            return None

        raw = read_text_file_with_encoding_fallback(skill_md)
        post = fm.loads(raw)
        display_name = post.get("name") or name

        # /<name> without input → return skill info.
        if not user_input:
            desc = post.get("description") or "No description."
            logger.info("Skill info: %s", name)
            return legacy_assistant_text_msg(
                (
                    f"**{name}**\n\n"
                    f"- **command**: `/{name} <input>` to invoke\n"
                    f"- **name**: {display_name}\n"
                    f"- **description**: {desc}\n"
                    f"- **path**: `{skill_dir}`"
                ),
            )

        # /<name> <input> → rewrite user message with skill body.
        merged = (
            f"Use the [{display_name}] skill in "
            f"`{skill_dir}` to fulfill "
            f"user's task: {user_input}\n\n"
            f"{post.content}"
        )
        AgentRunner._rewrite_last_message_text(msgs, merged)
        logger.info("Skill invocation: %s", name)
        return None

    @staticmethod
    def _rewrite_last_message_text(
        msgs: list,
        new_text: str,
    ) -> None:
        """Rewrite the text content of the last message in-place."""
        legacy_rewrite_last_message_text(msgs, new_text)

    @staticmethod
    def _runtime_messages_from_legacy_msgs(msgs: list) -> list[RuntimeMessage]:
        """Convert current query messages into Lantai runtime messages."""
        return legacy_query_messages_to_runtime_messages(msgs)

    @staticmethod
    def _ensure_runtime_message_ids(messages: list[RuntimeMessage]) -> None:
        """Assign stable per-turn ids before bridging into legacy state."""

        for message in messages:
            if not message.id:
                message.id = f"runtime-{uuid4().hex}"

    @staticmethod
    def _is_runtime_presentation_only(message: RuntimeMessage) -> bool:
        """Keep UI-only Runtime projections outside future model context."""

        metadata = (
            message.metadata if isinstance(message.metadata, dict) else {}
        )
        for container in (metadata, metadata.get("metadata")):
            if (
                isinstance(container, dict)
                and container.get("runtime_presentation_only") is True
            ):
                return True
        return False

    @staticmethod
    async def _runtime_messages_from_agent_memory(
        agent: Any,
    ) -> list[RuntimeMessage]:
        """Read the loaded legacy memory into runtime messages for Native."""

        memory = getattr(agent, "memory", None)
        get_memory = getattr(memory, "get_memory", None)
        memories: list[Any]
        if callable(get_memory):
            try:
                result = get_memory(prepend_summary=True)
            except TypeError:
                result = get_memory()
            if isinstance(result, asyncio.Future) or hasattr(result, "__await__"):
                load_task = asyncio.ensure_future(result)

                async def settle_load() -> None:
                    await asyncio.gather(load_task, return_exceptions=True)

                try:
                    try:
                        done, _ = await asyncio.wait(
                            [load_task],
                            timeout=_FOREGROUND_MEMORY_LOAD_TIMEOUT_SECONDS,
                        )
                        # A late cleanup result must not turn a timeout into
                        # an apparently successful, empty history load.
                        if not done:
                            raise asyncio.TimeoutError
                        result = load_task.result()
                    finally:
                        if not load_task.done():
                            load_task.cancel()
                        # Keep ownership through repeated caller cancellation.
                        await await_settled(asyncio.create_task(settle_load()))
                except asyncio.CancelledError:
                    raise _NativeMemoryLoadCancelled() from None
                except asyncio.TimeoutError:
                    raise _NativeMemoryLoadError(
                        session_id="",
                        message=(
                            "会话历史记录读取超时，本轮已停止，原始记录已保留。"
                            "请稍后重试。"
                        ),
                        details={"reason": "native_memory_load_timeout"},
                    ) from None
            memories = list(result or [])
        else:
            content = getattr(memory, "content", None)
            if not isinstance(content, list):
                return []
            memories = [
                item[0] if isinstance(item, tuple) and item else item
                for item in content
            ]

        runtime_messages: list[RuntimeMessage] = []
        for row_index, msg in enumerate(memories):
            metadata = getattr(msg, "metadata", None)
            action = (
                metadata.get("system_action")
                if isinstance(metadata, dict)
                else None
            )
            if (
                isinstance(action, dict)
                and action.get("kind") == "context_compaction"
                and action.get("automatic") is True
            ):
                continue
            try:
                runtime_messages.extend(
                    message
                    for message in legacy_msg_to_runtime_messages(msg)
                    if not AgentRunner._is_runtime_presentation_only(message)
                )
            except Exception:
                raise _NativeMemoryLoadError(
                    session_id="",
                    message=(
                        "会话历史记录无法完整读取，本轮已停止，原始记录已保留。"
                        "请检查会话记录后重试。"
                    ),
                    details={
                        "reason": "native_memory_conversion_failed",
                        "row_index": row_index,
                    },
                ) from None
        return AgentRunner._deduplicate_adjacent_user_mirrors(runtime_messages)

    @staticmethod
    def _deduplicate_adjacent_user_mirrors(
        messages: list[RuntimeMessage],
    ) -> list[RuntimeMessage]:
        """Keep one causal input when legacy and accepted rows mirror a submit."""

        def client_turn_id(message: RuntimeMessage) -> str:
            metadata = (
                message.metadata if isinstance(message.metadata, dict) else {}
            )
            nested = metadata.get("metadata")
            for container in (
                metadata,
                nested if isinstance(nested, dict) else {},
            ):
                value = container.get("client_turn_id")
                if isinstance(value, str) and value.strip():
                    return value.strip()
            return ""

        def input_signature(message: RuntimeMessage) -> str:
            if message.role != "user":
                return ""
            return json.dumps(
                [
                    block.model_dump(exclude={"metadata"}, mode="json")
                    for block in message.content
                ],
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            )

        def timestamp_seconds(message: RuntimeMessage) -> float | None:
            value = str(message.created_at or "").strip()
            if not value:
                return None
            normalized = (
                value.removesuffix("Z") + "+00:00"
                if value.endswith("Z")
                else value
            )
            try:
                return datetime.fromisoformat(normalized).timestamp()
            except ValueError:
                return None

        result: list[RuntimeMessage] = []
        seen_turn_ids: set[str] = set()
        for message in messages:
            if message.role != "user":
                result.append(message)
                continue

            turn_id = client_turn_id(message)
            if turn_id and turn_id in seen_turn_ids:
                continue

            previous = result[-1] if result else None
            if previous is not None and previous.role == "user":
                signature = input_signature(message)
                previous_signature = input_signature(previous)
                current_time = timestamp_seconds(message)
                previous_time = timestamp_seconds(previous)
                if (
                    signature
                    and signature == previous_signature
                    and current_time is not None
                    and previous_time is not None
                    and abs(current_time - previous_time) <= 2.0
                ):
                    if turn_id:
                        seen_turn_ids.add(turn_id)
                    continue

            if turn_id:
                seen_turn_ids.add(turn_id)
            result.append(message)
        return result

    @staticmethod
    async def _stream_native_context_preflight(
        agent: Any,
        current_messages: list[RuntimeMessage],
    ) -> AsyncGenerator[tuple[Any, bool], None]:
        """Stream shared compaction lifecycle rows before a Native turn."""

        context_manager = getattr(agent, "context_manager", None)
        pre_reasoning = getattr(context_manager, "pre_reasoning", None)
        if not callable(pre_reasoning):
            return

        completed = object()
        status_queue: asyncio.Queue[tuple[Any, bool] | object] = asyncio.Queue(
            maxsize=_CONTEXT_PREFLIGHT_STATUS_QUEUE_LIMIT,
        )
        closing = False

        async def collect_status(msg: Any, last: bool) -> None:
            # Cleanup may report status after the consumer has closed. There
            # is no recipient then; never block resource settlement on it.
            if closing:
                return
            await status_queue.put((msg, last))

        additional_messages = [
            runtime_message_to_legacy_msg(message)
            for message in current_messages
        ]

        async def run_preflight() -> None:
            try:
                await pre_reasoning(
                    agent,
                    {
                        "_additional_context_messages": additional_messages,
                        "_context_status_sink": collect_status,
                    },
                )
            finally:
                # A closed consumer cannot drain a full queue. Normal completion
                # still waits behind every status row; nothing is discarded.
                if not closing:
                    await status_queue.put(completed)

        preflight_task = asyncio.create_task(run_preflight())
        status_emitted = False
        try:
            while True:
                timeout = (
                    _FOREGROUND_CONTEXT_PREFLIGHT_ACTIVE_TIMEOUT_SECONDS
                    if status_emitted
                    else _FOREGROUND_CONTEXT_PREFLIGHT_SILENT_TIMEOUT_SECONDS
                )
                try:
                    status_row = await asyncio.wait_for(
                        status_queue.get(),
                        timeout=timeout,
                    )
                except asyncio.TimeoutError:
                    logger.warning(
                        "Native context preflight exceeded the %s foreground "
                        "budget; continuing without waiting",
                        "active" if status_emitted else "silent",
                    )
                    break
                if status_row is completed:
                    break
                status_emitted = True
                yield status_row
            if preflight_task.done():
                await preflight_task
        finally:
            closing = True
            if not preflight_task.done():
                preflight_task.cancel()

            async def settle_preflight() -> None:
                await asyncio.gather(preflight_task, return_exceptions=True)

            await await_settled(asyncio.create_task(settle_preflight()))

    @staticmethod
    async def _native_memory_recall_messages(
        agent: Any,
        current_messages: list[RuntimeMessage],
    ) -> list[RuntimeMessage]:
        """Run Native turns through the same per-turn recall seam as Legacy."""

        context_manager = getattr(agent, "context_manager", None)
        pre_reply = getattr(context_manager, "pre_reply", None)
        if not callable(pre_reply):
            return []
        legacy_current = [
            runtime_message_to_legacy_msg(message)
            for message in current_messages
        ]
        try:
            result = await asyncio.wait_for(
                pre_reply(agent, {"msg": legacy_current}),
                timeout=_FOREGROUND_MEMORY_RECALL_TIMEOUT_SECONDS,
            )
        except asyncio.TimeoutError:
            logger.warning(
                "Native memory recall exceeded the foreground budget; "
                "continuing without automatic recall",
            )
            return []
        except Exception:
            logger.warning("Native memory recall failed open", exc_info=True)
            return []
        if not isinstance(result, dict):
            return []
        projected = result.get("msg")
        if not isinstance(projected, list) or len(projected) <= len(legacy_current):
            return []
        recalled: list[RuntimeMessage] = []
        for message in projected[len(legacy_current) :]:
            recalled.extend(
                runtime_message
                for runtime_message in legacy_msg_to_runtime_messages(message)
                if not AgentRunner._is_runtime_presentation_only(
                    runtime_message
                )
            )
        return recalled

    @staticmethod
    async def _persist_runtime_message_to_agent_memory(
        agent: Any,
        runtime_message: RuntimeMessage,
    ) -> None:
        """Persist a Native Runtime message into the legacy session state."""

        memory = getattr(agent, "memory", None)
        add = getattr(memory, "add", None)
        if not callable(add):
            return
        legacy_msg = runtime_message_to_legacy_msg(runtime_message)
        result = add(legacy_msg)
        if isinstance(result, asyncio.Future) or hasattr(result, "__await__"):
            await result

    @classmethod
    async def _persist_post_answer_reflection_message(
        cls,
        agent: Any,
        runtime_message: RuntimeMessage,
    ) -> None:
        """Persist a late Ququ once without crossing the loaded session."""

        memory = getattr(agent, "memory", None)
        get_memory = getattr(memory, "get_memory", None)
        if callable(get_memory):
            try:
                existing = get_memory(prepend_summary=False)
                if isinstance(existing, asyncio.Future) or hasattr(
                    existing,
                    "__await__",
                ):
                    existing = await existing
                job_id = str(
                    (runtime_message.metadata or {}).get(
                        "post_answer_reflection_job_id"
                    )
                    or ""
                )
                for message in list(existing or []):
                    if str(getattr(message, "id", "") or "") == str(
                        runtime_message.id or ""
                    ):
                        return
                    metadata = getattr(message, "metadata", None)
                    if (
                        job_id
                        and isinstance(metadata, dict)
                        and str(
                            metadata.get("post_answer_reflection_job_id") or ""
                        )
                        == job_id
                    ):
                        return
            except Exception:
                logger.debug(
                    "Could not inspect memory before late Ququ persistence",
                    exc_info=True,
                )
        await cls._persist_runtime_message_to_agent_memory(
            agent,
            runtime_message,
        )

    async def _persist_native_runtime_control_event(
        self,
        *,
        event: RuntimeEvent,
        request_context: dict[str, Any],
        workspace_dir: Path,
        session_id: str,
        attempt_id: str | None = None,
    ) -> None:
        """Write Native tool/approval lifecycle to the frontend event ledger.

        Message rows are still projected from Console SSE so the primary
        response stream and reconnect stream share the same cumulative-text
        normalization. Turn start/terminal rows remain owned by the Console
        lifecycle. Persisting only control events here closes the gap where
        Native events were otherwise lost by the temporary legacy message
        bridge.
        """

        presentation_context = str(
            request_context.get("presentation_context") or ""
        ).strip().lower()
        is_subagent_internal = presentation_context == "subagent_internal"
        kind = _NATIVE_LEDGER_EVENT_KINDS.get(event.type)
        if kind is None and is_subagent_internal:
            kind = _SUBAGENT_NATIVE_LEDGER_EVENT_KINDS.get(event.type)
        if kind is None:
            return
        chat_id = str(request_context.get("chat_id") or "").strip()
        run_id = str(
            request_context.get("run_id") or event.turn_id or "",
        ).strip()
        if not chat_id or not run_id:
            return

        from ..experience.events import append_chat_event

        payload = dict(event.payload or {})
        normalized_attempt_id = str(attempt_id or "").strip()
        if normalized_attempt_id:
            # This event belongs to the receiving attempt, even when admission
            # rejects an earlier effect. That effect keeps its own canonical
            # attempt identity; it must not hide this event from current filters.
            payload["attempt_id"] = normalized_attempt_id
        if is_subagent_internal and event.type.startswith("message."):
            payload = self._safe_subagent_message_payload(event, payload)
        if is_subagent_internal and event.type in _RUNTIME_TURN_TERMINAL_TYPES:
            payload.setdefault(
                "final_task_status",
                {
                    "turn.completed": "completed",
                    "turn.interrupted": "interrupted",
                    "turn.failed": "failed",
                    "turn.stalled": "stalled",
                }.get(event.type, "failed"),
            )
        details = (
            dict(payload.get("details") or {})
            if isinstance(payload.get("details"), dict)
            else {}
        )
        details.setdefault("native_runtime_event_id", event.id)
        details.setdefault("native_runtime_event_seq", event.seq)
        payload["details"] = details
        if event.presentation is not None:
            payload.setdefault(
                "presentation",
                event.presentation.model_dump(mode="json"),
            )
        status = str(event.status or "info").strip().lower()
        if status not in _CHAT_LEDGER_STATUSES:
            status = "info"
        await append_chat_event(
            workspace_dir=workspace_dir,
            agent_id=self.agent_id,
            chat_id=chat_id,
            session_id=session_id,
            run_id=run_id,
            item_id=event.item_id,
            kind=kind,
            type=event.type,
            status=status,
            source=event.source,
            summary=(
                SUBAGENT_INTERNAL_ACTIVITY_STATUS
                if is_subagent_internal
                and event.type.startswith("message.")
                and self._is_reasoning_message_payload(payload)
                else event.summary or event.type
            ),
            payload=payload,
            usage=event.usage,
        )

    @staticmethod
    def _is_reasoning_message_payload(payload: dict[str, Any]) -> bool:
        if str(payload.get("content_type") or "").lower() in {
            "thinking",
            "reasoning",
        }:
            return True
        if str(payload.get("stream_message_type") or "").lower() == "reasoning":
            return True
        message = payload.get("message")
        if not isinstance(message, dict):
            return False
        content = message.get("content")
        return bool(
            isinstance(content, list)
            and content
            and all(
                isinstance(block, dict)
                and str(block.get("type") or "").lower()
                in {"thinking", "reasoning"}
                for block in content
            )
        )

    @classmethod
    def _safe_subagent_message_payload(
        cls,
        event: RuntimeEvent,
        payload: dict[str, Any],
    ) -> dict[str, Any]:
        """Keep child transcript useful without persisting private reasoning."""

        safe = dict(payload)
        reasoning = cls._is_reasoning_message_payload(safe)
        message = safe.get("message")
        if isinstance(message, dict):
            safe_message = dict(message)
            content = safe_message.get("content")
            if isinstance(content, list):
                public_blocks = [
                    dict(block)
                    for block in content
                    if isinstance(block, dict)
                    and str(block.get("type") or "").lower()
                    not in {"thinking", "reasoning"}
                ]
                if reasoning and not public_blocks:
                    public_blocks = [
                        {
                            "type": "text",
                            "text": SUBAGENT_INTERNAL_ACTIVITY_STATUS,
                        }
                    ]
                safe_message["content"] = public_blocks
            safe["message"] = safe_message
        if reasoning:
            if "delta" in safe:
                safe["delta"] = SUBAGENT_INTERNAL_ACTIVITY_STATUS
            if "text" in safe:
                safe["text"] = SUBAGENT_INTERNAL_ACTIVITY_STATUS
            safe["content_type"] = "thinking"
            safe["stream_message_type"] = "reasoning"
        return safe

    async def _close_native_runtime_control_events(
        self,
        *,
        terminal_event: RuntimeEvent,
        open_tools: dict[str, RuntimeEvent],
        open_approvals: dict[str, RuntimeEvent],
        request_context: dict[str, Any],
        workspace_dir: Path,
        session_id: str,
        attempt_id: str | None = None,
    ) -> None:
        """Close pending control rows before the lifecycle writes a terminal."""

        interrupted = terminal_event.type == "turn.interrupted"
        status = "interrupted" if interrupted else "failed"
        decision = "cancelled" if interrupted else "runtime_failed"
        reason = (
            "Runtime interrupted before approval or tool completion."
            if interrupted
            else "Runtime stopped before approval or tool completion."
        )

        for request_id, requested in list(open_approvals.items()):
            payload = dict(requested.payload or {})
            payload["request_id"] = request_id
            payload["decision"] = decision
            resolved = RuntimeEvent(
                id=f"runtime-close-{uuid4().hex}",
                seq=terminal_event.seq,
                type="approval.resolved",
                status=status,
                source="system",
                session_id=terminal_event.session_id or session_id,
                turn_id=terminal_event.turn_id,
                item_id=request_id,
                summary=reason,
                payload=payload,
                presentation=requested.presentation,
            )
            await self._persist_native_runtime_control_event(
                event=resolved,
                request_context=request_context,
                workspace_dir=workspace_dir,
                session_id=session_id,
                attempt_id=attempt_id,
            )
            open_approvals.pop(request_id, None)

        for call_id, started in list(open_tools.items()):
            payload = dict(started.payload or {})
            collaboration = payload.get("collaboration")
            if isinstance(collaboration, dict):
                collaboration = dict(collaboration)
                members = []
                terminal_status = "cancelled" if interrupted else "failed"
                terminal_error_code = (
                    "cancelled" if interrupted else "runtime_failed"
                )
                for raw_member in collaboration.get("members") or []:
                    if not isinstance(raw_member, dict):
                        continue
                    member = dict(raw_member)
                    if str(member.get("status") or "") in {
                        "queued",
                        "running",
                        "retrying",
                    }:
                        member["status"] = terminal_status
                        member["error_code"] = terminal_error_code
                    members.append(member)
                collaboration = close_public_room_snapshot(
                    source=collaboration,
                    batch_id=str(
                        collaboration.get("batch_id") or call_id,
                    ),
                    members=members,
                )
                payload["collaboration"] = collaboration
            payload.update(
                {
                    "id": call_id,
                    "ok": False,
                    "error": reason,
                    "output": [{"type": "text", "text": reason}],
                },
            )
            completed = RuntimeEvent(
                id=f"runtime-close-{uuid4().hex}",
                seq=terminal_event.seq,
                type="tool.call.completed",
                status=status,
                source="tool",
                session_id=terminal_event.session_id or session_id,
                turn_id=terminal_event.turn_id,
                item_id=call_id,
                summary=reason,
                payload=payload,
                presentation=started.presentation,
            )
            await self._persist_native_runtime_control_event(
                event=completed,
                request_context=request_context,
                workspace_dir=workspace_dir,
                session_id=session_id,
                attempt_id=attempt_id,
            )
            open_tools.pop(call_id, None)

    @staticmethod
    def _runtime_engine_for_agent(agent_config: Any) -> str:
        runtime_cfg = getattr(agent_config, "runtime", None)
        engine = str(getattr(runtime_cfg, "engine", "native") or "native")
        if engine != "native":
            logger.warning(
                "Ignoring retired agent runtime engine %r; product wiring is Native-only.",
                engine,
            )
        return "native"

    def _build_native_tool_registry(
        self,
        *,
        agent_config: Any,
        request_context: dict[str, Any],
        mcp_clients: list[Any],
        current_user_text: str = "",
        approval_service: Any | None = None,
        plan_runtime_module: Any | None = None,
        runtime_attempt_id: str | None = None,
    ) -> LantaiToolRegistry:
        registry = build_lantai_tool_registry(
            agent_config=agent_config,
            request_context=request_context,
            approval_service=approval_service,
        )
        registry.extend(build_memory_tool_registry(self.memory_manager))
        registry.extend(
            build_mcp_client_registry(
                mcp_clients,
                request_context=request_context,
            ),
        )
        if plan_runtime_module is not None:
            registry.extend(plan_runtime_module.tool_registry())
        state_workspace_dir = Path(
            request_context.get("state_workspace_dir")
            or request_context.get("workspace_dir")
            or WORKING_DIR
        )
        effective_workspace_dir = Path(
            request_context.get("workspace_dir") or state_workspace_dir
        )
        works_workspace_dir = Path(
            request_context.get("works_workspace_dir") or state_workspace_dir
        )
        run_id = str(request_context.get("run_id") or "").strip()
        chat_id = str(request_context.get("chat_id") or "").strip()
        private_record_revision = request_context.get(
            "private_editor_record_revision"
        )
        private_revocation_epoch = request_context.get(
            "private_editor_revocation_epoch"
        )
        private_editor_bound = (
            request_context.get("_trusted_private_editor_binding") is True
            and request_context.get("work_method_id") == "private_editor"
            and request_context.get("private_editor_project_status") == "active"
            and bool(run_id)
            and bool(chat_id)
            and bool(str(request_context.get("private_editor_project_id") or "").strip())
            and bool(
                str(request_context.get("private_editor_current_work_id") or "").strip()
            )
            and bool(
                str(request_context.get("private_editor_current_digest") or "").strip()
            )
            and isinstance(private_record_revision, int)
            and not isinstance(private_record_revision, bool)
            and isinstance(private_revocation_epoch, int)
            and not isinstance(private_revocation_epoch, bool)
        )
        private_store = None
        private_snapshot = None
        if private_editor_bound:
            from ..private_editor import PrivateEditorError, PrivateEditorStore

            private_project_id = str(
                request_context["private_editor_project_id"]
            ).strip()
            private_current_work_id = str(
                request_context["private_editor_current_work_id"]
            ).strip()
            private_current_digest = str(
                request_context["private_editor_current_digest"]
            ).strip()
            private_store = PrivateEditorStore(works_workspace_dir)
            try:
                private_snapshot = private_store.get_project(private_project_id)
            except (OSError, ValueError, PrivateEditorError):
                logger.info(
                    "Private-editor tools withheld for a stale project binding",
                    extra={"private_editor_project_id": private_project_id},
                )
                private_editor_bound = False
            else:
                private_editor_bound = bool(
                    private_snapshot.status == "active"
                    and private_snapshot.chat_id == chat_id
                    and private_snapshot.current_work_id == private_current_work_id
                    and private_snapshot.current_digest == private_current_digest
                    and private_snapshot.record_revision == private_record_revision
                    and private_snapshot.revocation_epoch == private_revocation_epoch
                )
        if (
            private_editor_bound
            and private_store is not None
            and private_snapshot is not None
        ):
            from ..private_editor import (
                AdoptionRequest,
                AngleProposalDraft,
                ChangeGroup,
                PrivateEditorAnchorError,
                RecordEntryDraft,
                _uncommitted_private_editor_validation,
            )
            from ..works import WorksIndexUnavailable, get_managed_work_file
            from ...security.file_access_grants import authorize_external_file_path
            from ...runtime.tool_response import TextBlock, ToolResponse

            private_mutation_lock = asyncio.Lock()

            def _private_editor_anchor_recovery() -> dict[str, str] | None:
                """Point to the current manuscript only under the existing read grant."""

                def binding_matches(project):
                    return (
                        project.status == "active"
                        and project.chat_id == chat_id
                        and project.original_work_id == private_snapshot.original_work_id
                        and project.current_work_id == private_current_work_id
                        and project.current_digest == private_current_digest
                        and project.record_revision == private_record_revision
                        and project.revocation_epoch == private_revocation_epoch
                    )

                try:
                    if not private_store.get_settings().enabled:
                        return None
                    project = private_store.get_project(private_project_id)
                    if not binding_matches(project):
                        return None
                    managed = get_managed_work_file(works_workspace_dir, private_current_work_id)
                    if managed is None or managed[0].source_digest != private_current_digest:
                        return None
                    path = authorize_external_file_path(managed[1], operation="read")
                    if (
                        not private_store.get_settings().enabled
                        or not binding_matches(private_store.get_project(private_project_id))
                    ):
                        return None
                    return {
                        "current_work_id": private_current_work_id,
                        "current_digest": private_current_digest,
                        "read_only_path": str(path),
                        "read_tool": "read_file",
                    }
                except (OSError, ValueError, PrivateEditorError, WorksIndexUnavailable):
                    return None

            def _settle_private_editor_validation(executor):
                @wraps(executor)
                async def execute(*args, **kwargs):
                    try:
                        return await executor(*args, **kwargs)
                    except (ValueError, PrivateEditorError) as exc:
                        if not getattr(exc, "_private_editor_known_not_applied", False):
                            raise
                        text = str(exc)
                        metadata = {
                            "error_type": type(exc).__name__,
                            "error_code": "validation_failed",
                            "side_effect_outcome": "known_not_applied",
                        }
                        if isinstance(exc, PrivateEditorAnchorError):
                            recovery = await asyncio.to_thread(_private_editor_anchor_recovery)
                            if recovery is not None:
                                metadata["current_work_recovery"] = recovery
                                text += (
                                    "\n本次候选未保存。请先用 read_file 重新读取以下已授权的当前稿，"
                                    "依据真实正文重新判断修改组；不得用旧副本或旧锚点直接重试：\n"
                                    + json.dumps(recovery, ensure_ascii=False)
                                )
                        return ToolResponse(content=[TextBlock(text=text)], is_error=True, metadata=metadata)

                return execute

            def _private_editor_record_drafts(
                author_statements: list[dict[str, Any]],
                model_proposals: list[dict[str, Any]],
            ) -> list[RecordEntryDraft]:
                drafts: list[RecordEntryDraft] = []
                for raw in author_statements:
                    text = str(raw.get("text") or "").strip()
                    exact_words = str(raw.get("exact_user_words") or "").strip()
                    if (
                        not exact_words
                        or exact_words not in current_user_text
                        or text != exact_words
                    ):
                        raise ValueError(
                            "作者原话必须逐字来自当前用户消息；概括只能作为待确认的模型理解"
                        )
                    kind = str(raw.get("kind") or "").strip()
                    drafts.append(
                        RecordEntryDraft(
                            kind=kind,
                            text=text,
                            source="user_explicit",
                            source_ref=f"user-message:{run_id}",
                            status="open" if kind == "open_question" else "accepted",
                            supersedes=list(raw.get("supersedes") or []),
                        )
                    )
                for raw in model_proposals:
                    drafts.append(
                        RecordEntryDraft(
                            kind=str(raw.get("kind") or "").strip(),
                            text=str(raw.get("text") or "").strip(),
                            source="model_inferred",
                            source_ref=f"assistant-run:{run_id}",
                            status="proposed",
                            supersedes=list(raw.get("supersedes") or []),
                        )
                    )
                return drafts

            async def record_private_editor_context_tool(
                author_statements: list[dict[str, Any]],
                model_proposals: list[dict[str, Any]],
            ) -> dict[str, Any]:
                """Record project-scoped context without changing the manuscript.

                Put only exact substrings of this turn's user message in
                author_statements. Put every paraphrase or inference in
                model_proposals so it remains visibly pending author review.
                Use this on a useful zero-edit turn; zero important changes is
                valid and must not be replaced with a cosmetic candidate.
                """

                nonlocal private_record_revision

                with _uncommitted_private_editor_validation():
                    drafts = _private_editor_record_drafts(
                        author_statements,
                        model_proposals,
                    )
                async with private_mutation_lock:
                    entries = await asyncio.to_thread(
                        private_store.add_record_entries,
                        private_project_id,
                        expected_current_work_id=private_current_work_id,
                        expected_current_digest=private_current_digest,
                        expected_record_revision=private_record_revision,
                        expected_revocation_epoch=private_revocation_epoch,
                        drafts=drafts,
                    )
                    private_record_revision += 1
                    return {
                        "records": [entry.model_dump(mode="json") for entry in entries],
                        "record_revision": private_record_revision,
                        "current_work_id": private_current_work_id,
                        "current_digest": private_current_digest,
                        "current_draft_changed": False,
                    }

            async def stage_private_editor_candidate_tool(
                change_groups: list[dict[str, Any]],
                author_statements: list[dict[str, Any]],
                model_proposals: list[dict[str, Any]],
            ) -> dict[str, Any]:
                """Stage grounded edit groups without modifying the current draft.

                Each group must name the exact current-draft text it replaces,
                a target anchor, observation, author-facing impact, rationale,
                evidence, and trade-offs. Keep cross-paragraph dependencies in
                one group or reference another group's group_key. Never use this
                tool merely to force a change: zero important edits is valid.
                Read the current draft first. Copy before verbatim, without
                reader-added line numbers; it must occur exactly once in the
                unchanged current draft. Groups cannot overlap: combine all
                edits to the same passage into one group. For each
                author statement, text and exact_user_words must be identical
                verbatim substrings of this turn's user message, punctuation
                included. Put every paraphrase in model_proposals instead; an
                empty list is valid when no record needs to be saved. Describe
                objective size changes from the returned candidate_receipt;
                character counts alone do not prove clarity or quality.
                """

                nonlocal private_record_revision

                with _uncommitted_private_editor_validation():
                    if not change_groups:
                        raise ValueError("at least one grounded change group is required")
                    group_keys: list[str] = []
                    for index, raw in enumerate(change_groups):
                        group_key = str(raw.get("group_key") or f"group-{index + 1}").strip()
                        if not group_key or group_key in group_keys:
                            raise ValueError("change group keys must be non-empty and unique")
                        group_keys.append(group_key)
                    group_ids = {key: str(uuid4()) for key in group_keys}
                    groups: list[ChangeGroup] = []
                    for raw, group_key in zip(change_groups, group_keys, strict=True):
                        dependency_keys = [
                            str(item).strip() for item in raw.get("depends_on", [])
                        ]
                        if any(key not in group_ids for key in dependency_keys):
                            raise ValueError("change group dependency does not exist")
                        groups.append(
                            ChangeGroup(
                                id=group_ids[group_key],
                                target_anchor=str(raw.get("target_anchor") or "").strip(),
                                before=str(raw.get("before") or ""),
                                after=str(raw.get("after") or ""),
                                observation=str(raw.get("observation") or "").strip(),
                                impact=str(raw.get("impact") or "").strip(),
                                rationale=str(raw.get("rationale") or "").strip(),
                                evidence_refs=list(raw.get("evidence_refs") or []),
                                costs=list(raw.get("costs") or []),
                                depends_on=[group_ids[key] for key in dependency_keys],
                            )
                        )
                    drafts = _private_editor_record_drafts(
                        author_statements,
                        model_proposals,
                    )
                async with private_mutation_lock:
                    candidate = await asyncio.to_thread(
                        private_store.stage_candidate,
                        private_project_id,
                        expected_base_work_id=private_current_work_id,
                        expected_base_digest=private_current_digest,
                        expected_record_revision=private_record_revision,
                        expected_revocation_epoch=private_revocation_epoch,
                        change_groups=groups,
                        record_drafts=drafts,
                    )
                    private_record_revision = candidate.record_revision
                    candidate_receipt = {
                        "candidate_id": candidate.id,
                        "base_work_id": candidate.base_work_id,
                        "base_digest": candidate.base_digest,
                        "current_draft_changed": False,
                        "change_groups": [
                            {
                                "target_anchor": group.target_anchor,
                                "before": group.before,
                                "after": group.after,
                                "before_character_count": len(group.before),
                                "after_character_count": len(group.after),
                                "character_delta": (
                                    len(group.after) - len(group.before)
                                ),
                                "before_non_whitespace_character_count": sum(
                                    not character.isspace()
                                    for character in group.before
                                ),
                                "after_non_whitespace_character_count": sum(
                                    not character.isspace()
                                    for character in group.after
                                ),
                                "non_whitespace_character_delta": (
                                    sum(
                                        not character.isspace()
                                        for character in group.after
                                    )
                                    - sum(
                                        not character.isspace()
                                        for character in group.before
                                    )
                                ),
                                "changed": group.after != group.before,
                            }
                            for group in candidate.change_groups
                        ],
                    }
                    return {
                        "candidate": candidate.model_dump(mode="json"),
                        "candidate_receipt": candidate_receipt,
                        "record_revision": private_record_revision,
                        "current_work_id": private_current_work_id,
                        "current_digest": private_current_digest,
                        "current_draft_changed": False,
                    }

            async def record_private_editor_angle_result_tool(
                proposals: list[dict[str, Any]],
                empty_reason: str,
            ) -> dict[str, Any]:
                """Record one manual, source-grounded new-angle search result.

                Use only after the author explicitly requests a new angle for
                this project. Cite exact source refs visible in the bound
                project context. Do not browse, search another project, create
                a scheduled task, or invent a direction merely to avoid an
                empty result. If current material has no new value, return an
                empty proposals list and a concise honest empty_reason.
                """

                nonlocal private_record_revision

                with _uncommitted_private_editor_validation():
                    normalized_empty_reason = empty_reason.strip()
                    if not proposals:
                        if not normalized_empty_reason:
                            raise ValueError(
                                "an empty angle result requires an honest empty_reason"
                            )
                        return {
                            "proposals": [],
                            "empty_reason": normalized_empty_reason,
                            "record_revision": private_record_revision,
                            "current_work_id": private_current_work_id,
                            "current_digest": private_current_digest,
                            "current_draft_changed": False,
                        }
                    if normalized_empty_reason:
                        raise ValueError(
                            "empty_reason must be blank when proposals are returned"
                        )
                    if len(proposals) > 3:
                        raise ValueError("return at most three grounded proposals")
                    drafts = [
                        AngleProposalDraft.model_validate(proposal)
                        for proposal in proposals
                    ]
                async with private_mutation_lock:
                    recorded = await asyncio.to_thread(
                        private_store.add_angle_proposals,
                        private_project_id,
                        expected_current_work_id=private_current_work_id,
                        expected_current_digest=private_current_digest,
                        expected_record_revision=private_record_revision,
                        expected_revocation_epoch=private_revocation_epoch,
                        generated_run_id=run_id,
                        drafts=drafts,
                    )
                    private_record_revision += 1
                    return {
                        "proposals": [
                            proposal.model_dump(mode="json") for proposal in recorded
                        ],
                        "empty_reason": "",
                        "record_revision": private_record_revision,
                        "current_work_id": private_current_work_id,
                        "current_digest": private_current_digest,
                        "current_draft_changed": False,
                    }

            eligible_candidates = [
                candidate
                for candidate in private_snapshot.candidates
                if candidate.status == "staged"
                and candidate.base_work_id == private_current_work_id
                and candidate.base_digest == private_current_digest
                and candidate.record_revision == private_record_revision
                and candidate.revocation_epoch == private_revocation_epoch
            ]
            eligible_candidate_ids = [
                candidate.id for candidate in eligible_candidates
            ]
            eligible_group_ids = [
                group.id
                for candidate in eligible_candidates
                for group in candidate.change_groups
            ]

            async def adopt_private_editor_candidate_tool(
                candidate_id: str,
                selected_change_groups: list[str],
            ) -> dict[str, Any]:
                """Adopt selected groups only after clear author authorization.

                Call this only when the current user message unambiguously asks
                to adopt this displayed candidate or these exact groups.  Do not
                infer authorization from silence, continued conversation,
                download, or a model-authored confirmation.  Ask a clarifying
                question instead when the requested candidate or scope is
                ambiguous.  The server supplies identity, baseline, operation
                id, and authorization source; there is no model-controlled
                confirmed flag.
                """

                candidate = next(
                    (
                        item
                        for item in eligible_candidates
                        if item.id == candidate_id
                    ),
                    None,
                )
                if candidate is None:
                    raise ValueError("candidate is not eligible for this frozen turn")
                selected = [str(item).strip() for item in selected_change_groups]
                if not selected or any(not item for item in selected):
                    raise ValueError("at least one change group must be selected")
                candidate_group_ids = {group.id for group in candidate.change_groups}
                if any(item not in candidate_group_ids for item in selected):
                    raise ValueError("selected group does not belong to candidate")
                operation_fingerprint = hashlib.sha256(
                    json.dumps(
                        {
                            "candidate_id": candidate.id,
                            "run_id": run_id,
                            "selected_change_groups": selected,
                        },
                        ensure_ascii=False,
                        sort_keys=True,
                        separators=(",", ":"),
                    ).encode("utf-8")
                ).hexdigest()[:32]
                receipt = await asyncio.to_thread(
                    private_store.adopt_candidate,
                    private_project_id,
                    AdoptionRequest(
                        operation_id=f"runtime-adopt:{operation_fingerprint}",
                        candidate_id=candidate.id,
                        expected_base_work_id=candidate.base_work_id,
                        expected_base_digest=candidate.base_digest,
                        expected_record_revision=candidate.record_revision,
                        revocation_epoch=candidate.revocation_epoch,
                        selected_change_groups=selected,
                    ),
                    authorization_source=f"user-message:{run_id}",
                )
                return receipt.model_dump(mode="json")

            record_kind_schema = {
                "type": "string",
                "enum": [
                    "author_intent",
                    "decision",
                    "open_question",
                    "protected_quote",
                    "material_note",
                ],
            }
            supersedes_schema = {
                "type": "array",
                "items": {"type": "string"},
                "maxItems": 20,
            }
            author_statement_schema = {
                "type": "object",
                "properties": {
                    "kind": record_kind_schema,
                    "text": {
                        "type": "string",
                        "minLength": 1,
                        "description": "Copy an exact substring of the current user message, including punctuation. Must equal exact_user_words; never paraphrase.",
                    },
                    "exact_user_words": {
                        "type": "string",
                        "minLength": 1,
                        "description": "The identical verbatim substring used in text, from this turn only.",
                    },
                    "supersedes": supersedes_schema,
                },
                "required": ["kind", "text", "exact_user_words", "supersedes"],
                "additionalProperties": False,
            }
            model_proposal_schema = {
                "type": "object",
                "properties": {
                    "kind": record_kind_schema,
                    "text": {"type": "string", "minLength": 1},
                    "supersedes": supersedes_schema,
                },
                "required": ["kind", "text", "supersedes"],
                "additionalProperties": False,
            }
            record_list_properties = {
                "author_statements": {
                    "type": "array",
                    "description": "Verbatim statements from this turn's user message only; leave empty if none. Paraphrases belong in model_proposals.",
                    "items": author_statement_schema,
                    "maxItems": 20,
                },
                "model_proposals": {
                    "type": "array",
                    "description": "Paraphrases, inferences, and suggestions pending author review. Never supersede a confirmed author record.",
                    "items": model_proposal_schema,
                    "maxItems": 20,
                },
            }
            private_effect_recovery = RuntimeEffectRecoveryCapability(
                external_side_effect=True,
                idempotency="not_idempotent",
                probe="tool_specific",
                compensation="not_required",
                on_ambiguity="probe_required",
                notes=(
                    "Project state is atomically committed under a server lock, but "
                    "a lost tool response must be probed before retrying to avoid a "
                    "duplicate candidate or record revision."
                ),
            )
            private_metadata = {
                "runtime_owned": True,
                "private_editor_project_scoped": True,
            }
            registry.register_tool(
                ToolSpec(
                    name="record_private_editor_context",
                    description=(record_private_editor_context_tool.__doc__ or "").strip(),
                    input_schema={
                        "type": "object",
                        "properties": record_list_properties,
                        "required": ["author_statements", "model_proposals"],
                        "additionalProperties": False,
                    },
                    origin="builtin",
                    read_only=False,
                    concurrency_safe=False,
                    destructive=False,
                    effect_recovery=private_effect_recovery,
                    metadata={**private_metadata, "trusted_effect_settlement": True},
                ),
                _settle_private_editor_validation(record_private_editor_context_tool),
            )
            registry.register_tool(
                ToolSpec(
                    name="record_private_editor_angle_result",
                    description=(
                        record_private_editor_angle_result_tool.__doc__ or ""
                    ).strip(),
                    input_schema={
                        "type": "object",
                        "properties": {
                            "proposals": {
                                "type": "array",
                                "maxItems": 3,
                                "items": {
                                    "type": "object",
                                    "properties": {
                                        "source_refs": {
                                            "type": "array",
                                            "minItems": 1,
                                            "maxItems": 20,
                                            "uniqueItems": True,
                                            "items": {
                                                "type": "string",
                                                "minLength": 1,
                                            },
                                        },
                                        "key_idea": {
                                            "type": "string",
                                            "minLength": 1,
                                        },
                                        "why_now": {
                                            "type": "string",
                                            "minLength": 1,
                                        },
                                        "differs_from": {
                                            "type": "string",
                                            "minLength": 1,
                                        },
                                        "next_action": {
                                            "type": "string",
                                            "minLength": 1,
                                        },
                                        "open_questions": {
                                            "type": "array",
                                            "maxItems": 20,
                                            "items": {"type": "string"},
                                        },
                                    },
                                    "required": [
                                        "source_refs",
                                        "key_idea",
                                        "why_now",
                                        "differs_from",
                                        "next_action",
                                        "open_questions",
                                    ],
                                    "additionalProperties": False,
                                },
                            },
                            "empty_reason": {"type": "string"},
                        },
                        "required": ["proposals", "empty_reason"],
                        "additionalProperties": False,
                    },
                    origin="builtin",
                    read_only=False,
                    concurrency_safe=False,
                    destructive=False,
                    effect_recovery=private_effect_recovery,
                    metadata={
                        **private_metadata,
                        "trusted_effect_settlement": True,
                        "manual_request_only": True,
                        "network_allowed": False,
                        "cross_project_allowed": False,
                    },
                ),
                _settle_private_editor_validation(record_private_editor_angle_result_tool),
            )
            registry.register_tool(
                ToolSpec(
                    name="stage_private_editor_candidate",
                    description=(stage_private_editor_candidate_tool.__doc__ or "").strip(),
                    input_schema={
                        "type": "object",
                        "properties": {
                            "change_groups": {
                                "type": "array",
                                "minItems": 1,
                                "maxItems": 20,
                                "items": {
                                    "type": "object",
                                    "properties": {
                                        "group_key": {"type": "string"},
                                        "target_anchor": {"type": "string", "minLength": 1},
                                        "before": {"type": "string", "minLength": 1},
                                        "after": {"type": "string"},
                                        "observation": {"type": "string", "minLength": 1},
                                        "impact": {"type": "string", "minLength": 1},
                                        "rationale": {"type": "string", "minLength": 1},
                                        "evidence_refs": {
                                            "type": "array",
                                            "items": {"type": "string"},
                                            "maxItems": 20,
                                        },
                                        "costs": {
                                            "type": "array",
                                            "items": {"type": "string"},
                                            "maxItems": 20,
                                        },
                                        "depends_on": {
                                            "type": "array",
                                            "items": {"type": "string"},
                                            "maxItems": 20,
                                        },
                                    },
                                    "required": [
                                        "target_anchor",
                                        "before",
                                        "after",
                                        "observation",
                                        "impact",
                                        "rationale",
                                        "evidence_refs",
                                        "costs",
                                        "depends_on",
                                    ],
                                    "additionalProperties": False,
                                },
                            },
                            **record_list_properties,
                        },
                        "required": [
                            "change_groups",
                            "author_statements",
                            "model_proposals",
                        ],
                        "additionalProperties": False,
                    },
                    origin="builtin",
                    read_only=False,
                    concurrency_safe=False,
                    destructive=False,
                    effect_recovery=private_effect_recovery,
                    metadata={**private_metadata, "trusted_effect_settlement": True},
                ),
                _settle_private_editor_validation(stage_private_editor_candidate_tool),
            )
            if eligible_candidate_ids:
                registry.register_tool(
                    ToolSpec(
                        name="adopt_private_editor_candidate",
                        description=(
                            adopt_private_editor_candidate_tool.__doc__ or ""
                        ).strip(),
                        input_schema={
                            "type": "object",
                            "properties": {
                                "candidate_id": {
                                    "type": "string",
                                    "enum": eligible_candidate_ids,
                                },
                                "selected_change_groups": {
                                    "type": "array",
                                    "minItems": 1,
                                    "maxItems": len(eligible_group_ids),
                                    "uniqueItems": True,
                                    "items": {
                                        "type": "string",
                                        "enum": eligible_group_ids,
                                    },
                                },
                            },
                            "required": [
                                "candidate_id",
                                "selected_change_groups",
                            ],
                            "additionalProperties": False,
                        },
                        origin="builtin",
                        read_only=False,
                        concurrency_safe=False,
                        destructive=True,
                        effect_recovery=RuntimeEffectRecoveryCapability(
                            external_side_effect=True,
                            idempotency="idempotency_key",
                            probe="not_required",
                            compensation="not_required",
                            on_ambiguity="safe_to_retry",
                            notes=(
                                "The server derives a stable operation id from the "
                                "frozen run and selected groups; replay returns the "
                                "same adoption receipt."
                            ),
                        ),
                        metadata={
                            **private_metadata,
                            "requires_explicit_author_authorization": True,
                            "authorization_source": "current_user_message",
                        },
                    ),
                    adopt_private_editor_candidate_tool,
                )
        if run_id and chat_id:
            pending_deliverable_context = request_context.get(
                "pending_deliverable_context"
            )
            pending_source_run_id = ""
            pending_source_contract_revision = 0
            pending_source_task_revision = 0
            pending_job_id: str | None = None
            pending_job_owner_run_id: str | None = None
            pending_job_conversation_id: str | None = None
            pending_inherited_contract = None
            if isinstance(pending_deliverable_context, dict):
                pending_source_run_id = str(
                    pending_deliverable_context.get("previous_run_id") or ""
                ).strip()
                try:
                    pending_source_contract_revision = max(
                        0,
                        int(
                            pending_deliverable_context.get(
                                "source_contract_revision"
                            )
                            or 0
                        ),
                    )
                    pending_source_task_revision = max(
                        0,
                        int(
                            pending_deliverable_context.get(
                                "source_task_revision"
                            )
                            or 0
                        ),
                    )
                except (TypeError, ValueError):
                    pending_source_contract_revision = 0
                    pending_source_task_revision = 0
                pending_job_id = (
                    str(
                        pending_deliverable_context.get("pending_job_id") or ""
                    ).strip()
                    or None
                )
                pending_job_owner_run_id = (
                    str(
                        pending_deliverable_context.get(
                            "pending_job_owner_run_id"
                        )
                        or ""
                    ).strip()
                    or (pending_source_run_id if pending_job_id else None)
                )
                pending_job_conversation_id = (
                    str(
                        pending_deliverable_context.get(
                            "pending_job_conversation_id"
                        )
                        or ""
                    ).strip()
                    or None
                )
                raw_inherited_contract = pending_deliverable_context.get(
                    "deliverable_contract"
                )
                if pending_source_run_id and isinstance(
                    raw_inherited_contract, dict
                ):
                    from ...runtime.runs import RuntimeDeliverableContract

                    try:
                        pending_inherited_contract = (
                            RuntimeDeliverableContract.model_validate(
                                raw_inherited_contract
                            )
                        )
                    except (TypeError, ValueError):
                        pending_inherited_contract = None

            if pending_source_run_id and pending_inherited_contract is not None:

                async def resolve_pending_deliverable_choice_tool(
                    source_run_id: str,
                    decision: str,
                ) -> dict[str, Any]:
                    """Resolve one prior unfinished deliverable from context.

                    Call this before answering whenever it is exposed. Decide
                    from the complete conversation and current user meaning,
                    never from an isolated number or keyword. Use ``continue``
                    only when the user chose to resume the original task;
                    ``replace`` when the current request supersedes it; and
                    ``cancel`` when the user abandons it. The source ID must
                    match the pending choice supplied by the Runtime.
                    Replacing or cancelling also stops the exact active PPT
                    stage bound to that unfinished task, including when the
                    same durable job still belongs to an ancestor Run.
                    """

                    normalized_source = str(source_run_id or "").strip()
                    if normalized_source != pending_source_run_id:
                        raise ValueError(
                            "source_run_id does not match the pending task."
                        )
                    from ...runtime.deliverable_contract import (
                        resolve_pending_deliverable,
                    )

                    resolution = await resolve_pending_deliverable(
                        state_workspace_dir=state_workspace_dir,
                        run_id=run_id,
                        source_run_id=pending_source_run_id,
                        source_contract_revision=(
                            pending_source_contract_revision
                        ),
                        source_task_revision=pending_source_task_revision,
                        pending_job_id=pending_job_id,
                        decision=decision,
                        inherited_contract=pending_inherited_contract,
                    )
                    cancelled_pending_job_id = None
                    if (
                        resolution.decision in {"replace", "cancel"}
                        and resolution.pending_job_id is not None
                    ):
                        from ...presentation.jobs import (
                            cancel_active_job_for_runtime_run,
                        )

                        cancelled = cancel_active_job_for_runtime_run(
                            state_workspace_dir,
                            pending_job_owner_run_id
                            or pending_source_run_id,
                            resolution.pending_job_id,
                            conversation_id=pending_job_conversation_id,
                        )
                        if cancelled:
                            cancelled_pending_job_id = resolution.pending_job_id
                    return {
                        **resolution.model_dump(mode="json"),
                        "cancelled_pending_job_id": cancelled_pending_job_id,
                        "pending_job_owner_run_id": (
                            pending_job_owner_run_id
                        ),
                        "pending_job_conversation_id": (
                            pending_job_conversation_id
                        ),
                        "inherited_deliverable_contract": (
                            pending_inherited_contract.model_dump(mode="json")
                            if resolution.decision == "continue"
                            else None
                        ),
                    }

                registry.register_tool(
                    ToolSpec(
                        name="resolve_pending_deliverable_choice",
                        description=(
                            resolve_pending_deliverable_choice_tool.__doc__ or ""
                        ).strip(),
                        input_schema={
                            "type": "object",
                            "properties": {
                                "source_run_id": {
                                    "type": "string",
                                    "enum": [pending_source_run_id],
                                },
                                "decision": {
                                    "type": "string",
                                    "enum": ["continue", "replace", "cancel"],
                                },
                            },
                            "required": ["source_run_id", "decision"],
                            "additionalProperties": False,
                        },
                        origin="builtin",
                        read_only=False,
                        concurrency_safe=False,
                        destructive=False,
                        metadata={
                            "runtime_owned": True,
                            "pending_deliverable_choice": True,
                            "source_run_id": pending_source_run_id,
                            "source_contract_revision": (
                                pending_source_contract_revision
                            ),
                            "source_task_revision": pending_source_task_revision,
                            "pending_job_id": pending_job_id,
                            "pending_job_owner_run_id": (
                                pending_job_owner_run_id
                            ),
                            "pending_job_conversation_id": (
                                pending_job_conversation_id
                            ),
                        },
                    ),
                    resolve_pending_deliverable_choice_tool,
                )

            async def declare_deliverable_contract_tool(
                kind: str,
                accepted_extensions: list[str],
                visual_verification_required: bool,
            ) -> dict[str, Any]:
                """Bind the exact final file type before creating a deliverable.

                Call this first when the visible primary turn decides the user
                requested a durable file. When this tool is registered and
                permitted, it is directly callable before calling
                equip_lantai_tools; declare the output first, then equip the
                smallest group needed to create it. Derive the final kind and
                extensions from the user's requested outcome, not from a
                convenient skill or intermediate template. For an editable PPT,
                use kind="presentation", accepted_extensions=[".pptx"], and
                visual_verification_required=true. Use the corresponding exact
                final extension for DOCX, XLSX, or PDF. Once bound, the same task
                cannot drift to another output type unless the user explicitly
                changes it in a new turn.
                """

                from ...runtime.deliverable_contract import (
                    bind_deliverable_contract,
                )

                contract = await bind_deliverable_contract(
                    state_workspace_dir=state_workspace_dir,
                    run_id=run_id,
                    kind=kind,
                    accepted_extensions=accepted_extensions,
                    visual_verification_required=visual_verification_required,
                )
                return contract.model_dump(mode="json")

            registry.register_tool(
                ToolSpec(
                    name="declare_deliverable_contract",
                    description=(
                        declare_deliverable_contract_tool.__doc__ or ""
                    ).strip(),
                    input_schema={
                        "type": "object",
                        "properties": {
                            "kind": {"type": "string"},
                            "accepted_extensions": {
                                "type": "array",
                                "items": {"type": "string"},
                                "minItems": 1,
                                "maxItems": 4,
                            },
                            "visual_verification_required": {
                                "type": "boolean",
                            },
                        },
                        "required": [
                            "kind",
                            "accepted_extensions",
                            "visual_verification_required",
                        ],
                        "additionalProperties": False,
                    },
                    origin="builtin",
                    read_only=False,
                    concurrency_safe=False,
                    destructive=False,
                    metadata={
                        "runtime_owned": True,
                        "deliverable_contract": True,
                    },
                ),
                declare_deliverable_contract_tool,
            )

            async def finalize_deliverable_tool(
                primary_path: str,
                title: str,
                kind: str,
                entrypoint: str,
                verification_paths: list[str],
                acceptance_evidence: list = None,
            ) -> Any:
                """Declare a real file as this turn's primary deliverable.

                Use only after the user actually asked for a durable artifact
                and the main file has been created and checked. Never call this
                for casual chat, advice, analysis-only answers, or merely because
                the conversation mentions files, code, PPT, PDF, or documents.
                Acceptance evidence is descriptive context only; the Runtime
                computes every pass/fail decision from system-owned checks.
                If the bound contract requires visual verification, render the
                artifact first and pass the real workspace screenshot or preview
                paths in verification_paths; prose claims are not visual evidence.
                For a presentation, ordinary view_image calls are not a full-scope
                system review. Use a successful create_presentation or
                finalize_presentation_generation result, with every required page
                reviewed, and its model-visible finalize_deliverable_inputs
                (primary_path, entrypoint, kind, verification_paths and
                acceptance_evidence). The latter may be an empty array; it
                cannot substitute for system-owned checks. Supply
                the original task title as well. On failure,
                follow delivery_repair rather than bypassing the review gate.
                """

                from ..completion_gate import (
                    finalize_deliverable,
                    presentation_delivery_repair,
                )
                from ..experience import build_runtime_run_events_response

                result = await finalize_deliverable(
                    state_workspace_dir=state_workspace_dir,
                    effective_workspace_dir=effective_workspace_dir,
                    works_workspace_dir=works_workspace_dir,
                    run_id=run_id,
                    chat_id=chat_id,
                    response_status="completed",
                    assistant_text="deliverable finalization in progress",
                    final_response_id=f"pending:{run_id}",
                    primary_path=primary_path,
                    title=title,
                    kind=kind,
                    entrypoint=entrypoint,
                    acceptance_evidence=acceptance_evidence or (),
                    verification_paths=verification_paths,
                    runtime_events=build_runtime_run_events_response(
                        [state_workspace_dir],
                        run_id,
                        attempt_id=runtime_attempt_id,
                    ).events,
                    enforce_effect_settlement=False,
                )
                if result.work_item is None:
                    from ...runtime.tool_response import TextBlock, ToolResponse

                    failure_summary = (
                        result.failure_summary
                        or "Primary deliverable did not pass validation."
                    )
                    repair = presentation_delivery_repair(result)
                    if repair is not None:
                        failure_summary += "\nPresentation delivery repair:\n" + json.dumps(
                            repair, ensure_ascii=False
                        )
                    return ToolResponse(
                        content=[TextBlock(text=failure_summary)],
                        is_error=True,
                        metadata={
                            "error_code": "deliverable_verification_failed",
                            "delivery_status": result.delivery_status,
                            "primary_path": result.primary_path,
                            "acceptance_results": list(
                                result.acceptance_results
                            ),
                            "verification_receipt": (
                                result.verification_receipt
                            ),
                            "candidate_preserved": bool(result.primary_path),
                            "side_effect_outcome": "known_not_applied",
                            **({"delivery_repair": repair} if repair is not None else {}),
                        },
                    )
                return {
                    "primary_path": result.primary_path,
                    "artifact_id": result.work_item.artifact_id,
                    "work_id": result.work_item.id,
                    "source_run_id": run_id,
                    "delivery_status": result.delivery_status,
                    "acceptance_results": list(result.acceptance_results),
                    "verification_receipt": result.verification_receipt,
                }

            registry.register_tool(
                ToolSpec(
                    name="finalize_deliverable",
                    description=(finalize_deliverable_tool.__doc__ or "").strip(),
                    input_schema={
                        "type": "object",
                        "properties": {
                            "primary_path": {"type": "string"},
                            "title": {"type": "string"},
                            "kind": {"type": "string"},
                            "entrypoint": {"type": "string"},
                            "verification_paths": {
                                "type": "array",
                                "items": {"type": "string"},
                                "description": (
                                    "Real decodable render paths, not the primary PPTX. "
                                    "For presentations, use verification_paths in the "
                                    "model-visible finalize_deliverable_inputs from a successful "
                                    "canonical presentation generation result with "
                                    "system-owned review covering every required page."
                                ),
                            },
                            "acceptance_evidence": {
                                "type": "array",
                                "items": {
                                    "type": "object",
                                    "properties": {
                                        "check": {"type": "string"},
                                        "result": {"type": "string"},
                                        "status": {"type": "string"},
                                        "detail": {"type": "string"},
                                    },
                                    "required": ["check"],
                                    "additionalProperties": False,
                                },
                            },
                        },
                        "required": [
                            "primary_path",
                            "title",
                            "kind",
                            "entrypoint",
                            "verification_paths",
                            "acceptance_evidence",
                        ],
                        "additionalProperties": False,
                    },
                    origin="builtin",
                    read_only=False,
                    concurrency_safe=False,
                    destructive=False,
                    output_semantics=ToolOutputSemantics(
                        artifact_role="primary",
                        argument_path_fields=("primary_path",),
                        result_path_fields=("primary_path",),
                    ),
                    effect_recovery=RuntimeEffectRecoveryCapability(
                        external_side_effect=True,
                        idempotency="not_idempotent",
                        probe="tool_specific",
                        compensation="not_required",
                        on_ambiguity="probe_required",
                        notes=(
                            "Completed Works registrations are deduplicated, but a "
                            "crash after artifact copy/registration and before the "
                            "Works index commit can leave an unindexed side effect. "
                            "Unknown outcomes must be reconciled before retry."
                        ),
                    ),
                    metadata={
                        "runtime_owned": True,
                        "delivery_gate": True,
                        "trusted_effect_settlement": True,
                    },
                ),
                finalize_deliverable_tool,
            )
        return registry

    async def _stream_standard_runtime_turn(
        self,
        *,
        agent: Any,
        agent_config: Any,
        mcp_clients: list[Any],
        msgs: list,
        session_id: str,
        user_id: str,
        channel: str,
        request_context: dict[str, str],
        workspace_dir: Path,
        research_binding: Any = None,
    ) -> AsyncGenerator[tuple[Any, bool], None]:
        """Run the standard turn through RuntimeRunner's adapter seam."""

        foreground_preflight_started = time.perf_counter()
        foreground_latency_ms: dict[str, float] = {}
        engine = self._runtime_engine_for_agent(agent_config)
        request_context = dict(request_context)
        state_session_id = str(
            request_context.get("session_state_id") or session_id
        ).strip() or session_id
        collaboration_member_execution = bool(
            str(request_context.get("collaboration_member_id") or "").strip()
            and str(
                request_context.get("presentation_context") or ""
            ).strip()
            == "subagent_internal"
        )
        if collaboration_member_execution:
            engine = "native"
        if research_binding is not None:
            research_binding.current()
        current_messages = self._runtime_messages_from_legacy_msgs(msgs)
        plan_runtime_module = None
        if bool(getattr(getattr(agent_config, "plan", None), "enabled", False)):
            try:
                from ...plan.runtime import (
                    PlanRuntimeModule,
                    split_explicit_plan_request,
                )

                explicit_plan_entry, current_messages = (
                    split_explicit_plan_request(current_messages)
                )
                plan_runtime_module = await PlanRuntimeModule.load(
                    session=self.session,
                    agent_id=self.agent_id,
                    session_id=state_session_id,
                    user_id=user_id,
                    channel=channel,
                    explicit_entry=explicit_plan_entry,
                )
                if explicit_plan_entry:
                    # A planning turn is a runtime permission boundary, not
                    # merely a prompt style. Internal plan controls remain
                    # available; environment and external effects are denied.
                    request_context["permission_mode"] = "plan"
                    request_context["permission_profile"] = "workspace"
            except Exception:
                logger.warning(
                    "Native Runtime plan module failed open",
                    exc_info=True,
                )
                plan_runtime_module = None
        attachment_manifest_message = _runtime_attachment_manifest_message(
            request_context,
        )
        turn_messages = (
            [attachment_manifest_message, *current_messages]
            if attachment_manifest_message is not None
            else current_messages
        )
        turn_intelligence = None
        set_turn_intelligence = getattr(
            agent,
            "set_turn_intelligence_decision",
            None,
        )
        if callable(set_turn_intelligence) and not collaboration_member_execution:
            from ...agents.persona.subjective_reaction import (
                current_user_local_time,
            )
            from ...agents.turn_intelligence import TurnIntentDecision

            latest_user_text = next(
                (
                    message.text_content()
                    for message in reversed(current_messages)
                    if message.role == "user"
                ),
                "",
            )
            understanding_context = None
            understanding_projector = getattr(
                self.memory_manager,
                "project_user_understanding_context",
                None,
            )
            if callable(understanding_projector):
                stage_started = time.perf_counter()
                try:
                    understanding_context = understanding_projector(
                        latest_user_text,
                        language=(
                            "zh"
                            if re.search(r"[\u4e00-\u9fff]", latest_user_text)
                            else "en"
                        ),
                    )
                except Exception:
                    logger.warning(
                        "User understanding preflight failed open",
                        exc_info=True,
                    )
                finally:
                    foreground_latency_ms["user_understanding"] = (
                        time.perf_counter() - stage_started
                    ) * 1000
            set_understanding_context = getattr(
                agent,
                "set_user_understanding_turn_context",
                None,
            )
            if callable(set_understanding_context):
                set_understanding_context(understanding_context)
            now, timezone_name = current_user_local_time()
            subject_projection = None
            subject_projector = getattr(
                self.memory_manager,
                "project_subject_continuity_context",
                None,
            )
            if callable(subject_projector):
                stage_started = time.perf_counter()
                try:
                    subject_projection = subject_projector(
                        latest_user_text,
                        language=(
                            "zh"
                            if re.search(r"[\u4e00-\u9fff]", latest_user_text)
                            else "en"
                        ),
                        local_time=(
                            f"{now.strftime('%Y-%m-%d %H:%M')} {timezone_name}"
                        ),
                        session_id=state_session_id,
                    )
                except Exception:
                    logger.warning(
                        "Subject continuity preflight failed open",
                        exc_info=True,
                    )
                finally:
                    foreground_latency_ms["subject_continuity"] = (
                        time.perf_counter() - stage_started
                    ) * 1000
            set_subject_projection = getattr(
                agent,
                "set_subject_continuity_projection",
                None,
            )
            if callable(set_subject_projection):
                set_subject_projection(subject_projection)
            # Current-turn intent belongs to the primary model.  A dedicated
            # utility model used to run here synchronously, which serialized a
            # second provider request in front of every real response and made
            # utility availability part of first-token latency.  Keep the
            # projection seam explicit, but fail open to the primary turn; the
            # configured utility model remains available to post-answer
            # services such as memory, titles, summaries, and reflection.
            turn_intelligence = TurnIntentDecision.unavailable()
            foreground_latency_ms["turn_intelligence"] = 0.0
            set_turn_intelligence(turn_intelligence)
        selected_method = str(
            request_context.get("work_method_id") or "standard"
        ).strip().lower()
        method_resolution = {
            "task_contract": {"work_method_id": selected_method},
            **(
                {
                    "turn_intelligence": turn_intelligence.model_dump(
                        mode="json"
                    )
                }
                if turn_intelligence is not None
                else {}
            ),
        }
        if effective_work_method_id_from_metadata(method_resolution) in {
            "collaboration",
            "deep_research",
        }:
            # The legacy loop cannot host collaboration or enforce the
            # research-worker/final-delivery boundary.
            # Native Runtime may still wrap a legacy provider model adapter,
            # but it keeps the work-method contract authoritative.
            engine = "native"
        runtime_messages = turn_messages
        native_input_committed = engine != "native"
        native_persisted_steer_ids: set[str] = set()
        # The Console boundary supplies ``runtime_schema.Message`` objects,
        # while durable memory capture persists the historical LegacyMsg
        # shape for downgrade readability. Convert from the canonical
        # RuntimeMessage projection instead of leaking boundary objects into
        # the legacy serializer (which requires ``to_dict``).
        native_capture_messages = runtime_messages_to_legacy_msgs(
            current_messages,
        )
        native_terminal_type = ""
        native_terminal_turn_id = ""
        native_adapter: NativeLantaiRuntime | None = None
        memory_messages: list[RuntimeMessage] = []
        recall_messages: list[RuntimeMessage] = []
        plan_hint_message: RuntimeMessage | None = None
        if engine == "native":
            self._ensure_runtime_message_ids(current_messages)
            if not collaboration_member_execution:
                stage_started = time.perf_counter()
                async with DelegatedAsyncIterator(
                    self._stream_native_context_preflight(
                        agent,
                        current_messages,
                    )
                ) as _owned_events_2:
                    async for status_row in _owned_events_2:
                        try:
                            yield status_row
                        except asyncio.CancelledError as _stream_cancel:
                            _owned_events_2.request_cancel(_stream_cancel)
                foreground_latency_ms["native_context"] = (
                    time.perf_counter() - stage_started
                ) * 1000
                stage_started = time.perf_counter()
                prepare_persona_turn_async = getattr(
                    agent,
                    "prepare_persona_turn_async",
                    None,
                )
                if callable(prepare_persona_turn_async):
                    await prepare_persona_turn_async(msgs)
                else:
                    prepare_persona_turn = getattr(
                        agent,
                        "prepare_persona_turn",
                        None,
                    )
                    if callable(prepare_persona_turn):
                        prepare_persona_turn(msgs)
                foreground_latency_ms["persona_projection"] = (
                    time.perf_counter() - stage_started
                ) * 1000
                memory_messages = (
                    await self._runtime_messages_from_agent_memory(agent)
                )
                recall_messages = await self._native_memory_recall_messages(
                    agent,
                    current_messages,
                )
                if not any(
                    message.role == "system" for message in memory_messages
                ):
                    system_prompt = getattr(agent, "sys_prompt", "")
                    if isinstance(system_prompt, str) and system_prompt.strip():
                        memory_messages.insert(
                            0,
                            RuntimeMessage.text(
                                "system",
                                system_prompt,
                                metadata={
                                    "instruction_trust": "trusted_system",
                                    "instruction_origin": "prompt.compiled_agent",
                                },
                            ),
                        )
                plan_hint_message = (
                    plan_runtime_module.trusted_hint_message()
                    if plan_runtime_module is not None
                    else None
                )
            else:
                foreground_latency_ms["native_context"] = 0.0
                foreground_latency_ms["persona_projection"] = 0.0
            runtime_messages = (
                memory_messages
                + recall_messages
                + ([plan_hint_message] if plan_hint_message is not None else [])
                + turn_messages
            )

        state_workspace_dir = Path(
            request_context.get("state_workspace_dir") or workspace_dir,
        )
        runtime_metadata: dict[str, Any] = {
            "legacy_request_context": dict(request_context),
            "runtime_engine": engine,
            "state_workspace_dir": str(state_workspace_dir),
        }
        for key in (
            "collaboration_batch_id",
            "collaboration_member_id",
            "presentation_context",
            "collaboration_read_only",
            "execution_team_mode",
            "delegation_allowed_tools",
            "delegation_nested_allowed",
            "delegation_root_run_id",
            "delegation_parent_run_id",
            "delegation_parent_attempt_id",
            "delegation_parent_effect_id",
            "delegation_task_id",
            "delegation_attempt",
            "native_response_max_tokens",
            "pending_deliverable_context",
        ):
            if key in request_context:
                runtime_metadata[key] = request_context[key]
        foreground_latency_ms["total_before_runtime"] = (
            time.perf_counter() - foreground_preflight_started
        ) * 1000
        runtime_metadata["foreground_latency_ms"] = {
            "schema_version": "1",
            **{
                key: round(max(0.0, value), 2)
                for key, value in foreground_latency_ms.items()
            },
        }
        logger.info(
            "Foreground chat preflight completed",
            extra={
                "session_id": session_id,
                "runtime_engine": engine,
                "foreground_latency_ms": runtime_metadata[
                    "foreground_latency_ms"
                ],
            },
        )
        prompt_manifest = getattr(agent, "prompt_manifest", None)
        if isinstance(prompt_manifest, dict):
            runtime_metadata["prompt_manifest"] = prompt_manifest
            for runtime_message in memory_messages:
                if runtime_message.role == "system":
                    runtime_message.metadata.setdefault(
                        "instruction_trust",
                        "trusted_system",
                    )
                    runtime_message.metadata.setdefault(
                        "instruction_origin",
                        "prompt.compiled_agent",
                    )
                    runtime_message.metadata["prompt_manifest"] = prompt_manifest
                    break
        runtime_cfg = getattr(agent_config, "runtime", None)
        runtime_metadata.update(
            {
                "harness_mode": str(
                    getattr(runtime_cfg, "harness_mode", "shadow") or "shadow"
                ),
                "health_recovery": str(
                    getattr(runtime_cfg, "health_recovery", "safe") or "safe"
                ),
                "world_ledger_mode": str(
                    getattr(runtime_cfg, "world_ledger_mode", "read_only")
                    or "read_only"
                ),
            },
        )
        if turn_intelligence is not None:
            runtime_metadata["turn_intelligence"] = (
                turn_intelligence.model_dump(mode="json")
            )
        native_effect_journal: RuntimeEffectJournal | None = None
        native_artifact_effect_journal: RuntimeArtifactEffectJournal | None = None
        if engine == "native":
            native_effect_journal = RuntimeEffectJournal(state_workspace_dir)
            native_artifact_effect_journal = RuntimeArtifactEffectJournal(
                state_workspace_dir
            )
            run_id = str(request_context.get("run_id") or "").strip()
            if run_id:
                try:
                    contract = await RuntimeRunStore(
                        state_workspace_dir,
                    ).load_contract(run_id)
                except RuntimeRunStoreError:
                    logger.warning(
                        "Native Runtime could not load the chat run contract",
                        extra={"run_id": run_id},
                        exc_info=True,
                    )
                else:
                    if contract is not None:
                        try:
                            from ..experience.events import (
                                reconcile_runtime_run_checkpoint_from_events,
                            )

                            effects = await asyncio.to_thread(
                                native_effect_journal.list_effects,
                                run_id,
                                contract.attempt_id,
                            )
                            reconciled = (
                                await reconcile_runtime_run_checkpoint_from_events(
                                    workspace_dir=state_workspace_dir,
                                    run_id=run_id,
                                    attempt_id=contract.attempt_id,
                                    committed_effect_ids=frozenset(
                                        effect.effect_id
                                        for effect in effects
                                        if effect.status
                                        in {"committed", "probed_committed"}
                                    ),
                                )
                            )
                            if reconciled is not None:
                                contract = reconciled
                        except Exception:
                            logger.warning(
                                "Native Runtime could not reconcile chat checkpoint",
                                extra={"run_id": run_id},
                                exc_info=True,
                            )
                        runtime_metadata.update(
                            {
                                "attempt_id": contract.attempt_id,
                                "runtime_revision": contract.revision,
                                "runtime_lease_owner": contract.lease_owner,
                                "contract_hash": contract.contract_hash,
                                "policy_snapshot_hash": contract.policy_snapshot_hash,
                                "runtime_budget": contract.budget.model_dump(
                                    mode="json"
                                ),
                                "task_contract": (
                                    contract.task_contract.model_dump(mode="json")
                                    if contract.task_contract is not None
                                    else None
                                ),
                                "runtime_checkpoint": (
                                    contract.checkpoint.model_dump(mode="json")
                                    if contract.checkpoint is not None
                                    else None
                                ),
                                "runtime_attempt_reason": str(
                                    getattr(contract, "attempt_reason", "start")
                                    or "start"
                                ),
                            },
                        )
                        runtime_generation = request_context.get(
                            "runtime_generation"
                        )
                        if isinstance(runtime_generation, int):
                            runtime_metadata["runtime_generation"] = max(
                                0,
                                runtime_generation,
                            )

        if (
            engine == "native"
            and runtime_metadata.get("harness_mode") != "off"
            and request_context.get("run_id")
            and runtime_metadata.get("attempt_id")
        ):
            try:
                from ...runtime.coherence import HarnessHealthService

                health_service = HarnessHealthService(state_workspace_dir)
                health_snapshot = await health_service.audit(
                    run_id=str(request_context["run_id"]),
                    attempt_id=str(runtime_metadata["attempt_id"]),
                )
                if runtime_metadata.get("health_recovery") == "safe":
                    recovery_results = [
                        await asyncio.to_thread(
                            health_service.execute_safe_recovery,
                            action,
                        )
                        for action in health_snapshot.recovery_actions
                        if action.safe_automatic
                    ]
                    if any(
                        result.status == "executed"
                        for result in recovery_results
                    ):
                        health_snapshot = await health_service.audit(
                            run_id=str(request_context["run_id"]),
                            attempt_id=str(runtime_metadata["attempt_id"]),
                        )
                runtime_metadata["harness_health"] = {
                    "level": health_snapshot.level,
                    "snapshot_id": health_snapshot.snapshot_id,
                    "snapshot_sha256": health_snapshot.integrity_sha256,
                    "finding_codes": [
                        finding.code for finding in health_snapshot.findings
                    ],
                }
                health_capsule = health_snapshot.model_capsule()
                if health_capsule is not None:
                    runtime_metadata["harness_health_capsule"] = health_capsule
            except Exception:
                if runtime_metadata.get("harness_mode") == "enforced":
                    raise
                logger.warning(
                    "Coherence Harness health audit failed open in "
                    "shadow mode",
                    exc_info=True,
                )

        if engine == "native" and not collaboration_member_execution:
            persona_turn_plan = getattr(agent, "persona_turn_plan", None)
            if persona_turn_plan is not None and hasattr(
                persona_turn_plan,
                "to_dict",
            ):
                runtime_metadata["persona_turn_plan"] = persona_turn_plan.to_dict()
            runtime_metadata["ququ_hard_required"] = bool(
                getattr(agent, "ququ_hard_required", False),
            )
        runtime_input = RuntimeInput(
            request=RuntimeRequestContext(
                agent_id=self.agent_id,
                session_id=session_id,
                user_id=user_id,
                channel=channel,
                chat_id=request_context.get("chat_id"),
                run_id=request_context.get("run_id"),
                root_session_id=request_context.get("root_session_id"),
                workspace_dir=str(workspace_dir),
                metadata=runtime_metadata,
            ),
            messages=runtime_messages,
        )
        from ..approvals.service import get_approval_service

        approval_service = get_approval_service()
        configured_max_iters = request_context.get("runtime_max_iters")
        native_max_iters = (
            int(configured_max_iters)
            if isinstance(configured_max_iters, (int, str))
            and str(configured_max_iters).isdigit()
            and int(configured_max_iters) > 0
            else None
        )
        native_tool_registry = self._build_native_tool_registry(
            agent_config=agent_config,
            request_context=request_context,
            mcp_clients=mcp_clients,
            current_user_text=_get_last_user_text(msgs) or "",
            approval_service=approval_service,
            plan_runtime_module=plan_runtime_module,
            runtime_attempt_id=(
                str(runtime_metadata.get("attempt_id") or "").strip() or None
            ),
        )
        native_tool_policy_context = build_tool_policy_context(
            agent_config=agent_config,
            request_context=request_context,
            workspace_dir=workspace_dir,
        )
        if type(native_tool_policy_context) is ToolPolicyContext:
            runtime_input = runtime_input.model_copy(
                deep=True,
                update={
                    "tool_policy_context": (
                        native_tool_policy_context.model_dump(mode="json")
                    ),
                },
            )
        turn_id = runtime_input.request.run_id or session_id
        try:
            _turn_assembly_shadow = capture_shadow_turn_assembly(
                snapshot_id=f"{turn_id}:turn-assembly:v1",
                turn_id=turn_id,
                runtime_input=runtime_input,
                memory_messages=memory_messages,
                recall_messages=recall_messages,
                plan_hint_message=plan_hint_message,
                turn_messages=turn_messages,
                attachment_manifest_message=attachment_manifest_message,
                tool_registry=native_tool_registry,
                tool_policy_context=native_tool_policy_context,
            )
        except Exception:
            # This boundary belongs only to the new, observational S7 seam.
            # Existing preparation, policy, adapter, provider, and execution
            # failures remain authoritative and are never caught here.
            _turn_assembly_shadow = None
        _turn_assembly_input_handoff_failed = False
        if (
            _turn_assembly_shadow is not None
            and _turn_assembly_shadow.status == "captured"
            and _turn_assembly_shadow.snapshot is not None
        ):
            try:
                snapshot_runtime_input = (
                    runtime_input_from_turn_assembly_snapshot(
                        _turn_assembly_shadow.snapshot
                    )
                )
                snapshot_runtime_input_matches = (
                    runtime_inputs_are_exactly_equivalent(
                        snapshot_runtime_input,
                        runtime_input,
                    )
                )
            except Exception:
                # S7 keeps the already-prepared input as a bounded fallback
                # until the snapshot consumer has passed exact-app gates.
                _turn_assembly_input_handoff_failed = True
            else:
                if snapshot_runtime_input_matches:
                    runtime_input = snapshot_runtime_input
                else:
                    _turn_assembly_input_handoff_failed = True
        native_adapter = NativeLantaiRuntime(
            model_client_factory=(
                create_model_client if research_binding is None else
                lambda *args, **kwargs: create_model_client(
                    *args, **kwargs, dispatch_wrapper=research_binding.wrap_native,
                )
            ),
            tool_registry=native_tool_registry,
            tool_policy_context=native_tool_policy_context,
            approval_service=approval_service,
            effect_journal=native_effect_journal,
            artifact_effect_journal=native_artifact_effect_journal,
            # Desktop turns are progress-bounded, not resource-quota bounded.
            # The explicit headless CLI contract retains its finite operator
            # limit without restoring the Legacy loop.
            max_iters=native_max_iters,
            turn_timeout_seconds=None,
            progressive_tools=True,
            protected_write_admission=(
                lambda capability: protected_write_denial_reason(
                    self._beta_access_admission_guard,
                    capability,
                )
            ),
        )
        try:
            _request_policy_construction_receipt = (
                native_adapter.request_policy_construction_receipt()
            )
        except Exception:
            # Construction receipts are diagnostics only.  An unavailable
            # receipt must not alter the existing Native execution path.
            _request_policy_construction_receipt = None
        try:
            _request_policy_observation_token = (
                native_adapter.request_policy_observer.begin(runtime_input)
            )
        except Exception:
            _request_policy_observation_token = None
        if _request_policy_observation_token is None:
            runtime_runner = RuntimeRunner(native_adapter)
        else:
            runtime_runner = RuntimeRunner(
                native_adapter,
                diagnostic_run_turn=lambda observed_input, invocation: (
                    native_adapter.run_turn(
                        observed_input,
                        request_policy_observation_invocation=invocation,
                    )
                ),
                request_policy_observation_token=(
                    _request_policy_observation_token
                ),
            )
        _request_policy_observation_invocation = (
            runtime_runner.request_policy_observation_invocation
            if _request_policy_observation_token is not None
            else None
        )

        _turn_assembly_observation = None
        _turn_assembly_prepared_sources = None
        shadow_snapshot = (
            _turn_assembly_shadow.snapshot
            if _turn_assembly_shadow is not None
            and _turn_assembly_shadow.status == "captured"
            and not _turn_assembly_input_handoff_failed
            else None
        )
        if shadow_snapshot is None:
            _turn_assembly_oracle_report = (
                unavailable_turn_assembly_oracle_report(
                    (
                        "snapshot_runtime_input_unavailable"
                        if _turn_assembly_input_handoff_failed
                        else "shadow_snapshot_unavailable"
                    )
                )
            )
        else:
            try:
                _turn_assembly_observation = observe_native_input_boundary(
                    runtime_input=runtime_input,
                    tool_registry=native_tool_registry,
                    tool_policy_context=native_tool_policy_context,
                )
            except Exception:
                _turn_assembly_oracle_report = (
                    unavailable_turn_assembly_oracle_report(
                        "oracle_projection_failed"
                    )
                )
            else:
                try:
                    _turn_assembly_prepared_sources = (
                        observe_prepared_turn_sources(
                            runtime_input=runtime_input,
                            memory_messages=memory_messages,
                            recall_messages=recall_messages,
                            plan_hint_message=plan_hint_message,
                            turn_messages=turn_messages,
                            attachment_manifest_message=(
                                attachment_manifest_message
                            ),
                            persona_preparation_receipt=getattr(
                                agent,
                                "persona_preparation_receipt",
                                None,
                            ),
                        )
                    )
                except Exception:
                    # Source-boundary evidence is observational.  If it is
                    # unavailable, retain A3's truthful partial comparison.
                    _turn_assembly_prepared_sources = None
                try:
                    _turn_assembly_oracle_report = (
                        compare_turn_assembly_snapshot(
                            observation=_turn_assembly_observation,
                            snapshot=shadow_snapshot,
                            prepared_sources=(
                                _turn_assembly_prepared_sources
                            ),
                            request_policy_construction_receipt=(
                                _request_policy_construction_receipt
                            ),
                        )
                    )
                except Exception:
                    _turn_assembly_oracle_report = (
                        unavailable_turn_assembly_oracle_report(
                            "oracle_comparison_failed"
                        )
                    )
        try:
            log_turn_assembly_oracle_report(_turn_assembly_oracle_report)
        except Exception:
            # The logging helper is fail-open itself; keep the Runner boundary
            # defensive so replacing a diagnostic sink cannot block a turn.
            pass

        pending_terminal_item = None
        open_native_tools: dict[str, RuntimeEvent] = {}
        open_native_approvals: dict[str, RuntimeEvent] = {}
        from ...plugins.registry import PluginRegistry

        runtime_plugin_registry = PluginRegistry()
        async with DelegatedAsyncIterator(
            _runtime_events_with_terminal_boundary(
                runtime_runner.run_turn(runtime_input),
            )
        ) as _owned_events_3:
            async for event in _owned_events_3:
                runtime_plugin_registry.publish_runtime_event(event)
                if engine == "native":
                    try:
                        if event.type in _RUNTIME_TURN_TERMINAL_TYPES:
                            await self._close_native_runtime_control_events(
                                terminal_event=event,
                                open_tools=open_native_tools,
                                open_approvals=open_native_approvals,
                                request_context=request_context,
                                workspace_dir=state_workspace_dir,
                                session_id=session_id,
                                attempt_id=(
                                    str(runtime_metadata.get("attempt_id") or "").strip()
                                    or None
                                ),
                            )
                        await self._persist_native_runtime_control_event(
                            event=event,
                            request_context=request_context,
                            workspace_dir=state_workspace_dir,
                            session_id=session_id,
                            attempt_id=(
                                str(runtime_metadata.get("attempt_id") or "").strip()
                                or None
                            ),
                        )
                        if event.type == "tool.call.started":
                            key = _runtime_tool_event_key(event)
                            if key:
                                open_native_tools[key] = event
                        elif event.type == "tool.call.delta":
                            key = _runtime_tool_event_key(event)
                            current = open_native_tools.get(key)
                            if key and current is not None:
                                open_native_tools[key] = event.model_copy(
                                    update={
                                        "payload": {
                                            **dict(current.payload or {}),
                                            **dict(event.payload or {}),
                                        },
                                        "presentation": (
                                            event.presentation
                                            or current.presentation
                                        ),
                                    },
                                )
                        elif event.type == "tool.call.completed":
                            open_native_tools.pop(
                                _runtime_tool_event_key(event),
                                None,
                            )
                        elif event.type == "approval.requested":
                            key = _runtime_approval_event_key(event)
                            if key:
                                open_native_approvals[key] = event
                        elif event.type == "approval.resolved":
                            open_native_approvals.pop(
                                _runtime_approval_event_key(event),
                                None,
                            )
                    except Exception:
                        # The primary Console stream still carries the converted
                        # tool/approval message. A ledger projection failure must
                        # be observable, but must not take down model execution.
                        logger.warning(
                            "Failed to persist Native Runtime control event: %s",
                            event.type,
                            exc_info=True,
                        )
                    if (
                        event.type == "runtime.warning"
                        and event.payload.get("kind") == "run_inbox_consumed"
                    ):
                        steer_client_turn_id = str(
                            event.payload.get("client_turn_id") or ""
                        ).strip()
                        steer_text = str(
                            event.payload.get("steer_text") or ""
                        ).strip()
                        if (
                            steer_client_turn_id
                            and steer_text
                            and steer_client_turn_id not in native_persisted_steer_ids
                        ):
                            steer_message = RuntimeMessage.text(
                                "user",
                                steer_text,
                                metadata={
                                    "client_turn_id": steer_client_turn_id,
                                    "operation": "steer",
                                },
                            )
                            await self._persist_runtime_message_to_agent_memory(
                                agent,
                                steer_message,
                            )
                            native_capture_messages.append(
                                runtime_message_to_legacy_msg(steer_message),
                            )
                            native_persisted_steer_ids.add(steer_client_turn_id)

                    if (
                        event.type in {"tool.call.completed", "tool.completed"}
                        and event.payload.get("ok", True) is not False
                    ):
                        tool_output = event.payload.get("output")
                        if tool_output is not None and tool_output != "":
                            if isinstance(tool_output, str):
                                output_text = tool_output
                            else:
                                try:
                                    output_text = json.dumps(
                                        tool_output,
                                        ensure_ascii=False,
                                        default=str,
                                    )
                                except (TypeError, ValueError):
                                    output_text = str(tool_output)
                            native_capture_messages.append(
                                LegacyMsg(
                                    name=str(
                                        event.payload.get("name")
                                        or event.payload.get("tool_name")
                                        or "tool"
                                    ),
                                    role="assistant",
                                    content=[
                                        {
                                            "type": "tool_result",
                                            "name": str(
                                                event.payload.get("name")
                                                or event.payload.get("tool_name")
                                                or "tool"
                                            ),
                                            "output": output_text[:12000],
                                        },
                                    ],
                                    metadata={
                                        "memory_source": "tool_result",
                                        "tool_call_id": str(
                                            event.payload.get("id")
                                            or event.item_id
                                            or ""
                                        ),
                                    },
                                ),
                            )

                    if (
                        not native_input_committed
                        and event.type in _NATIVE_MEMORY_COMMIT_EVENT_TYPES
                    ):
                        for runtime_message in current_messages:
                            await self._persist_runtime_message_to_agent_memory(
                                agent,
                                runtime_message,
                            )
                        native_input_committed = True

                if (
                    engine == "native"
                    and native_input_committed
                    and event.type == "message.completed"
                ):
                    message_payload = event.payload.get("message")
                    if isinstance(message_payload, dict):
                        try:
                            runtime_message = RuntimeMessage.model_validate(
                                message_payload,
                            )
                            await self._persist_runtime_message_to_agent_memory(
                                agent,
                                runtime_message,
                            )
                            if not self._is_runtime_presentation_only(
                                runtime_message
                            ):
                                native_capture_messages.append(
                                    runtime_message_to_legacy_msg(runtime_message),
                                )
                        except Exception:
                            logger.debug(
                                "Failed to persist native assistant message",
                                exc_info=True,
                            )
                legacy_item = runtime_event_to_legacy_stream_item(event)
                if legacy_item is not None:
                    if event.type in _RUNTIME_TURN_TERMINAL_TYPES:
                        native_terminal_type = event.type
                        native_terminal_turn_id = event.turn_id or ""
                        pending_terminal_item = legacy_item
                        continue
                    try:
                        yield legacy_item
                    except asyncio.CancelledError as _stream_cancel:
                        _owned_events_3.request_cancel(_stream_cancel)
        if (
            _request_policy_observation_invocation is not None
            and native_terminal_type
            and native_terminal_turn_id
            and shadow_snapshot is not None
            and _turn_assembly_observation is not None
        ):
            try:
                _request_policy_effective_receipt = (
                    native_adapter.request_policy_observer.take(
                        _request_policy_observation_invocation,
                        terminal_type=native_terminal_type,
                        terminal_turn_id=native_terminal_turn_id,
                    )
                )
            except Exception:
                _request_policy_effective_receipt = None
            if _request_policy_effective_receipt is not None:
                try:
                    _request_policy_dispatch_receipt = (
                        native_adapter.request_policy_observer.take_dispatch(
                            _request_policy_observation_invocation,
                            terminal_type=native_terminal_type,
                            terminal_turn_id=native_terminal_turn_id,
                        )
                    )
                except Exception:
                    _request_policy_dispatch_receipt = None
                try:
                    policy_observer = native_adapter.request_policy_observer
                    _request_policy_consumption_receipt = (
                        policy_observer.take_consumption(
                            _request_policy_observation_invocation,
                            terminal_type=native_terminal_type,
                            terminal_turn_id=native_terminal_turn_id,
                        )
                    )
                except Exception:
                    _request_policy_consumption_receipt = None
                try:
                    _turn_assembly_oracle_report = (
                        compare_turn_assembly_snapshot(
                            observation=_turn_assembly_observation,
                            snapshot=shadow_snapshot,
                            prepared_sources=_turn_assembly_prepared_sources,
                            request_policy_construction_receipt=(
                                _request_policy_construction_receipt
                            ),
                            request_policy_effective_receipt=(
                                _request_policy_effective_receipt
                            ),
                            request_policy_dispatch_receipt=(
                                _request_policy_dispatch_receipt
                            ),
                            request_policy_consumption_receipt=(
                                _request_policy_consumption_receipt
                            ),
                        )
                    )
                    log_turn_assembly_oracle_report(
                        _turn_assembly_oracle_report
                    )
                except Exception:
                    pass
        # The user-visible turn is complete before semantic memory analysis.
        # Yield the terminal row first so a generous background model budget
        # never makes the chat UI look stuck.
        if pending_terminal_item is not None:
            yield pending_terminal_item
            pending_terminal_item = None

        native_post_answer = post_answer_admission(
            terminal_type=native_terminal_type,
            input_committed=native_input_committed,
        )
        if (
            engine == "native"
            and native_adapter is not None
            and native_post_answer.schedule_reflection
        ):
            try:
                from ..post_answer_reflection_service import (
                    PostAnswerReflectionService,
                )

                reflection_service = PostAnswerReflectionService(
                    state_workspace_dir,
                    agent_id=self.agent_id,
                    research_workspace=(
                        request_context.get("works_workspace_dir")
                        or self.workspace_dir or WORKING_DIR
                    ),
                )

                async def persist_late_ququ(
                    runtime_message: RuntimeMessage,
                ) -> None:
                    await self._persist_post_answer_reflection_message(
                        agent,
                        runtime_message,
                    )

                reflection_service.recover_pending(
                    chat_id=str(request_context.get("chat_id") or ""),
                    session_id=state_session_id,
                    persist_message=persist_late_ququ,
                )
                take_reflection_request = getattr(
                    native_adapter,
                    "take_post_answer_reflection_request",
                    None,
                )
                reflection_request = (
                    take_reflection_request()
                    if callable(take_reflection_request)
                    else None
                )
                if reflection_request is not None:
                    reflection_request = replace(
                        reflection_request,
                        session_id=state_session_id,
                    )
                    reflection_service.schedule(
                        reflection_request,
                        persist_message=persist_late_ququ,
                    )
            except Exception:
                # Foreground delivery is already complete. Keep the durable
                # answer valid even if sidecar admission itself fails.
                logger.warning(
                    "Failed to schedule Native post-answer reflection",
                    exc_info=True,
                )

        if (
            engine == "native"
            and native_post_answer.schedule_memory_capture
            and self.memory_manager is not None
            and should_capture_global_memory(request_context)
        ):
            try:
                schedule_capture = getattr(
                    self.memory_manager,
                    "schedule_capture_turn",
                    None,
                )
                if callable(schedule_capture):
                    schedule_capture(
                        messages=native_capture_messages,
                        session_id=state_session_id,
                        source="native_post_reply",
                    )
                else:
                    capture_result = await self.memory_manager.capture_turn(
                        messages=native_capture_messages,
                        session_id=state_session_id,
                        source="native_post_reply",
                    )
                    if (
                        isinstance(capture_result, dict)
                        and capture_result.get("maintenance_required")
                    ):
                        system2_runner = getattr(
                            self.memory_manager,
                            "run_system2_consolidation",
                            None,
                        )
                        if callable(system2_runner):
                            await system2_runner(reason="native_post_reply")
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.warning(
                    "Native Runtime post-reply memory capture failed",
                    exc_info=True,
                )
        if pending_terminal_item is not None:
            yield pending_terminal_item

    async def query_handler(
        self,
        msgs,
        request: Any = None,
        **kwargs,
    ):
        """
        Handle agent query.
        """
        request = _coerce_agent_request(request)
        _bind_request_external_file_grants(
            getattr(request, "channel_meta", None),
        )
        logger.debug(
            f"AgentRunner.query_handler called: agent_id={self.agent_id}, "
            f"msgs={msgs}, request={request}",
        )
        query = _get_last_user_text(msgs)
        session_id = getattr(request, "session_id", "") or ""
        user_id = getattr(request, "user_id", "") or ""
        channel = getattr(request, "channel", DEFAULT_CHANNEL)
        channel_meta = getattr(request, "channel_meta", None)
        if not isinstance(channel_meta, dict):
            channel_meta = {}
        chat = None
        requested_chat_id = str(channel_meta.get("chat_id") or "").strip()
        if self._chat_manager is not None and requested_chat_id:
            requested_chat = await self._chat_manager.get_chat(requested_chat_id)
            if (
                requested_chat is not None
                and requested_chat.session_id == session_id
                and requested_chat.user_id == user_id
                and requested_chat.channel == channel
            ):
                chat = requested_chat
        state_session_id = (
            await resolve_chat_session_state_id(self._chat_manager, chat)
            if self._chat_manager is not None and chat is not None
            else session_id
        )
        channel_meta["session_state_id"] = state_session_id
        request.channel_meta = channel_meta
        request.session_state_id = state_session_id

        # Check if query is a command (including /approval)
        logger.debug(f"Query: {query!r}, is_command: {_is_command(query)}")
        if query and _is_command(query):
            logger.info("Command path: %s", query.strip()[:50])
            async with DelegatedAsyncIterator(
                run_command_path(request, msgs, self)
            ) as _owned_events_4:
                async for (msg, last) in _owned_events_4:
                    try:
                        yield msg, last
                    except asyncio.CancelledError as _stream_cancel:
                        _owned_events_4.request_cancel(_stream_cancel)
            return

        if self._beta_access_admission_guard is not None:
            self._beta_access_admission_guard.require(
                boundary=AccessBoundary.WORK_START,
                capability="chat.turn.start",
            )

        logger.debug(
            f"AgentRunner.stream_query: request={request}, "
            f"agent_id={self.agent_id}",
        )

        # Set agent context for model creation
        from ..agent_context import (
            set_current_agent_id,
            set_current_session_id,
            set_current_root_session_id,
        )

        set_current_agent_id(self.agent_id)

        # Set session_id in context for token usage tracking
        set_current_session_id(state_session_id)

        from ...research.context import set_binding, reset_binding

        research_context_token = set_binding(None)
        agent = None
        session_state_loaded = False
        try:
            session_id = request.session_id
            user_id = request.user_id
            channel = getattr(request, "channel", DEFAULT_CHANNEL)

            logger.info(
                "Handle agent query:\n%s",
                json.dumps(
                    {
                        "session_id": session_id,
                        "user_id": user_id,
                        "channel": channel,
                        "msgs_len": len(msgs) if msgs else 0,
                        "msgs_str": str(msgs)[:300] + "...",
                    },
                    ensure_ascii=False,
                    indent=2,
                ),
            )

            # Optional sender display name from channel_meta.user_name.
            channel_meta = getattr(request, "channel_meta", None)
            if not isinstance(channel_meta, dict):
                channel_meta = {}
            user_name = channel_meta.get("user_name")
            from ...config.context import set_current_workspace_context
            from ..workspace_context import resolve_workspace_context

            workspace_context = resolve_workspace_context(
                agent_home=self.workspace_dir or WORKING_DIR,
                agent_id=self.agent_id,
                channel_meta=channel_meta,
            )
            workspace_context.state_root.mkdir(parents=True, exist_ok=True)
            workspace_context.project_root.mkdir(parents=True, exist_ok=True)
            set_current_workspace_context(workspace_context)
            effective_workspace_dir = workspace_context.project_root
            env_context = build_env_context(
                session_id=session_id,
                user_id=user_id,
                user_name=user_name,
                channel=channel,
                working_dir=str(effective_workspace_dir),
            )

            # Get MCP clients from manager (hot-reloadable)
            mcp_clients = []
            guanlan_capability_status = "starting"
            if self._mcp_manager is not None:
                mcp_clients = await self._mcp_manager.get_clients()
                get_runtime_state = getattr(
                    self._mcp_manager,
                    "get_runtime_state",
                    None,
                )
                if callable(get_runtime_state):
                    guanlan_state = await get_runtime_state("guanlan")
                    if guanlan_state is not None:
                        guanlan_capability_status = guanlan_state.status

            # Load agent-specific configuration
            agent_config = load_agent_config(self.agent_id)

            logger.debug(f"Enabled MCP: {mcp_clients}")

            # Build base request context
            base_request_context = {
                "session_id": session_id,
                "session_state_id": state_session_id,
                "user_id": user_id,
                "channel": channel,
                "agent_id": self.agent_id,
                "runtime_engine": self._runtime_engine_for_agent(agent_config),
                "runtime_entrypoint": "runner.standard_turn",
                "workspace_dir": str(effective_workspace_dir),
                "state_workspace_dir": str(workspace_context.state_root),
                "works_workspace_dir": str(workspace_context.agent_home),
                "workspace_access_mode": workspace_context.access_mode,
                "workspace_trust_state": workspace_context.trust_state,
                "permission_snapshot_at": time.time(),
                "guanlan_capability_status": guanlan_capability_status,
            }
            work_method_id = channel_meta.get("work_method_id")
            if isinstance(work_method_id, str) and work_method_id.strip():
                base_request_context["work_method_id"] = work_method_id.strip()
            trusted_current_attachments = (
                _trusted_current_attachments_from_channel_meta(channel_meta)
            )
            if trusted_current_attachments:
                base_request_context["trusted_current_attachments"] = (
                    trusted_current_attachments
                )
            request_metadata = getattr(request, "metadata", None)
            if isinstance(request_metadata, dict):
                for key in (
                    "collaboration_batch_id",
                    "collaboration_member_id",
                    "presentation_context",
                ):
                    value = request_metadata.get(key)
                    if isinstance(value, str) and value.strip():
                        base_request_context[key] = value.strip()[:160]
                for key in (
                    "delegation_root_run_id",
                    "delegation_parent_run_id",
                    "delegation_parent_attempt_id",
                    "delegation_parent_effect_id",
                    "delegation_task_id",
                ):
                    value = request_metadata.get(key)
                    if (
                        isinstance(value, str)
                        and value.strip()
                        and len(value.strip()) <= 160
                    ):
                        base_request_context[key] = value.strip()
                if request_metadata.get("collaboration_read_only") is True:
                    base_request_context["collaboration_read_only"] = True
                if request_metadata.get("execution_team_mode") == "scoped_execute":
                    base_request_context["execution_team_mode"] = "scoped_execute"
                    raw_allowed_tools = request_metadata.get(
                        "delegation_allowed_tools"
                    )
                    if isinstance(raw_allowed_tools, list):
                        base_request_context["delegation_allowed_tools"] = [
                            item.strip()
                            for item in raw_allowed_tools
                            if isinstance(item, str) and item.strip()
                        ][:64]
                    base_request_context["delegation_nested_allowed"] = (
                        request_metadata.get("delegation_nested_allowed") is True
                    )
                    delegation_attempt = request_metadata.get(
                        "delegation_attempt"
                    )
                    if (
                        isinstance(delegation_attempt, int)
                        and not isinstance(delegation_attempt, bool)
                        and delegation_attempt > 0
                    ):
                        base_request_context["delegation_attempt"] = (
                            delegation_attempt
                        )
                native_response_max_tokens = request_metadata.get(
                    "native_response_max_tokens"
                )
                if (
                    isinstance(native_response_max_tokens, int)
                    and not isinstance(native_response_max_tokens, bool)
                    and native_response_max_tokens > 0
                ):
                    base_request_context["native_response_max_tokens"] = (
                        native_response_max_tokens
                    )
                if request_metadata.get("convergence_mode") in {
                    "off",
                    "shadow",
                    "enforce",
                }:
                    base_request_context[
                        "convergence_mode"
                    ] = request_metadata["convergence_mode"]
                convergence_policy = request_metadata.get("convergence_policy")
                if isinstance(convergence_policy, dict):
                    base_request_context["convergence_policy"] = {
                        str(key): value
                        for key, value in convergence_policy.items()
                        if isinstance(value, (str, int, float, bool))
                    }
            builtin_team_agent_ids = {
                str(config["id"]) for config in BUILTIN_TEAM_AGENT_CONFIGS
            }
            user_explicitly_referenced_skill = bool(
                isinstance(query, str)
                and (
                    query.lstrip().startswith("/")
                    or "skill" in query.lower()
                    or "技能" in query
                )
            )
            if (
                base_request_context.get("presentation_context")
                == "subagent_internal"
            ):
                base_request_context["skill_exposure"] = "subagent_internal"
            elif user_explicitly_referenced_skill:
                base_request_context["skill_exposure"] = "explicit"
                base_request_context["skill_invocation_origin"] = "user_explicit"
            elif self.agent_id in builtin_team_agent_ids:
                base_request_context["skill_exposure"] = "subagent_internal"
            if workspace_context.workspace_grant_id:
                base_request_context["workspace_grant_id"] = (
                    workspace_context.workspace_grant_id
                )
            for key in ("chat_id", "run_id", "turn_id"):
                value = channel_meta.get(key)
                if isinstance(value, str) and value.strip():
                    base_request_context[key] = value.strip()
            pending_deliverable_context = channel_meta.get(
                "pending_deliverable_context"
            )
            if isinstance(pending_deliverable_context, dict):
                # The channel boundary attaches this server-owned projection
                # after it resolves the prior durable Run.  Keep it out of
                # free-form user metadata, but carry it intact through the
                # Runner so Native Runtime can require semantic resolution
                # before any answer or presentation mutation.
                base_request_context["pending_deliverable_context"] = dict(
                    pending_deliverable_context
                )
            runtime_generation = channel_meta.get("runtime_generation")
            if isinstance(runtime_generation, int) and not isinstance(
                runtime_generation,
                bool,
            ):
                # The session registry owns the mutable generation fence.  It
                # is projected into channel metadata immediately before the
                # producer starts, and must survive this trust-boundary copy so
                # Run Inbox steers admitted against that generation can be
                # claimed by the same Native Runtime attempt.  Dropping it here
                # makes a durable `steered` receipt lie: the inbox row is then
                # rejected against the runner's default generation 0.
                base_request_context["runtime_generation"] = max(
                    0,
                    runtime_generation,
                )
            permission_mode = channel_meta.get("permission_mode")
            if isinstance(permission_mode, str):
                normalized_permission_mode = permission_mode.strip().lower()
                if normalized_permission_mode in {
                    "default",
                    "plan",
                    "accept_edits",
                    "quiet",
                    "full_auto",
                }:
                    base_request_context["permission_mode"] = (
                        normalized_permission_mode
                    )
            permission_profile = channel_meta.get("permission_profile")
            if isinstance(permission_profile, str):
                normalized_permission_profile = permission_profile.strip().lower()
                if normalized_permission_profile in {
                    "observe",
                    "workspace",
                    "full",
                }:
                    base_request_context["permission_profile"] = (
                        normalized_permission_profile
                    )
            coding_mode = channel_meta.get("coding_mode")
            if isinstance(coding_mode, bool):
                base_request_context["coding_mode"] = (
                    "true" if coding_mode else "false"
                )
            elif isinstance(coding_mode, str) and coding_mode.strip():
                base_request_context["coding_mode"] = coding_mode.strip()
            coding_context = channel_meta.get("coding_context")
            if isinstance(coding_context, str) and coding_context.strip():
                base_request_context["coding_context"] = coding_context.strip()[:8000]
            from ...agents.mac_use_context import normalized_mac_use_target

            mac_use_target = normalized_mac_use_target(
                channel_meta.get("mac_use_target"),
            )
            if mac_use_target is not None:
                base_request_context["mac_use_target"] = mac_use_target
            work_edit_context = channel_meta.get("work_edit_context")
            if isinstance(work_edit_context, str) and work_edit_context.strip():
                base_request_context["work_edit_context"] = (
                    work_edit_context.strip()[:8000]
                )
            work_method_id = channel_meta.get("work_method_id")
            if isinstance(work_method_id, str) and work_method_id.strip():
                from ...work_methods import normalize_work_method_id

                normalized_work_method_id = normalize_work_method_id(
                    work_method_id
                )
                if normalized_work_method_id:
                    base_request_context["work_method_id"] = (
                        normalized_work_method_id
                    )
            trusted_private_editor_binding = (
                _trusted_private_editor_binding_from_channel_meta(channel_meta)
            )
            if trusted_private_editor_binding:
                base_request_context.update(trusted_private_editor_binding)
                if isinstance(work_edit_context, str) and work_edit_context.strip():
                    base_request_context["work_edit_context"] = work_edit_context.strip()
            raw_knowledge_sources = channel_meta.get("knowledge_sources")
            knowledge_sources: list[dict[str, str]] = []
            if isinstance(raw_knowledge_sources, list):
                for reference in raw_knowledge_sources[:20]:
                    if not isinstance(reference, dict):
                        continue
                    source_path = str(reference.get("path") or "").strip()
                    source_filename = str(reference.get("filename") or "").strip()
                    if source_path and source_filename:
                        knowledge_sources.append(
                            {
                                "path": source_path,
                                "filename": source_filename,
                                "kind": str(reference.get("kind") or ""),
                            }
                        )
            knowledge_source = channel_meta.get("knowledge_source")
            if not knowledge_sources and isinstance(knowledge_source, dict):
                source_path = str(knowledge_source.get("path") or "").strip()
                source_filename = str(
                    knowledge_source.get("filename") or ""
                ).strip()
                if source_path and source_filename:
                    knowledge_sources.append(
                        {
                            "path": source_path,
                            "filename": source_filename,
                            "kind": str(knowledge_source.get("kind") or ""),
                        }
                    )
            if knowledge_sources:
                base_request_context["knowledge_sources"] = knowledge_sources
                base_request_context["knowledge_scope_mode"] = (
                    "expanded"
                    if channel_meta.get("knowledge_scope_mode") == "expanded"
                    else "exact"
                )
                if len(knowledge_sources) == 1:
                    base_request_context["knowledge_source"] = knowledge_sources[0]
            knowledge_source_context = channel_meta.get(
                "knowledge_source_context"
            )
            if (
                isinstance(knowledge_source_context, str)
                and knowledge_source_context.strip()
            ):
                base_request_context["knowledge_source_context"] = (
                    knowledge_source_context.strip()[:18_000]
                )
            from ...agents.kdocs_references import normalize_kdocs_references

            kdocs_references = normalize_kdocs_references(
                channel_meta.get("kdocs_references")
            )
            if kdocs_references:
                base_request_context["kdocs_references"] = kdocs_references
            if "turn_id" not in base_request_context:
                run_id = base_request_context.get("run_id")
                if isinstance(run_id, str) and run_id:
                    base_request_context["turn_id"] = run_id

            # Extract root_session_id from request payload (agent chat)
            payload_root_session = getattr(request, "root_session_id", "")
            if payload_root_session and isinstance(payload_root_session, str):
                base_request_context["root_session_id"] = payload_root_session
                set_current_root_session_id(payload_root_session)
                root_preview = (
                    payload_root_session[:12]
                    if len(payload_root_session) >= 12
                    else payload_root_session
                )
                logger.debug(
                    "Runner: using root_session_id from payload: %s",
                    root_preview,
                )
            else:
                # Current session is the root
                base_request_context["root_session_id"] = session_id
                set_current_root_session_id(session_id)
                session_preview = (
                    session_id[:12] if len(session_id) >= 12 else session_id
                )
                logger.debug(
                    "Runner: current session is root: %s",
                    session_preview,
                )

            # Mission Mode: /mission
            _ws = effective_workspace_dir
            mission_info: dict | None = None

            mission_result = await maybe_handle_mission_command(
                query=query,
                msgs=msgs,
                workspace_dir=_ws,
                agent_id=self.agent_id,
                rewrite_fn=self._rewrite_last_message_text,
                session_id=session_id,
            )
            if mission_result is not None and not isinstance(mission_result, dict):
                yield mission_result, True
                return
            if isinstance(mission_result, dict):
                mission_info = mission_result

            # Mission state is resumed only through an explicit `/mission`
            # command (including `/mission continue`).  A persisted active
            # mission must never rewrite an unrelated chat message into a
            # worker/verifier continuation.

            # Mission Mode: inject context reminder for the explicit command
            if mission_info is not None:
                # Inject context reminder for active mission
                loop_dir = mission_info.get("loop_dir", "")
                phase = mission_info.get("mission_phase", 1)
                if phase == 1:
                    refresher = (
                        f"[Mission active — dir: `{loop_dir}`]\n"
                        f"You are in Mission Phase 1 (PRD review). "
                        f"The user's message follows.\n"
                        f"If the user is confirming the PRD, update "
                        f"`{loop_dir}/loop_config.json` setting "
                        f"`current_phase` to `execution_confirmed`.\n"
                        f"If the user requests changes, modify "
                        f"prd.json.\n---\n"
                    )
                elif phase == 2:
                    refresher = (
                        f"[Mission active — dir: `{loop_dir}`]\n"
                        f"You are in Mission Phase 2 (execution). "
                        f"The user's follow-up message follows.\n"
                        f"Continue the worker → verifier pipeline. "
                        f"Check prd.json progress and dispatch workers "
                        f"for remaining stories.\n---\n"
                    )
                else:
                    refresher = f"[Mission active — dir: `{loop_dir}`]\n---\n"
                original = query or ""
                self._rewrite_last_message_text(
                    msgs,
                    refresher + original,
                )

            # --- Plan Mode ------------------------------------------
            plan_notebook = None
            plan_enabled = getattr(
                getattr(agent_config, "plan", None),
                "enabled",
                False,
            )
            if plan_enabled:
                try:
                    from ...plan.hints import SimplePlanToHint, set_plan_gate

                    hint_gen = SimplePlanToHint()
                    plan_notebook = legacy_create_plan_notebook(
                        plan_to_hint=hint_gen,
                    )
                    hint_gen.bind_notebook(plan_notebook)

                    # Detect /plan <description> and set gate
                    if query and query.strip().lower().startswith("/plan "):
                        plan_desc = query.strip()[6:].strip()
                        if plan_desc:
                            set_plan_gate(plan_notebook, enabled=True)
                            self._rewrite_last_message_text(
                                msgs,
                                plan_desc,
                            )
                            logger.info(
                                "Plan mode: /plan gate set, desc=%s",
                                plan_desc[:60],
                            )

                    # Register SSE broadcast hook + state tracking
                    from ...plan.broadcast import broadcast_plan_update
                    from ...plan.schemas import plan_to_response

                    def _on_plan_change(  # pylint: disable=protected-access
                        nb,
                        plan,
                    ):
                        had_plan = getattr(nb, "_qp_had_plan", False)
                        prev_id = getattr(nb, "_qp_prev_plan_id", None)

                        if plan is not None:
                            cur_id = plan.id
                            if not had_plan or cur_id != prev_id:
                                nb._plan_just_mutated = True
                            nb._qp_prev_plan_id = cur_id
                        else:
                            if had_plan:
                                nb._plan_recently_finished = True
                            nb._qp_prev_plan_id = None
                        nb._qp_had_plan = plan is not None

                        payload = {
                            "type": "plan_update",
                            "plan": (
                                plan_to_response(plan).model_dump()
                                if plan is not None
                                else None
                            ),
                        }
                        broadcast_plan_update(
                            self.agent_id,
                            payload,
                            session_id=session_id,
                        )

                    plan_notebook.register_plan_change_hook(
                        "broadcast",
                        _on_plan_change,
                    )
                except Exception:
                    logger.warning(
                        "Failed to create plan notebook",
                        exc_info=True,
                    )
                    plan_notebook = None

            agent = LegacyLantaiAgent(
                agent_config=agent_config,
                env_context=env_context,
                mcp_clients=mcp_clients,
                memory_manager=self.memory_manager,
                context_manager=self.context_manager,
                request_context=base_request_context,
                workspace_dir=self.workspace_dir,
                runtime_workspace_dir=effective_workspace_dir,
                task_tracker=self._task_tracker,
                plan_notebook=plan_notebook,
            )
            await agent.register_mcp_clients()

            logger.debug(
                f"Agent Query msgs {msgs}",
            )

            name = "New Chat"
            if len(msgs) > 0:
                content = msgs[0].get_text_content()
                if content:
                    name = _semantic_fallback_title(content)
                else:
                    name = "Media Message"

            logger.debug(
                f"DEBUG chat_manager status: "
                f"_chat_manager={self._chat_manager}, "
                f"is_none={self._chat_manager is None}, "
                f"agent_id={self.agent_id}",
            )

            if self._chat_manager is not None:
                logger.debug(
                    f"Runner: Calling get_or_create_chat for "
                    f"session_id={session_id}, user_id={user_id}, "
                    f"channel={channel}, name={name}",
                )
                if chat is None:
                    chat = await self._chat_manager.get_or_create_chat(
                        session_id,
                        user_id,
                        channel,
                        name=name,
                    )
                    state_session_id = await resolve_chat_session_state_id(
                        self._chat_manager,
                        chat,
                    )
                    channel_meta["session_state_id"] = state_session_id
                    request.session_state_id = state_session_id
                    base_request_context["session_state_id"] = state_session_id
                    set_current_session_id(state_session_id)
                logger.debug(f"Runner: Got chat: {chat.id}")
                base_request_context.setdefault("chat_id", chat.id)
                if not base_request_context.get("run_id"):
                    generated_run_id = f"subagent-{uuid4()}"
                    base_request_context["run_id"] = generated_run_id
                    base_request_context["turn_id"] = generated_run_id
                agent_request_context = getattr(agent, "_request_context", None)
                if isinstance(agent_request_context, dict):
                    agent_request_context.update(base_request_context)
            else:
                logger.warning(
                    f"ChatManager is None! Cannot auto-register chat for "
                    f"session_id={session_id}",
                )

            from ...research.chat import bind_chat

            research_binding = bind_chat(chat, workspace_context.agent_home)
            set_binding(research_binding)
            research_arguments = ({"research_binding": research_binding}
                                  if research_binding is not None else {})

            # Skill info (/<name> without input) is display-only
            if mission_info is None:
                skill_response = self._maybe_inject_skill(
                    query,
                    msgs,
                    agent.toolkit.skills,
                )
                if skill_response is not None:
                    yield skill_response, True
                    return

            try:
                await self.session.load_session_state(
                    session_id=state_session_id,
                    user_id=user_id,
                    channel=channel,
                    # Persist migrations with the completed turn, so a
                    # history-read failure leaves the source file intact.
                    persist_migrations=False,
                    agent=agent,
                )
            except (KeyError, TypeError, ValueError) as exc:
                from ...exceptions import AgentStateError

                # A partially restored agent must never execute or replace
                # the persisted history in the completion/finally path.
                raise AgentStateError(
                    session_id=state_session_id,
                    message=(
                        "会话历史加载失败，原始记录已保留，本轮未执行。"
                        "请恢复兼容版本或修复会话数据后重试。"
                    ),
                    details={"reason": "session_state_schema_mismatch"},
                ) from exc
            session_state_loaded = True

            # Rebuild system prompt so it always reflects the latest
            # AGENTS.md / SOUL.md / PROFILE.md, not the stale one saved
            # in the session state.
            agent.rebuild_sys_prompt()

            # --- Execution: Mission Mode (phased) or standard -----
            if mission_info is not None:
                from ...agents.mission.mission_runner import (
                    IMPLEMENTATION_TOOLS,
                    run_mission_phase1,
                    run_mission_phase2,
                )

                phase = mission_info["mission_phase"]
                loop_dir = Path(mission_info["loop_dir"])
                max_iters = mission_info.get(
                    "max_iterations",
                    20,
                )
                mission_request_context = dict(base_request_context)
                mission_request_context["mission_phase"] = phase
                if phase == 2:
                    mission_request_context["runtime_blocked_tool_names"] = sorted(
                        runtime_blocked_tool_names(mission_request_context)
                        | IMPLEMENTATION_TOOLS,
                    )

                async def stream_mission_turn(
                    turn_msgs: list,
                ) -> AsyncGenerator[tuple[Any, bool], None]:
                    async with DelegatedAsyncIterator(
                        self._stream_standard_runtime_turn(
                            agent=agent,
                            agent_config=agent_config,
                            mcp_clients=mcp_clients,
                            msgs=turn_msgs,
                            session_id=session_id,
                            user_id=user_id,
                            channel=channel,
                            request_context=mission_request_context,
                            workspace_dir=effective_workspace_dir,
                            **research_arguments,
                        )
                    ) as _owned_events_5:
                        async for item in _owned_events_5:
                            try:
                                yield item
                            except asyncio.CancelledError as _stream_cancel:
                                _owned_events_5.request_cancel(_stream_cancel)

                if phase == 1:
                    async with DelegatedAsyncIterator(
                        run_mission_phase1(
                            agent=agent,
                            msgs=msgs,
                            loop_dir=loop_dir,
                            max_iterations=max_iters,
                            agent_id=self.agent_id,
                            stream_turn=stream_mission_turn,
                        )
                    ) as _owned_events_6:
                        async for (msg, last) in _owned_events_6:
                            try:
                                yield msg, last
                            except asyncio.CancelledError as _stream_cancel:
                                _owned_events_6.request_cancel(_stream_cancel)
                else:
                    async with DelegatedAsyncIterator(
                        run_mission_phase2(
                            agent=agent,
                            msgs=msgs,
                            loop_dir=loop_dir,
                            max_iterations=max_iters,
                            agent_id=self.agent_id,
                            stream_turn=stream_mission_turn,
                        )
                    ) as _owned_events_7:
                        async for (msg, last) in _owned_events_7:
                            try:
                                yield msg, last
                            except asyncio.CancelledError as _stream_cancel:
                                _owned_events_7.request_cancel(_stream_cancel)
            else:
                async with DelegatedAsyncIterator(
                    self._stream_standard_runtime_turn(
                        agent=agent,
                        agent_config=agent_config,
                        mcp_clients=mcp_clients,
                        msgs=msgs,
                        session_id=session_id,
                        user_id=user_id,
                        channel=channel,
                        request_context=base_request_context,
                        workspace_dir=effective_workspace_dir,
                        **research_arguments,
                    )
                ) as _owned_events_8:
                    async for (msg, last) in _owned_events_8:
                        try:
                            yield msg, last
                        except asyncio.CancelledError as _stream_cancel:
                            _owned_events_8.request_cancel(_stream_cancel)

        except asyncio.CancelledError as exc:
            if isinstance(exc, _NativeMemoryLoadCancelled):
                session_state_loaded = False
            logger.info(f"query_handler: {session_id} cancelled!")

            # Cancel all pending approvals for this root session
            root_session_id = base_request_context.get(
                "root_session_id",
                session_id,
            )
            from ..approvals.service import get_approval_service

            approval_svc = get_approval_service()
            cancelled_count = (
                await approval_svc.cancel_all_pending_by_root_session(
                    root_session_id,
                )
            )
            if cancelled_count > 0:
                logger.info(
                    "Auto-denied %d pending approval(s) for root session %s",
                    cancelled_count,
                    root_session_id[:8]
                    if len(root_session_id) >= 8
                    else root_session_id,
                )

            raise
        except Exception as e:
            if isinstance(e, _NativeMemoryLoadError):
                session_state_loaded = False
                e.details["session_id"] = state_session_id
            if is_legacy_app_base_exception(e):
                raise

            if _is_provider_content_filter_error(e):
                sanitized_count = 0
                if agent is not None:
                    sanitized_count = (
                        _sanitize_recent_tool_results_after_content_filter(
                            agent,
                        )
                    )

                fallback_msg = _provider_content_filter_fallback_msg()
                if agent is not None and getattr(agent, "memory", None):
                    try:
                        await agent.memory.add(fallback_msg)
                    except Exception:
                        logger.debug(
                            "Failed to store content-filter fallback message",
                            exc_info=True,
                        )

                logger.warning(
                    "Provider content filter rejected the model prompt; "
                    "returned graceful fallback and sanitized %d recent "
                    "tool result(s).",
                    sanitized_count,
                    exc_info=True,
                )
                yield fallback_msg, True
                return

            model_name = None
            if agent and hasattr(agent, "model"):
                model_name = getattr(agent.model, "model_name", None)

            converted = convert_model_exception(e, model_name)

            # Preserve all original error dump logic
            debug_dump_path = write_query_error_dump(
                request=request,
                exc=converted,
                locals_=locals(),
            )
            path_hint = (
                f"\n(Details:  {debug_dump_path})" if debug_dump_path else ""
            )
            logger.exception(f"Error in query handler: {converted}{path_hint}")
            if debug_dump_path:
                setattr(converted, "debug_dump_path", debug_dump_path)
            raise converted from e
        finally:
            try:
                if agent is not None and session_state_loaded:
                    await self.session.save_session_state(
                        session_id=state_session_id,
                        user_id=user_id,
                        channel=channel,
                        agent=agent,
                    )

                if self._chat_manager is not None and chat is not None:
                    await self._chat_manager.touch_chat(chat.id)
            finally:
                reset_binding(research_context_token)

    async def stream_query(self, request: Any):
        """Channel-facing process adapter.

        ChannelManager wires channels to ``runner.stream_query(request)``.
        Keep this thin adapter separate from ``query_handler`` so the console
        and external channels share the same request-to-message contract.
        """

        request = _coerce_agent_request(request)
        owner_key = str(
            (getattr(request, "channel_meta", None) or {}).get("chat_id")
            or getattr(request, "session_state_id", "")
            or getattr(request, "session_id", "")
            or getattr(request, "user_id", "")
            or "anonymous-agent-turn"
        ).strip()
        from ..background_semantics import foreground_model_turn

        async with foreground_model_turn(self.agent_id):
            async with get_runtime_concurrency_governor().slot(
                category="agent",
                owner_key=owner_key,
            ):
                msgs = list(getattr(request, "input", None) or [])
                started_message_ids: set[str] = set()
                streamed_message_texts: dict[str, str] = {}
                async with DelegatedAsyncIterator(
                    self.query_handler(msgs, request=request)
                ) as _owned_events_9:
                    async for item in _owned_events_9:
                        for event in legacy_stream_item_to_channel_events(
                            item,
                            started_message_ids=started_message_ids,
                            streamed_message_texts=streamed_message_texts,
                        ):
                            try:
                                yield event
                            except asyncio.CancelledError as _stream_cancel:
                                _owned_events_9.request_cancel(_stream_cancel)

    async def start(self, *args, **kwargs) -> None:
        """Initialize runner state required by workspace services."""

        if self.session is None:
            await self.init_handler(*args, **kwargs)
        self._started = True

    async def stop(self, *args, **kwargs) -> None:
        """Stop runner lifecycle state.

        The runner currently has no long-lived internal task; this method keeps
        the workspace service contract stable for reload/shutdown paths.
        """

        await self.shutdown_handler(*args, **kwargs)
        self._started = False

    async def init_handler(self, *args, **kwargs):
        """
        Init handler.
        """
        # Load environment variables from .env file
        # env_path = Path(__file__).resolve().parents[4] / ".env"
        env_path = Path("./") / ".env"
        if env_path.exists():
            load_dotenv(env_path)
            logger.debug(f"Loaded environment variables from {env_path}")
        else:
            logger.debug(
                f".env file not found at {env_path}, "
                "using existing environment variables",
            )

        session_dir = str(
            (self.workspace_dir if self.workspace_dir else WORKING_DIR)
            / "sessions",
        )
        Path(session_dir).mkdir(parents=True, exist_ok=True)
        self.session = SafeJSONSession(save_dir=session_dir)

    async def shutdown_handler(self, *args, **kwargs):
        """
        Shutdown handler.
        """
