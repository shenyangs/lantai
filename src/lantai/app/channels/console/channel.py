# -*- coding: utf-8 -*-
# pylint: disable=too-many-branches,too-many-statements
"""Console Channel.

A lightweight channel that prints all agent responses to stdout.

Messages are sent to the agent via the standard Lantai process endpoint or via
POST /console/chat. This channel handles the **output** side:
whenever a completed message event or a proactive send arrives, it is
pretty-printed to the terminal.
"""

from __future__ import annotations

from lantai.utils.async_iterator import DelegatedAsyncIterator

import copy
import json
import logging
import os
import sys
from datetime import datetime
from pathlib import Path
from typing import Any, AsyncGenerator, Dict, List, Optional, Union

from lantai.app.channels.runtime_schema import (
    MessageType,
    Message,
    RunStatus,
)

from ....config.config import ConsoleConfig as ConsoleChannelConfig
from ...console_push_store import append as push_store_append
from ....constant import DEFAULT_MEDIA_DIR
from ....providers.reasoning import (
    ReasoningRequestOptions,
    reset_reasoning_options,
    set_reasoning_options,
)
from ....runtime.converters import legacy_stream_item_to_channel_events
from ..base import (
    BaseChannel,
    AudioContent,
    ContentType,
    FileContent,
    ImageContent,
    OnReplySent,
    OutgoingContentPart,
    ProcessHandler,
    VideoContent,
    TextContent,
)
from ..utils import file_url_to_local_path


logger = logging.getLogger(__name__)

# ANSI colour helpers (degrade gracefully if not a tty)
_USE_COLOR = hasattr(sys.stdout, "isatty") and sys.stdout.isatty()

_GREEN = "\033[32m" if _USE_COLOR else ""
_YELLOW = "\033[33m" if _USE_COLOR else ""
_RED = "\033[31m" if _USE_COLOR else ""
_BOLD = "\033[1m" if _USE_COLOR else ""
_RESET = "\033[0m" if _USE_COLOR else ""


def _ts() -> str:
    return datetime.now().strftime("%H:%M:%S")


class ConsoleChannel(BaseChannel):
    """Console Channel: prints agent responses to stdout.

    Input is handled by the Lantai process endpoint; this
    channel only takes care of output (printing to the terminal).

    Supports filtering options via config:
        - show_tool_details: Display tool execution details
        - filter_tool_messages: Hide intermediate tool messages
        - filter_thinking: Hide agent thinking/reasoning blocks
    """

    channel = "console"

    def __init__(
        self,
        process: ProcessHandler,
        enabled: bool,
        bot_prefix: str,
        on_reply_sent: OnReplySent = None,
        show_tool_details: bool = True,
        filter_tool_messages: bool = False,
        filter_thinking: bool = False,
        workspace_dir: Optional[Union[str, Path]] = None,
        media_dir: Optional[str] = None,
    ):
        """Initialize ConsoleChannel.

        Args:
            process: Handler for agent requests.
            enabled: Whether this channel is active.
            bot_prefix: Prefix string for bot messages.
            on_reply_sent: Callback when reply is sent.
            show_tool_details: Whether to show tool execution details.
            filter_tool_messages: Whether to filter out tool messages.
            filter_thinking: Whether to filter thinking/reasoning blocks.
            workspace_dir: Agent workspace directory; used to resolve uploaded
                file names (media_dir = workspace_dir / "media").
            media_dir: Agent workspace directory for resolving uploads.
        """
        super().__init__(
            process,
            on_reply_sent=on_reply_sent,
            show_tool_details=show_tool_details,
            filter_tool_messages=filter_tool_messages,
            filter_thinking=filter_thinking,
        )
        self.enabled = enabled
        self.bot_prefix = bot_prefix
        self._workspace_dir = (
            Path(workspace_dir).expanduser() if workspace_dir else None
        )

        # Use workspace-specific media dir if workspace_dir is provided
        if not media_dir and self._workspace_dir:
            self._media_dir = self._workspace_dir / "media"
        elif media_dir:
            self._media_dir = Path(media_dir).expanduser()
        else:
            self._media_dir = DEFAULT_MEDIA_DIR
        self._media_dir.mkdir(parents=True, exist_ok=True)

        # Windows stdout encoding fix
        if sys.platform == "win32":
            try:
                sys.stdout.reconfigure(encoding="utf-8", errors="replace")
                sys.stderr.reconfigure(encoding="utf-8", errors="replace")
            except Exception as e:
                logger.debug(
                    "Failed to reconfigure stdout encoding on Windows: %s",
                    e,
                )

    @property
    def media_dir(self) -> Path:
        """Media directory"""
        return self._media_dir

    @classmethod
    def from_env(
        cls,
        process: ProcessHandler,
        on_reply_sent: OnReplySent = None,
    ) -> "ConsoleChannel":
        return cls(
            process=process,
            enabled=os.getenv("CONSOLE_CHANNEL_ENABLED", "1") == "1",
            bot_prefix=os.getenv("CONSOLE_BOT_PREFIX", ""),
            on_reply_sent=on_reply_sent,
            media_dir=os.getenv("CONSOLE_MEDIA_DIR", ""),
        )

    @classmethod
    def from_config(
        cls,
        process: ProcessHandler,
        config: ConsoleChannelConfig,
        on_reply_sent: OnReplySent = None,
        show_tool_details: bool = True,
        filter_tool_messages: bool = False,
        filter_thinking: bool = False,
        workspace_dir: Optional[Union[str, Path]] = None,
    ) -> "ConsoleChannel":
        """Create ConsoleChannel from config.

        Args:
            process: Handler for agent requests.
            config: Console channel configuration.
            on_reply_sent: Callback when reply is sent.
            show_tool_details: Whether to show tool execution details.
            filter_tool_messages: Whether to filter out tool messages.
            filter_thinking: Whether to filter thinking/reasoning blocks.
            workspace_dir: Agent workspace directory for resolving uploads.

        Returns:
            Configured ConsoleChannel instance.
        """
        return cls(
            process=process,
            enabled=config.enabled,
            bot_prefix=config.bot_prefix or "",
            on_reply_sent=on_reply_sent,
            show_tool_details=show_tool_details,
            filter_tool_messages=filter_tool_messages,
            filter_thinking=filter_thinking,
            workspace_dir=workspace_dir,
            media_dir=config.media_dir or "",
        )

    def resolve_session_id(
        self,
        sender_id: str,
        channel_meta: Optional[dict] = None,
    ) -> str:
        """Resolve session_id: use explicit meta['session_id'] when provided
        (e.g. from the HTTP /console/chat API), otherwise fall back to
        'console:<sender_id>'.
        """
        if channel_meta and channel_meta.get("session_id"):
            return channel_meta["session_id"]
        return f"{self.channel}:{sender_id}"

    def _resolve_console_upload_refs(
        self,
        content_parts: List[Any],
    ) -> List[Any]:
        """Resolve Image/File/Audio/VideoContent."""
        if not self._media_dir:
            return content_parts

        def _value(part: Any, key: str, default: Any = None) -> Any:
            if isinstance(part, dict):
                return part.get(key, default)
            return getattr(part, key, default)

        def _is_type(content_type: Any, expected: ContentType) -> bool:
            expected_value = getattr(expected, "value", expected)
            return content_type == expected or content_type == expected_value

        def _attachment_fields(part: Any) -> Dict[str, Any]:
            return {
                key: value
                for key in (
                    "file_name",
                    "file_source_url",
                    "stored_name",
                    "attachment_role",
                    "attachment_role_label",
                    "attachment_schema_version",
                    "attachment_digest",
                    "attachment_version",
                    "attachment_source_id",
                    "attachment_source_chat_id",
                    "attachment_source_command_id",
                    "attachment_grant_ref",
                    "attachment_grant_state",
                    "attachment_upload_receipt",
                )
                if (value := _value(part, key)) is not None
            }

        def resolve_one(part: Any) -> Optional[OutgoingContentPart]:
            content_type = _value(part, "type")
            if _is_type(content_type, ContentType.IMAGE):
                url = _value(part, "image_url")
                if url:
                    return ImageContent(
                        type=ContentType.IMAGE,
                        image_url=url,
                        filename=_value(part, "filename"),
                        media_type=_value(part, "media_type"),
                        file_size=_value(part, "file_size"),
                        **_attachment_fields(part),
                    )
            elif _is_type(content_type, ContentType.VIDEO):
                url = _value(part, "video_url")
                if url:
                    return VideoContent(
                        type=ContentType.VIDEO,
                        video_url=url,
                        filename=_value(part, "filename"),
                        media_type=_value(part, "media_type"),
                        file_size=_value(part, "file_size"),
                        **_attachment_fields(part),
                    )
            elif _is_type(content_type, ContentType.AUDIO):
                url = _value(part, "data")
                if url:
                    return AudioContent(
                        type=ContentType.AUDIO,
                        data=url,
                        filename=_value(part, "filename"),
                        media_type=_value(part, "media_type"),
                        file_size=_value(part, "file_size"),
                        **_attachment_fields(part),
                    )
            elif _is_type(content_type, ContentType.FILE):
                url = _value(part, "file_url")
                if url:
                    return FileContent(
                        type=ContentType.FILE,
                        filename=_value(part, "filename") or url,
                        file_url=url,
                        media_type=_value(part, "media_type"),
                        file_size=_value(part, "file_size"),
                        **_attachment_fields(part),
                    )
            elif _is_type(content_type, ContentType.TEXT):
                return TextContent(
                    type=ContentType.TEXT,
                    text=str(_value(part, "text", "") or ""),
                )
            return part

        input_content_parts = []
        for content in content_parts:
            part = resolve_one(content)
            if part is not None:
                input_content_parts.append(part)
        return input_content_parts

    def _should_process_media_only_desktop_payload(
        self,
        content_parts: List[Any],
    ) -> bool:
        """Console desktop sends image/file/video-only payloads as complete input."""

        if self._content_has_text(content_parts):
            return False
        media_types = {
            ContentType.IMAGE,
            ContentType.FILE,
            ContentType.VIDEO,
        }
        return any(
            getattr(part, "type", None) in media_types
            for part in (content_parts or [])
        )

    def _merge_pending_content_for_session(
        self,
        session_id: str,
        content_parts: List[Any],
    ) -> List[Any]:
        pending = self._pending_content_by_session.pop(session_id, [])
        return pending + list(content_parts)

    def build_agent_request_from_native(self, native_payload: Any) -> Any:
        """
        Build AgentRequest from console native payload (dict with
        channel_id, sender_id, content_parts, meta). content_parts are
        runtime Content types.
        """
        payload = native_payload if isinstance(native_payload, dict) else {}
        channel_id = payload.get("channel_id") or self.channel
        sender_id = payload.get("sender_id") or ""
        content_parts = payload.get("content_parts") or []
        content_parts = self._resolve_console_upload_refs(content_parts)
        meta = payload.get("meta") or {}
        session_id = self.resolve_session_id(sender_id, meta)
        request = self.build_agent_request_from_user_content(
            channel_id=channel_id,
            sender_id=sender_id,
            session_id=session_id,
            content_parts=content_parts,
            channel_meta=meta,
        )
        kdocs_references = meta.get("kdocs_references")
        if isinstance(kdocs_references, list) and request.input:
            existing_metadata = request.input[0].metadata
            request.input[0].metadata = {
                **(existing_metadata if isinstance(existing_metadata, dict) else {}),
                "kdocs_references": kdocs_references,
            }
        request.channel_meta = meta
        return request

    async def _extract_media_message(self, message: Message) -> Message | None:
        """Extract media message from message."""
        parts = self._message_to_content_parts(message)
        media_message = None
        if message.type in (
            MessageType.FUNCTION_CALL_OUTPUT,
            MessageType.PLUGIN_CALL_OUTPUT,
            MessageType.MCP_TOOL_CALL_OUTPUT,
        ):
            new_parts = []
            for part in parts:
                if part.type == ContentType.IMAGE:
                    new_part = copy.deepcopy(part)
                    new_part.image_url = file_url_to_local_path(
                        new_part.image_url,
                    )
                    new_parts.append(new_part)
                elif part.type == ContentType.VIDEO:
                    new_part = copy.deepcopy(part)
                    new_part.video_url = file_url_to_local_path(
                        new_part.video_url,
                    )
                    new_parts.append(new_part)
                elif part.type == ContentType.AUDIO:
                    new_part = copy.deepcopy(part)
                    new_part.data = file_url_to_local_path(new_part.data)
                    new_parts.append(new_part)
                elif part.type == ContentType.FILE:
                    new_part = copy.deepcopy(part)
                    new_part.file_url = file_url_to_local_path(
                        new_part.file_url,
                    )
                    new_parts.append(new_part)
            if new_parts:
                media_message = Message(
                    type=MessageType.MESSAGE,
                    role="assistant",
                    content=new_parts,
                )
        return media_message

    def _extract_token_usage(
        self,
        session_id: Optional[str] = None,
    ) -> Optional[Dict[str, Any]]:
        from ....token_usage import TokenRecordingModelWrapper

        if not session_id:
            return None

        usage = TokenRecordingModelWrapper.pop_usage_for_session(session_id)
        logger.info("Usage for session %s (cleaned up): %s", session_id, usage)
        return usage

    async def stream_one(self, payload: Any) -> AsyncGenerator[str, None]:
        """Process one payload and yield SSE-formatted events"""
        reasoning_token = None
        if isinstance(payload, dict) and "content_parts" in payload:
            session_id = self.resolve_session_id(
                payload.get("sender_id") or "",
                payload.get("meta"),
            )
            content_parts = self._resolve_console_upload_refs(
                payload.get("content_parts") or [],
            )
            if self._should_process_media_only_desktop_payload(content_parts):
                should_process = True
                merged = self._merge_pending_content_for_session(
                    session_id,
                    content_parts,
                )
            else:
                should_process, merged = self._apply_no_text_debounce(
                    session_id,
                    content_parts,
                )
            if not should_process:
                return
            payload = {**payload, "content_parts": merged}
            request = self.build_agent_request_from_native(payload)
        else:
            request = payload
            session_id = getattr(request, "session_id", "") or ""
            if getattr(request, "input", None):
                contents = list(
                    getattr(request.input[0], "content", None) or [],
                )
                should_process, merged = self._apply_no_text_debounce(
                    session_id,
                    contents,
                )
                if not should_process:
                    return
                if merged and hasattr(request.input[0], "content"):
                    request.input[0].content = merged
        try:
            send_meta = getattr(request, "channel_meta", None) or {}
            deep_thinking = send_meta.get("deep_thinking")
            if isinstance(deep_thinking, dict):
                reasoning_token = set_reasoning_options(
                    ReasoningRequestOptions(
                        enabled=bool(deep_thinking.get("enabled")),
                        effort=str(deep_thinking.get("effort") or "high"),
                    ),
                )
            send_meta.setdefault("bot_prefix", self.bot_prefix)
            last_response = None
            event_count = 0
            started_message_ids: set[str] = set()
            streamed_message_texts: dict[str, str] = {}

            async with DelegatedAsyncIterator(
                self._process(request)
            ) as _owned_events_1:
                async for raw_event in _owned_events_1:
                    for event in legacy_stream_item_to_channel_events(
                        raw_event,
                        started_message_ids=started_message_ids,
                        streamed_message_texts=streamed_message_texts,
                    ):
                        event_count += 1
                        obj = getattr(event, "object", None)
                        status = getattr(event, "status", None)
                        ev_type = getattr(event, "type", None)

                        logger.debug(
                            "console event #%s: object=%s status=%s type=%s",
                            event_count,
                            obj,
                            status,
                            ev_type,
                        )

                        if obj == "response" and status == RunStatus.Completed:
                            event_output = event.output
                            event.output = []
                            if event_output is not None:
                                for message in event_output:
                                    event.output.append(message)
                                    media_message = await self._extract_media_message(
                                        message,
                                    )
                                    if media_message:
                                        event.output.append(media_message)

                        if obj == "response":
                            current_session_id = str(
                                send_meta.get("session_state_id")
                                or getattr(request, "session_id", "")
                                or ""
                            )
                            usage_data = self._extract_token_usage(current_session_id)
                            if usage_data and hasattr(event, "usage"):
                                setattr(event, "usage", usage_data)

                        data = self._serialize_event_for_sse(event)
                        try:
                            yield f"data: {data}\n\n"
                        except asyncio.CancelledError as _stream_cancel:
                            _owned_events_1.request_cancel(_stream_cancel)

                        if obj == "message" and status == RunStatus.Completed:
                            media_message = await self._extract_media_message(event)
                            if media_message:
                                media_json = self._serialize_event_for_sse(
                                    media_message,
                                )
                                try:
                                    yield f"data: {media_json}\n\n"
                                except asyncio.CancelledError as _stream_cancel:
                                    _owned_events_1.request_cancel(_stream_cancel)

                            parts = self._message_to_content_parts(event)
                            self._print_parts(parts, ev_type)

                        elif obj == "response":
                            last_response = event

            logger.info(
                "console stream done: event_count=%s has_response=%s",
                event_count,
                last_response is not None,
            )

            err_msg = self._get_response_error_message(last_response)
            if err_msg:
                self._print_error(err_msg)

            to_handle = request.user_id or ""
            if self._on_reply_sent:
                self._on_reply_sent(
                    self.channel,
                    to_handle,
                    request.session_id or f"{self.channel}:{to_handle}",
                )

        except Exception as e:
            logger.exception("console process/reply failed")
            err_msg = str(e).strip() or "An error occurred while processing."
            self._print_error(err_msg)
            now = datetime.now()
            failed_payload = {
                "id": f"response_error_{int(now.timestamp() * 1000)}",
                "object": "response",
                "created_at": int(now.timestamp()),
                "status": "failed",
                "type": "response.failed",
                "output": [],
                "error": {
                    "message": err_msg,
                    "type": type(e).__name__,
                    "code": "console_process_failed",
                },
                "usage": None,
            }
            yield f"data: {json.dumps(failed_payload, ensure_ascii=False)}\n\n"
        finally:
            if reasoning_token is not None:
                reset_reasoning_options(reasoning_token)

    async def consume_one(self, payload: Any) -> None:
        """Process one payload; drain stream_one (queue/terminal)."""
        async for _ in self.stream_one(payload):
            pass

    # ── pretty-print helpers ────────────────────────────────────────

    def _safe_print(self, text: str) -> None:
        """Safely print text, handling Windows encoding and pipe issues.

        On Windows, print() can raise OSError [Errno 22] when output is
        piped or contains unsupported characters. This wrapper handles
        such cases gracefully.
        """
        try:
            print(text)
        except OSError as e:
            if e.errno == 22:
                logger.warning(
                    "Print failed with OSError [Errno 22], attempting "
                    "fallback encoding",
                )
                try:
                    sys.stdout.buffer.write(
                        text.encode("utf-8", errors="replace"),
                    )
                    sys.stdout.buffer.write(b"\n")
                    sys.stdout.buffer.flush()
                except Exception as fallback_err:
                    logger.error(
                        "Failed to print even with fallback: %s",
                        fallback_err,
                    )
            else:
                logger.error("Print failed with OSError: %s", e)

    def _print_parts(
        self,
        parts: List[OutgoingContentPart],
        ev_type: Optional[str] = None,
    ) -> None:
        """Print outgoing content parts to stdout."""
        ts = _ts()
        label = f" ({ev_type})" if ev_type else ""
        self._safe_print(
            f"\n{_GREEN}{_BOLD}🤖 [{ts}] Bot{label}{_RESET}",
        )
        for p in parts:
            t = getattr(p, "type", None)
            if t == ContentType.TEXT and getattr(p, "text", None):
                self._safe_print(f"{self.bot_prefix}{p.text}")
            elif t == ContentType.REFUSAL and getattr(p, "refusal", None):
                self._safe_print(f"{_RED}⚠ Refusal: {p.refusal}{_RESET}")
            elif t == ContentType.IMAGE and getattr(p, "image_url", None):
                self._safe_print(f"{_YELLOW}🖼  [Image: {p.image_url}]{_RESET}")
            elif t == ContentType.VIDEO and getattr(p, "video_url", None):
                self._safe_print(f"{_YELLOW}🎬 [Video: {p.video_url}]{_RESET}")
            elif t == ContentType.AUDIO and getattr(p, "data", None):
                self._safe_print(f"{_YELLOW}🔊 [Audio]{_RESET}")
            elif t == ContentType.FILE:
                url = (
                    getattr(p, "file_url", None)
                    or getattr(p, "file_id", None)
                    or ""
                )
                self._safe_print(f"{_YELLOW}📎 [File: {url}]{_RESET}")
        self._safe_print("")

    def _print_error(self, err: str) -> None:
        ts = _ts()
        self._safe_print(
            f"\n{_RED}{_BOLD}❌ [{ts}] Error{_RESET}\n{_RED}{err}{_RESET}\n",
        )

    def _parts_to_text(
        self,
        parts: List[OutgoingContentPart],
        meta: Optional[Dict[str, Any]] = None,
    ) -> str:
        """
        Merge parts to one body string (same logic as base send_content_parts).
        """
        text_parts: List[str] = []
        for p in parts:
            t = getattr(p, "type", None)
            if t == ContentType.TEXT and getattr(p, "text", None):
                text_parts.append(p.text or "")
            elif t == ContentType.REFUSAL and getattr(p, "refusal", None):
                text_parts.append(p.refusal or "")
        body = "\n".join(text_parts) if text_parts else ""
        prefix = (meta or {}).get("bot_prefix", self.bot_prefix) or ""
        if prefix and body:
            body = prefix + "  " + body
        return body

    # ── send (for proactive sends / cron) ───────────────────────────

    async def send(
        self,
        to_handle: str,
        text: str,
        meta: Optional[Dict[str, Any]] = None,
    ) -> None:
        """Send a text message — prints to stdout and pushes to frontend."""
        if not self.enabled:
            return
        ts = _ts()
        prefix = (meta or {}).get("bot_prefix", self.bot_prefix) or ""
        self._safe_print(
            f"\n{_GREEN}{_BOLD}🤖 [{ts}] Bot → {to_handle}{_RESET}\n"
            f"{prefix}{text}\n",
        )
        sid = (meta or {}).get("session_id")
        if sid and text.strip():
            await push_store_append(
                sid,
                text.strip(),
                source_kind=(meta or {}).get("push_source_kind"),
                source_id=(meta or {}).get("push_source_id"),
                source_name=(meta or {}).get("push_source_name"),
            )

    async def send_content_parts(
        self,
        to_handle: str,
        parts: List[OutgoingContentPart],
        meta: Optional[Dict[str, Any]] = None,
    ) -> None:
        """
        Send content parts — prints to stdout and pushes to frontend store.
        """
        self._print_parts(parts)
        sid = (meta or {}).get("session_id")
        if sid:
            body = self._parts_to_text(parts, meta)
            if body.strip():
                await push_store_append(
                    sid,
                    body.strip(),
                    source_kind=(meta or {}).get("push_source_kind"),
                    source_id=(meta or {}).get("push_source_id"),
                    source_name=(meta or {}).get("push_source_name"),
                )

    # ── lifecycle ───────────────────────────────────────────────────

    async def health_check(self) -> Dict[str, Any]:
        """Console channel is always healthy when enabled."""
        if not self.enabled:
            return {
                "channel": self.channel,
                "status": "disabled",
                "detail": "Console channel is disabled.",
            }
        return {
            "channel": self.channel,
            "status": "healthy",
            "detail": "Console channel is running.",
        }

    async def start(self) -> None:
        if not self.enabled:
            logger.debug("console channel disabled")
            return
        logger.info("Console channel started")

    async def stop(self) -> None:
        if not self.enabled:
            return
        logger.info("console channel stopped")
