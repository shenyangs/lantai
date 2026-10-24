# -*- coding: utf-8 -*-
"""Converters between legacy messages and Lantai runtime contracts."""

from __future__ import annotations

import json
from typing import Any, List, Union
from urllib.parse import unquote, urlparse
from uuid import uuid4

from lantai.app.channels.runtime_schema import (
    AgentResponse,
    AudioContent,
    DataContent,
    Error,
    FileContent,
    FunctionCall,
    FunctionCallOutput,
    ImageContent,
    Message,
    MessageType,
    RunStatus,
    TextContent,
    VideoContent,
)
from lantai.exceptions import AgentRuntimeErrorException

from ..events import RuntimeEvent, RuntimePresentation
from ..input import RuntimeRequestContext
from ..messages import ContentBlock, RuntimeMessage
from ..public_reasoning import (
    RUNTIME_PUBLIC_PROGRESS_ATTESTATION_FIELD,
    attest_runtime_public_progress,
    without_runtime_public_progress_claims,
)
from ...app.experience.presentation import tool_presentation_for_tool_name
from .legacy_message_components import (
    LegacyInMemoryMemory,
    LegacyMemoryMark,
    LegacyMsg,
    LegacyPlanNotebook,
    is_legacy_msg_like,
)


def _block_data(block: dict[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in block.items() if key != "type"}


def _legacy_text_block(text: str) -> dict[str, str]:
    return {"type": "text", "text": text}


_ATTACHMENT_WIRE_FIELDS = (
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
    "media_type",
    "file_size",
)


def _attachment_wire_kwargs(block: Any) -> dict[str, Any]:
    """Keep upload identity and role fields through Runtime/history conversion."""

    if isinstance(block, dict):
        get_value = block.get
    else:
        get_value = lambda key: getattr(block, key, None)
    result = {
        key: value
        for key in _ATTACHMENT_WIRE_FIELDS
        if (value := get_value(key)) is not None
    }
    filename = get_value("filename") or get_value("file_name")
    if filename is not None:
        result["filename"] = filename
    return result


def _legacy_tool_result_failed(block: dict[str, Any]) -> bool:
    status = str(block.get("status") or "").strip().lower()
    return block.get("ok") is False or "fail" in status or "error" in status


def _legacy_tool_error(block: dict[str, Any]) -> Error:
    raw_error = block.get("error")
    if isinstance(raw_error, dict):
        code = str(raw_error.get("code") or "tool_execution_failed")
        message = str(
            raw_error.get("message")
            or raw_error.get("error")
            or raw_error
        )
    else:
        code = "tool_execution_failed"
        message = str(raw_error or "工具执行失败。")
    return Error(code=code, message=message)


def _finalize_channel_message(message: Message) -> Message:
    if message.status == RunStatus.Failed:
        return message
    return message.completed()


def _content_block_from_legacy(block: dict[str, Any]) -> ContentBlock:
    block_type = str(block.get("type") or "text")
    if block_type == "text":
        return ContentBlock(type="text", text=str(block.get("text") or ""))
    if block_type == "thinking":
        return ContentBlock(
            type="thinking",
            text=str(block.get("thinking") or block.get("text") or ""),
            data=_block_data(block),
        )
    if block_type == "tool_use":
        return ContentBlock(
            type="tool_call",
            data={
                "id": block.get("id"),
                "name": block.get("name"),
                "arguments": block.get("input", {}),
            },
        )
    if block_type == "tool_result":
        return ContentBlock(
            type="tool_result",
            data={
                "id": block.get("id"),
                "name": block.get("name"),
                "output": block.get("output"),
                "ok": block.get("ok"),
                "status": block.get("status"),
                "error": block.get("error"),
            },
        )
    if block_type in {"image", "file"}:
        return ContentBlock(type=block_type, data=_block_data(block))
    if block_type in {"audio", "video"}:
        return ContentBlock(type="file", data={"media_type": block_type, **_block_data(block)})
    if block_type == "error":
        return ContentBlock(
            type="error",
            text=str(block.get("text") or block.get("error") or ""),
            data=_block_data(block),
        )
    return ContentBlock(
        type="text",
        text=str(block.get("text") or ""),
        data=_block_data(block),
        metadata={
            "legacy_type": block_type,
            "legacy_unknown_block": True,
        },
    )


def legacy_msg_to_runtime_messages(
    messages: LegacyMsg | list[LegacyMsg],
) -> list[RuntimeMessage]:
    """Convert legacy ``LegacyMsg`` objects to Lantai ``RuntimeMessage`` rows."""

    source_messages = [messages] if is_legacy_msg_like(messages) else list(messages)
    result: list[RuntimeMessage] = []
    for msg in source_messages:
        content = getattr(msg, "content", "")
        if isinstance(content, str):
            blocks = [ContentBlock(type="text", text=content)]
        elif isinstance(content, list):
            blocks = [
                _content_block_from_legacy(block)
                for block in content
                if isinstance(block, dict)
            ]
        else:
            blocks = [ContentBlock(type="text", text=str(content))]

        result.append(
            RuntimeMessage(
                id=getattr(msg, "id", None),
                role=getattr(msg, "role", None) or "assistant",
                name=getattr(msg, "name", None),
                created_at=getattr(msg, "timestamp", None),
                content=blocks,
                metadata={
                    "source": "legacy",
                    "original_name": getattr(msg, "name", None),
                    "metadata": getattr(msg, "metadata", None),
                },
            ),
        )
    return result


def _runtime_schema_message_to_runtime_message(message: Message) -> RuntimeMessage:
    """Preserve typed console content while entering the shared runtime.

    ``runtime_schema.Message`` is already a structured runtime contract.  Treating
    it as a generic ``get_text_content`` object silently discarded image and file
    parts before the legacy agent formatter could process them.
    """

    blocks: list[ContentBlock] = []
    for item in message.content or []:
        if isinstance(item, TextContent):
            blocks.append(ContentBlock(type="text", text=item.text or ""))
            continue

        if isinstance(item, ImageContent):
            data = _attachment_wire_kwargs(item)
            if item.image_url:
                data["source"] = {
                    "type": "url",
                    "url": item.image_url,
                }
            blocks.append(ContentBlock(type="image", data=data))
            continue

        if isinstance(item, FileContent):
            data = _attachment_wire_kwargs(item)
            source: dict[str, Any] | None = None
            if item.file_url:
                source = {"type": "url", "url": item.file_url}
            elif item.file_data:
                source = {"type": "base64", "data": item.file_data}

            if source is not None:
                data["source"] = source
            if item.file_id:
                data["file_id"] = item.file_id
            blocks.append(ContentBlock(type="file", data=data))
            continue

        if isinstance(item, AudioContent):
            data = _attachment_wire_kwargs(item)
            if item.data:
                data["source"] = {"type": "url", "url": item.data}
            if item.format:
                data["format"] = item.format
            blocks.append(
                ContentBlock(
                    type="file",
                    data=data,
                    metadata={
                        "legacy_type": "audio",
                        "legacy_unknown_block": True,
                    },
                ),
            )
            continue

        if isinstance(item, VideoContent):
            data = _attachment_wire_kwargs(item)
            if item.video_url:
                data["source"] = {"type": "url", "url": item.video_url}
            blocks.append(
                ContentBlock(
                    type="file",
                    data=data,
                    metadata={
                        "legacy_type": "video",
                        "legacy_unknown_block": True,
                    },
                ),
            )
            continue

        if isinstance(item, DataContent):
            blocks.append(
                ContentBlock(
                    type="text",
                    text=json.dumps(item.data or {}, ensure_ascii=False),
                ),
            )

    if not blocks:
        blocks.append(ContentBlock(type="text", text=""))

    raw_role = str(message.role or "user")
    role = raw_role if raw_role in {"system", "user", "assistant", "tool"} else "user"
    created_at = message.created_at
    return RuntimeMessage(
        id=message.id,
        role=role,
        created_at=str(created_at) if created_at is not None else None,
        content=blocks,
        metadata={
            **dict(message.metadata or {}),
            "source": "runtime_schema",
        },
    )


def _legacy_block_from_runtime(block: ContentBlock) -> dict[str, Any]:
    legacy_type = block.metadata.get("legacy_type")
    if isinstance(legacy_type, str) and block.metadata.get("legacy_unknown_block"):
        payload = dict(block.data)
        if block.text and "text" not in payload:
            payload["text"] = block.text
        return {"type": legacy_type, **payload}

    if block.type == "text":
        return {"type": "text", "text": block.text or ""}
    if block.type == "thinking":
        payload = dict(block.data)
        return {"type": "thinking", **payload, "thinking": block.text or ""}
    if block.type == "tool_call":
        return {
            "type": "tool_use",
            "id": block.data.get("id"),
            "name": block.data.get("name"),
            "input": block.data.get("arguments", {}),
        }
    if block.type == "tool_result":
        return {
            "type": "tool_result",
            "id": block.data.get("id"),
            "name": block.data.get("name"),
            "output": block.data.get("output"),
        }
    if block.type in {"image", "file"}:
        return {"type": block.type, **block.data}
    if block.type == "error":
        payload = dict(block.data)
        return {
            "type": "error",
            **payload,
            "text": block.text or block.data.get("error") or "",
        }
    return {"type": "text", "text": block.text or json.dumps(block.data, ensure_ascii=False)}


def runtime_message_to_legacy_msg(message: RuntimeMessage) -> LegacyMsg:
    """Convert one Lantai ``RuntimeMessage`` to a legacy ``LegacyMsg``."""

    if (
        len(message.content) == 1
        and message.content[0].type == "text"
        and not message.content[0].metadata.get("legacy_unknown_block")
    ):
        content: str | list[dict[str, Any]] = message.content[0].text or ""
    else:
        content = [_legacy_block_from_runtime(block) for block in message.content]
    msg = LegacyMsg(
        name=message.name or message.role,
        content=content,
        role=message.role,
        metadata=message.metadata,
        timestamp=message.created_at,
    )
    if message.id:
        msg.id = message.id
    return msg


def runtime_messages_to_legacy_msgs(
    messages: list[RuntimeMessage],
) -> list[LegacyMsg]:
    """Convert Lantai runtime messages into legacy ``LegacyMsg`` objects."""

    return [runtime_message_to_legacy_msg(message) for message in messages]


def legacy_assistant_text_msg(text: str, *, name: str = "Friday") -> LegacyMsg:
    """Build a legacy assistant text message for compatibility streams."""

    return LegacyMsg(
        name=name,
        role="assistant",
        content=[_legacy_text_block(text)],
    )


def legacy_user_text_msg(text: str, *, name: str = "user") -> LegacyMsg:
    """Build a legacy user text message for compatibility entry points."""

    return LegacyMsg(name=name, role="user", content=text)


def legacy_message(
    *,
    name: str,
    role: str,
    content: Any,
    metadata: dict[str, Any] | None = None,
) -> LegacyMsg:
    """Build a legacy message while isolating old message construction."""

    return LegacyMsg(name=name, role=role, content=content, metadata=metadata)


def legacy_message_from_dict(data: dict[str, Any]) -> LegacyMsg:
    """Rehydrate a legacy message from its persisted dictionary shape."""

    return LegacyMsg.from_dict(data)


def legacy_message_to_dict(message: Any) -> dict[str, Any]:
    """Return the persisted dictionary shape for a legacy message."""

    return message.to_dict()


def legacy_rewrite_last_message_text(messages: list[Any], new_text: str) -> None:
    """Rewrite the text content of the last legacy message in-place."""

    if not messages:
        return
    last = messages[-1]
    content = getattr(last, "content", None)
    if isinstance(content, list):
        for index, block in enumerate(content):
            if isinstance(block, dict) and block.get("type") == "text":
                content[index] = _legacy_text_block(new_text)
                return
        content.insert(
            0,
            _legacy_text_block(new_text),
        )
    elif isinstance(content, str):
        last.content = new_text


def legacy_query_messages_to_runtime_messages(
    messages: list[Any],
) -> list[RuntimeMessage]:
    """Convert current query messages into Lantai runtime messages."""

    runtime_messages: list[RuntimeMessage] = []
    for message in messages or []:
        if isinstance(message, Message):
            runtime_messages.append(
                _runtime_schema_message_to_runtime_message(message),
            )
            continue
        if is_legacy_msg_like(message):
            runtime_messages.extend(legacy_msg_to_runtime_messages(message))
            continue
        get_text = getattr(message, "get_text_content", None)
        if callable(get_text):
            role = getattr(message, "role", "user") or "user"
            runtime_messages.append(
                RuntimeMessage.text(
                    role,
                    str(get_text() or ""),
                    name=getattr(message, "name", None),
                ),
            )
            continue
        runtime_messages.append(RuntimeMessage.text("user", str(message)))
    return runtime_messages


def legacy_empty_plan_notebook_state() -> dict[str, Any]:
    """Return an empty legacy plan notebook state."""

    return LegacyPlanNotebook().state_dict()


def legacy_create_plan_notebook(plan_to_hint: Any | None = None) -> Any:
    """Create a legacy plan notebook for migration-period plan mode."""

    kwargs: dict[str, Any] = {}
    if plan_to_hint is not None:
        kwargs["plan_to_hint"] = plan_to_hint
    return LegacyPlanNotebook(**kwargs)


def is_legacy_app_base_exception(exc: BaseException) -> bool:
    """True when *exc* is a legacy runtime application exception."""

    from lantai.exceptions import AppBaseException

    return isinstance(exc, AppBaseException)


def legacy_task_cancelled_exception(message: str) -> Exception:
    """Build the legacy runner cancellation exception."""

    from lantai.exceptions import AgentException

    return AgentException(message)


def legacy_configuration_exception(message: str) -> Exception:
    """Build the legacy runtime configuration exception."""

    from lantai.exceptions import ConfigurationException

    return ConfigurationException(config_key="session.key", message=message)


def _is_local_file_url(url: str) -> bool:
    """True if url is a local file reference."""
    if not url or not isinstance(url, str):
        return False
    s = url.strip()
    if not s:
        return False
    lower = s.lower()
    if lower.startswith(("http://", "https://", "data:")):
        return False
    return (
        lower.startswith("file:")
        or (s.startswith("/") and not s.startswith("//"))
        or (len(s) >= 2 and s[1] == ":" and s[0].isalpha())
    )


def _abspath_from_url(url: str) -> str:
    s = url.strip()
    if s.lower().startswith("file:"):
        s = s[5:]
    s = "/" + s.lstrip("/")
    return unquote(s)


def _resolve_content_url(url: str) -> str:
    if not isinstance(url, str):
        return url
    if not _is_local_file_url(url):
        return url
    return _abspath_from_url(url)


def _build_media_message_from_block(
    block: dict,
    role: str,
    metadata: dict,
    created_at: str | None,
) -> Message:
    output = block.get("output")
    media_message = None
    if isinstance(output, list):
        media_items = [
            item
            for item in output
            if isinstance(item, dict)
            and item.get("type") in ("image", "audio", "video", "file")
        ]
        if media_items:
            media_message = Message(
                type=MessageType.MESSAGE,
                role=role,
                created_at=created_at,
            )
            media_message.metadata = metadata

            for item in media_items:
                itype = item.get("type")

                if itype == "image":
                    kwargs = _attachment_wire_kwargs(item)
                    source = item.get("source")
                    if isinstance(item.get("image_url"), str):
                        kwargs["image_url"] = _resolve_content_url(
                            item["image_url"],
                        )
                    elif (
                        isinstance(source, dict)
                        and source.get("type") == "url"
                    ):
                        kwargs["image_url"] = _resolve_content_url(
                            source.get("url", ""),
                        )
                    elif (
                        isinstance(source, dict)
                        and source.get("type") == "base64"
                    ):
                        media_type = source.get(
                            "media_type",
                            "image/jpeg",
                        )
                        base64_data = source.get("data", "")
                        kwargs[
                            "image_url"
                        ] = f"data:{media_type};base64,{base64_data}"
                    media_message.add_content(
                        new_content=ImageContent(
                            delta=False,
                            index=None,
                            **kwargs,
                        ),
                    )

                elif itype == "audio":
                    kwargs = _attachment_wire_kwargs(item)
                    source = item.get("source")
                    if isinstance(item.get("data"), str):
                        kwargs["data"] = _resolve_content_url(item["data"])
                        kwargs["format"] = item.get("format")
                    elif (
                        isinstance(source, dict)
                        and source.get("type") == "url"
                    ):
                        url = _resolve_content_url(
                            source.get("url", ""),
                        )
                        kwargs["data"] = url
                        try:
                            kwargs["format"] = urlparse(
                                url,
                            ).path.split(
                                ".",
                            )[-1]
                        except (
                            AttributeError,
                            IndexError,
                            ValueError,
                        ):
                            kwargs["format"] = None
                    elif (
                        isinstance(source, dict)
                        and source.get("type") == "base64"
                    ):
                        media_type = source.get("media_type")
                        base64_data = source.get("data", "")
                        kwargs[
                            "data"
                        ] = f"data:{media_type};base64,{base64_data}"
                        kwargs["format"] = media_type
                    media_message.add_content(
                        new_content=AudioContent(
                            delta=False,
                            index=None,
                            **kwargs,
                        ),
                    )

                elif itype == "video":
                    kwargs = _attachment_wire_kwargs(item)
                    source = item.get("source")
                    if isinstance(item.get("video_url"), str):
                        kwargs["video_url"] = _resolve_content_url(
                            item["video_url"],
                        )
                    elif (
                        isinstance(source, dict)
                        and source.get("type") == "url"
                    ):
                        kwargs["video_url"] = _resolve_content_url(
                            source.get("url", ""),
                        )
                    elif (
                        isinstance(source, dict)
                        and source.get("type") == "base64"
                    ):
                        media_type = source.get(
                            "media_type",
                            "video/mp4",
                        )
                        base64_data = source.get("data", "")
                        kwargs[
                            "video_url"
                        ] = f"data:{media_type};base64,{base64_data}"
                    media_message.add_content(
                        new_content=VideoContent(
                            delta=False,
                            index=None,
                            **kwargs,
                        ),
                    )

                elif itype == "file":
                    kwargs = _attachment_wire_kwargs(item)
                    source = item.get("source")
                    if isinstance(item.get("file_url"), str):
                        kwargs["file_url"] = _resolve_content_url(
                            item["file_url"],
                        )
                    elif (
                        isinstance(source, dict)
                        and source.get("type") == "url"
                    ):
                        kwargs["file_url"] = _resolve_content_url(
                            source.get("url", ""),
                        )
                    elif (
                        isinstance(source, dict)
                        and source.get("type") == "base64"
                    ):
                        media_type = source.get(
                            "media_type",
                            "application/octet-stream",
                        )
                        base64_data = source.get("data", "")
                        kwargs[
                            "file_url"
                        ] = f"data:{media_type};base64,{base64_data}"
                    elif isinstance(source, str):
                        kwargs["file_url"] = _resolve_content_url(
                            source,
                        )
                    media_message.add_content(
                        new_content=FileContent(
                            delta=False,
                            index=None,
                            **kwargs,
                        ),
                    )
    return media_message


def _legacy_tool_presentation_payload(
    block: dict[str, Any],
    message_metadata: Any,
) -> dict[str, Any]:
    """Return bounded context for contextual tool presentation decisions."""

    explicit = block.get("presentation")
    if isinstance(explicit, dict):
        return {"presentation": explicit}

    result: dict[str, Any] = {}
    queue = [message_metadata]
    seen: set[int] = set()
    while queue:
        candidate = queue.pop(0)
        if not isinstance(candidate, dict) or id(candidate) in seen:
            continue
        seen.add(id(candidate))
        for key in (
            "agent_id",
            "presentation_context",
            "skill_exposure",
            "skill_invocation_origin",
        ):
            value = candidate.get(key)
            if value is not None and key not in result:
                result[key] = value
        for key in ("metadata", "legacy_request_context"):
            nested = candidate.get(key)
            if isinstance(nested, dict):
                queue.append(nested)
    return result


def _presentation_for_legacy_tool_block(
    block: dict[str, Any],
    message_metadata: Any,
    tool_name: str,
) -> dict[str, Any]:
    context = _legacy_tool_presentation_payload(block, message_metadata)
    explicit = context.pop("presentation", None)
    if isinstance(explicit, dict):
        return explicit
    return tool_presentation_for_tool_name(
        tool_name,
        payload=context,
        summary=tool_name or "工具",
    ).model_dump(mode="json")


def legacy_msg_to_chat_messages(
    messages: Union[LegacyMsg, List[LegacyMsg]],
) -> List[Message]:
    """Convert legacy ``LegacyMsg`` rows into current Chat API messages."""

    if is_legacy_msg_like(messages):
        msgs = [messages]
    elif isinstance(messages, list):
        msgs = messages
    else:
        raise AgentRuntimeErrorException(
            code="INVALID_MESSAGE_TYPE",
            message=(
                f"Expected LegacyMsg or list[LegacyMsg], got {type(messages).__name__}"
            ),
        )

    results: List[Message] = []

    for msg in msgs:
        role = msg.role or "assistant"
        created_at = getattr(msg, "timestamp", None)
        metadata = {
            "original_id": msg.id,
            "original_name": msg.name,
            "metadata": msg.metadata,
        }

        if isinstance(msg.content, str):
            message = Message(
                type=MessageType.MESSAGE,
                role=role,
                created_at=created_at,
            )
            message.metadata = metadata
            text_content = TextContent(
                delta=False,
                index=None,
                text=msg.content,
            )
            message.add_content(new_content=text_content)
            results.append(message)
            continue

        current_message = None
        current_type = None

        for block in msg.content:
            if isinstance(block, dict):
                btype = block.get("type", "text")
            else:
                continue

            if btype == "text":
                if current_type != MessageType.MESSAGE:
                    if current_message:
                        results.append(_finalize_channel_message(current_message))
                    current_message = Message(
                        type=MessageType.MESSAGE,
                        role=role,
                        created_at=created_at,
                    )
                    current_message.metadata = metadata
                    current_type = MessageType.MESSAGE

                text_content = TextContent(
                    delta=False,
                    index=None,
                    text=block.get("text", ""),
                )
                current_message.add_content(new_content=text_content)

            elif btype == "thinking":
                if current_type != MessageType.REASONING:
                    if current_message:
                        results.append(_finalize_channel_message(current_message))
                    current_message = Message(
                        type=MessageType.REASONING,
                        role=role,
                        created_at=created_at,
                    )
                    current_message.metadata = metadata
                    current_type = MessageType.REASONING

                text_content = TextContent(
                    delta=False,
                    index=None,
                    text=block.get("thinking", ""),
                )
                current_message.add_content(new_content=text_content)

            elif btype == "tool_use":
                if current_message:
                    results.append(_finalize_channel_message(current_message))

                current_message = Message(
                    type=MessageType.PLUGIN_CALL,
                    role=role,
                    created_at=created_at,
                )
                current_message.metadata = metadata
                current_type = MessageType.PLUGIN_CALL

                if isinstance(block.get("input"), (dict, list)):
                    arguments = json.dumps(
                        block.get("input"),
                        ensure_ascii=False,
                    )
                else:
                    arguments = block.get("input")

                call_data = FunctionCall(
                    call_id=block.get("id"),
                    name=block.get("name"),
                    arguments=arguments,
                ).model_dump()
                call_data["presentation"] = _presentation_for_legacy_tool_block(
                    block,
                    msg.metadata,
                    str(block.get("name") or ""),
                )

                data_content = DataContent(
                    delta=False,
                    index=None,
                    data=call_data,
                )
                current_message.add_content(new_content=data_content)

            elif btype == "tool_result":
                if current_message:
                    results.append(_finalize_channel_message(current_message))

                current_message = Message(
                    type=MessageType.PLUGIN_CALL_OUTPUT,
                    role=role,
                    created_at=created_at,
                )
                current_message.metadata = metadata
                current_type = MessageType.PLUGIN_CALL_OUTPUT

                if isinstance(block.get("output"), (dict, list)):
                    output = json.dumps(
                        block.get("output"),
                        ensure_ascii=False,
                    )
                else:
                    output = block.get("output")

                output_data = FunctionCallOutput(
                    call_id=block.get("id"),
                    name=block.get("name"),
                    output=output,
                ).model_dump(exclude_none=True)
                output_data.update(
                    {
                        "ok": block.get("ok"),
                        "status": block.get("status"),
                        "error": block.get("error"),
                    }
                )
                output_data["presentation"] = _presentation_for_legacy_tool_block(
                    block,
                    msg.metadata,
                    str(block.get("name") or ""),
                )

                data_content = DataContent(
                    delta=False,
                    index=None,
                    data=output_data,
                )
                current_message.add_content(new_content=data_content)
                if _legacy_tool_result_failed(block):
                    current_message.failed(_legacy_tool_error(block))

                media_message = _build_media_message_from_block(
                    block,
                    role,
                    metadata,
                    created_at,
                )
                if media_message:
                    results.append(media_message)

            elif btype == "image":
                if current_type != MessageType.MESSAGE:
                    if current_message:
                        results.append(_finalize_channel_message(current_message))
                    current_message = Message(
                        type=MessageType.MESSAGE,
                        role=role,
                        created_at=created_at,
                    )
                    current_message.metadata = metadata
                    current_type = MessageType.MESSAGE

                kwargs = _attachment_wire_kwargs(block)
                if isinstance(block.get("image_url"), str):
                    kwargs["image_url"] = _resolve_content_url(
                        block["image_url"],
                    )
                elif (
                    isinstance(block.get("source"), dict)
                    and block.get("source", {}).get("type") == "url"
                ):
                    url = block.get("source", {}).get("url")
                    url = _resolve_content_url(url)
                    kwargs["image_url"] = url

                elif (
                    isinstance(block.get("source"), dict)
                    and block.get("source").get("type") == "base64"
                ):
                    media_type = block.get("source", {}).get(
                        "media_type",
                        "image/jpeg",
                    )
                    base64_data = block.get("source", {}).get("data", "")
                    url = f"data:{media_type};base64,{base64_data}"
                    kwargs["image_url"] = url

                image_content = ImageContent(
                    delta=False,
                    index=None,
                    **kwargs,
                )
                current_message.add_content(new_content=image_content)

            elif btype == "audio":
                if current_type != MessageType.MESSAGE:
                    if current_message:
                        results.append(_finalize_channel_message(current_message))
                    current_message = Message(
                        type=MessageType.MESSAGE,
                        role=role,
                        created_at=created_at,
                    )
                    current_message.metadata = metadata
                    current_type = MessageType.MESSAGE

                kwargs = _attachment_wire_kwargs(block)
                if isinstance(block.get("data"), str):
                    url = _resolve_content_url(block["data"])
                    kwargs["data"] = url
                    kwargs["format"] = block.get("format")
                elif (
                    isinstance(block.get("source"), dict)
                    and block.get("source", {}).get("type") == "url"
                ):
                    url = block.get("source", {}).get("url")
                    url = _resolve_content_url(url)
                    kwargs["data"] = url
                    try:
                        kwargs["format"] = urlparse(url).path.split(".")[-1]
                    except (AttributeError, IndexError, ValueError):
                        kwargs["format"] = None

                elif (
                    isinstance(block.get("source"), dict)
                    and block.get("source").get("type") == "base64"
                ):
                    media_type = block.get("source", {}).get("media_type")
                    base64_data = block.get("source", {}).get("data", "")
                    url = f"data:{media_type};base64,{base64_data}"
                    kwargs["data"] = url
                    kwargs["format"] = media_type

                audio_content = AudioContent(
                    delta=False,
                    index=None,
                    **kwargs,
                )
                current_message.add_content(new_content=audio_content)

            elif btype == "video":
                if current_type != MessageType.MESSAGE:
                    if current_message:
                        results.append(_finalize_channel_message(current_message))
                    current_message = Message(
                        type=MessageType.MESSAGE,
                        role=role,
                        created_at=created_at,
                    )
                    current_message.metadata = metadata
                    current_type = MessageType.MESSAGE

                kwargs = _attachment_wire_kwargs(block)
                if isinstance(block.get("video_url"), str):
                    kwargs["video_url"] = _resolve_content_url(
                        block["video_url"],
                    )
                elif (
                    isinstance(block.get("source"), dict)
                    and block.get("source", {}).get("type") == "url"
                ):
                    url = block.get("source", {}).get("url")
                    url = _resolve_content_url(url)
                    kwargs["video_url"] = url

                elif (
                    isinstance(block.get("source"), dict)
                    and block.get("source").get("type") == "base64"
                ):
                    media_type = block.get("source", {}).get(
                        "media_type",
                        "video/mp4",
                    )
                    base64_data = block.get("source", {}).get("data", "")
                    url = f"data:{media_type};base64,{base64_data}"
                    kwargs["video_url"] = url

                video_content = VideoContent(
                    delta=False,
                    index=None,
                    **kwargs,
                )
                current_message.add_content(new_content=video_content)

            elif btype == "file":
                if current_type != MessageType.MESSAGE:
                    if current_message:
                        results.append(_finalize_channel_message(current_message))
                    current_message = Message(
                        type=MessageType.MESSAGE,
                        role=role,
                        created_at=created_at,
                    )
                    current_message.metadata = metadata
                    current_type = MessageType.MESSAGE

                kwargs = _attachment_wire_kwargs(block)
                if isinstance(block.get("file_url"), str):
                    kwargs["file_url"] = _resolve_content_url(
                        block["file_url"],
                    )
                elif (
                    isinstance(block.get("source"), dict)
                    and block.get("source", {}).get("type") == "url"
                ):
                    url = block.get("source", {}).get("url")
                    url = _resolve_content_url(url)
                    kwargs["file_url"] = url

                elif (
                    isinstance(block.get("source"), dict)
                    and block.get("source").get("type") == "base64"
                ):
                    media_type = block.get("source", {}).get(
                        "media_type",
                        "application/octet-stream",
                    )
                    base64_data = block.get("source", {}).get("data", "")
                    url = f"data:{media_type};base64,{base64_data}"
                    kwargs["file_url"] = url
                elif isinstance(block.get("source"), str):
                    url = _resolve_content_url(block.get("source", ""))
                    kwargs["file_url"] = url

                file_content = FileContent(
                    delta=False,
                    index=None,
                    **kwargs,
                )
                current_message.add_content(new_content=file_content)

            else:
                if current_type != MessageType.MESSAGE:
                    if current_message:
                        results.append(_finalize_channel_message(current_message))
                    current_message = Message(
                        type=MessageType.MESSAGE,
                        role=role,
                        created_at=created_at,
                    )
                    current_message.metadata = metadata
                    current_type = MessageType.MESSAGE

                text_content = TextContent(
                    delta=False,
                    index=None,
                    text=str(block),
                )
                current_message.add_content(new_content=text_content)

        if current_message:
            results.append(_finalize_channel_message(current_message))

    return results


def _legacy_msg_stream_text_block(message: LegacyMsg) -> tuple[str, str] | None:
    content = message.content
    if isinstance(content, str):
        return ("text", content)
    if not isinstance(content, list):
        return None
    for block in content:
        if not isinstance(block, dict):
            continue
        block_type = str(block.get("type") or "text")
        if block_type == "text":
            return ("text", str(block.get("text") or ""))
        if block_type == "thinking":
            return ("thinking", str(block.get("thinking") or block.get("text") or ""))
    return None


def _stream_message_id_for_block(
    message_id: str,
    metadata: dict[str, Any],
    block_type: str,
) -> str:
    """Split native thinking/text streams that share one provider message id."""

    runtime_item_id = str(metadata.get("runtime_item_id") or "")
    if not runtime_item_id or message_id != runtime_item_id:
        return message_id
    suffix = "reasoning" if block_type == "thinking" else "message"
    return f"{message_id}:{suffix}"


def _stream_block_type_for_channel_message(message: Message) -> str:
    message_type = getattr(message, "type", None)
    if message_type == MessageType.REASONING or str(message_type) == "reasoning":
        return "thinking"
    return "text"


_TURN_TERMINAL_RUNTIME_EVENT_TYPES = {
    "turn.completed",
    "turn.failed",
    "turn.interrupted",
    "turn.stalled",
}


def _response_status_for_runtime_terminal(runtime_event_type: str) -> str:
    if runtime_event_type == "turn.completed":
        return RunStatus.Completed
    if runtime_event_type == "turn.failed":
        return RunStatus.Failed
    if runtime_event_type in {"turn.interrupted", "turn.stalled"}:
        return RunStatus.Incomplete
    return RunStatus.Completed


def legacy_stream_item_to_channel_events(
    item: tuple[LegacyMsg, bool] | Any,
    *,
    started_message_ids: set[str] | None = None,
    streamed_message_texts: dict[str, str] | None = None,
) -> list[Any]:
    """Convert legacy ``(msg, last)`` stream rows to channel schema events.

    Channel ``process`` handlers consume first-class ``Message``/``Content``/
    ``AgentResponse`` objects.  The old runner contract yielded ``(msg,last)``
    rows, so this adapter preserves streaming without leaking tuples into SSE.
    """

    if not isinstance(item, tuple) or len(item) != 2:
        return [item]

    legacy_msg, last = item
    if not is_legacy_msg_like(legacy_msg):
        return [item]

    started = started_message_ids if started_message_ids is not None else set()
    message_id = str(getattr(legacy_msg, "id", None) or uuid4())
    role = getattr(legacy_msg, "role", None) or "assistant"
    metadata = dict(getattr(legacy_msg, "metadata", None) or {})
    if metadata.get("message_type") == "tool_guard_approval":
        # The approval service creates the pending request before this legacy
        # compatibility item reaches Console. It is control-plane state, not
        # an assistant transcript message: rendering its Markdown here leaks
        # request IDs and action instructions into the conversation instead of
        # letting the client render an ApprovalCard from the pending request.
        return []
    runtime_event_type = str(metadata.get("runtime_event_type") or "")
    text_block = _legacy_msg_stream_text_block(legacy_msg)
    events: list[Any] = []

    if last and runtime_event_type in _TURN_TERMINAL_RUNTIME_EVENT_TYPES:
        response_status = _response_status_for_runtime_terminal(runtime_event_type)
        artifact_required = metadata.get("durable_artifact_required")
        response = AgentResponse(
            status=response_status,
            output=[],
            metadata=(
                {"durable_artifact_required": artifact_required}
                if isinstance(artifact_required, bool)
                else None
            ),
        )
        if response_status == RunStatus.Completed:
            response.completed()
        elif response_status == RunStatus.Failed:
            response.failed(
                Error(
                    code=str(
                        metadata.get("runtime_error_code")
                        or "runtime_turn_failed"
                    ),
                    message=str(
                        metadata.get("runtime_error_message")
                        or metadata.get("runtime_summary")
                        or "Runtime turn failed."
                    ),
                )
            )
        events.append(response)
        return events

    if runtime_event_type == "message.delta" and text_block is not None:
        block_type, text = text_block
        stream_message_id = _stream_message_id_for_block(
            message_id,
            metadata,
            block_type,
        )
        previous_text = (
            streamed_message_texts.get(stream_message_id, "")
            if streamed_message_texts is not None
            else ""
        )
        explicit_delta = metadata.get("runtime_delta_text")
        if isinstance(explicit_delta, str):
            delta_text = explicit_delta
            accumulated_text = f"{previous_text}{delta_text}"
        elif streamed_message_texts is not None:
            # Legacy partial messages are cumulative snapshots.  Convert them
            # once at the runtime boundary so channel consumers always receive
            # true append-only deltas.
            if text == previous_text:
                delta_text = ""
                accumulated_text = previous_text
            elif text.startswith(previous_text):
                delta_text = text[len(previous_text) :]
                accumulated_text = text
            else:
                delta_text = text
                accumulated_text = f"{previous_text}{text}"
        else:
            delta_text = text
            accumulated_text = text
        if streamed_message_texts is not None:
            streamed_message_texts[stream_message_id] = accumulated_text
        message_type = (
            MessageType.REASONING
            if block_type == "thinking"
            else MessageType.MESSAGE
        )
        if stream_message_id not in started:
            started.add(stream_message_id)
            events.append(
                Message(
                    id=stream_message_id,
                    type=message_type,
                    role=role,
                    created_at=getattr(legacy_msg, "timestamp", None),
                    status=RunStatus.InProgress,
                    content=[],
                    metadata=metadata,
                ),
            )
        if delta_text:
            events.append(
                TextContent(
                    status=RunStatus.InProgress,
                    delta=True,
                    msg_id=stream_message_id,
                    text=delta_text,
                ),
            )
        return events

    completed_messages = legacy_msg_to_chat_messages(legacy_msg)
    for message in completed_messages:
        block_type = _stream_block_type_for_channel_message(message)
        message.id = _stream_message_id_for_block(
            message_id,
            metadata,
            block_type,
        )
        message.metadata = {
            **dict(getattr(message, "metadata", None) or {}),
            **metadata,
        }
        message.completed()
    events.extend(completed_messages)

    if last:
        events.append(
            AgentResponse(
                status=RunStatus.Completed,
                output=completed_messages,
            ).completed(),
        )
    return events


async def legacy_memory_state_to_chat_messages(
    memory_state: dict[str, Any] | None,
) -> list[Any]:
    """Convert legacy memory state into chat API message rows.

    Chat history still stores memory in the legacy shape during the
    migration.  Keeping that restore logic here prevents API handlers from
    knowing which runtime wrote the state.
    """

    memory = LegacyInMemoryMemory()
    memory.load_state_dict(memory_state or {}, strict=False)
    memories = await memory.get_memory(prepend_summary=False)
    return legacy_msg_to_chat_messages(memories)


def runtime_event_to_legacy_stream_item(
    event: RuntimeEvent,
) -> tuple[LegacyMsg, bool] | None:
    """Convert RuntimeEvent rows back to legacy ``(LegacyMsg, last)`` stream items.

    This is a temporary bridge for APIs that still expose legacy ``LegacyMsg``
    streams while the execution path moves behind ``RuntimeRunner``.
    """

    def _message_payload_to_stream_msg() -> LegacyMsg | None:
        message_payload = event.payload.get("message")
        if not isinstance(message_payload, dict):
            return None
        runtime_message = RuntimeMessage.model_validate(message_payload)
        msg = runtime_message_to_legacy_msg(runtime_message)
        stable_id = (
            event.item_id
            or runtime_message.id
            or event.turn_id
            or event.id
        )
        if stable_id:
            msg.id = stable_id
        if isinstance(msg.content, str):
            msg.content = [_legacy_text_block(msg.content)]
        scrubbed_metadata = without_runtime_public_progress_claims(
            dict(getattr(msg, "metadata", None) or {})
        )
        metadata = (
            dict(scrubbed_metadata)
            if isinstance(scrubbed_metadata, dict)
            else {}
        )
        run_id = str(event.turn_id or event.payload.get("run_id") or "").strip()
        client_turn_id = str(event.payload.get("client_turn_id") or "").strip()
        metadata.update(
            {
                "runtime_event_id": event.id,
                "runtime_event_type": event.type,
                "runtime_turn_id": event.turn_id,
                "runtime_item_id": stable_id,
                "source_message_id": str(
                    event.payload.get("source_message_id") or stable_id or ""
                ),
                **({"run_id": run_id} if run_id else {}),
                **(
                    {"client_turn_id": client_turn_id}
                    if client_turn_id
                    else {}
                ),
            },
        )
        public_progress = event.payload.get("public_progress")
        stream_block = _legacy_msg_stream_text_block(msg)
        block_type, public_text = stream_block or ("", "")
        wire_message_id = (
            _stream_message_id_for_block(
                str(stable_id or ""),
                metadata,
                block_type,
            )
            if stable_id and block_type
            else ""
        )
        message_type = "reasoning" if block_type == "thinking" else "message"
        attestation = attest_runtime_public_progress(
            public_progress,
            run_id=run_id,
            wire_message_id=wire_message_id,
            message_type=message_type,
            text=public_text,
        )
        if attestation is not None:
            metadata["public_progress"] = dict(public_progress)
            metadata[RUNTIME_PUBLIC_PROGRESS_ATTESTATION_FIELD] = attestation
            metadata["runtime_presentation_only"] = True
        if event.type == "message.delta" and isinstance(
            event.payload.get("delta"),
            str,
        ):
            metadata["runtime_delta_text"] = event.payload["delta"]
        msg.metadata = metadata
        return msg

    if event.type == "message.delta":
        msg = _message_payload_to_stream_msg()
        if msg is None:
            return None
        return (msg, False)

    if event.type == "message.completed":
        msg = _message_payload_to_stream_msg()
        if msg is None:
            return None
        return (msg, False)

    if event.type in _TURN_TERMINAL_RUNTIME_EVENT_TYPES:
        runtime_error = event.payload.get("error")
        runtime_error_message = (
            str(runtime_error.get("message") or "")
            if isinstance(runtime_error, dict)
            else str(runtime_error or "")
        ).strip()
        return (
            LegacyMsg(
                name="Friday",
                role="assistant",
                content=[],
                metadata={
                    "runtime_event_id": event.id,
                    "runtime_event_type": event.type,
                    "runtime_turn_id": event.turn_id,
                    "runtime_status": event.status,
                    "runtime_summary": event.summary,
                    "runtime_error_code": event.payload.get("error_code"),
                    "runtime_error_message": (
                        runtime_error_message or event.summary
                    ),
                    "durable_artifact_required": event.payload.get(
                        "durable_artifact_required"
                    ),
                },
            ),
            True,
        )

    if event.type == "tool.call.started":
        return (
            LegacyMsg(
                name="Friday",
                role="assistant",
                content=[
                    {
                        "type": "tool_use",
                        "id": event.payload.get("id") or event.item_id,
                        "name": event.payload.get("name")
                        or event.payload.get("tool_name"),
                        "input": event.payload.get("arguments", {}),
                        **(
                            {
                                "presentation": event.presentation.model_dump(
                                    mode="json",
                                ),
                            }
                            if event.presentation is not None
                            else {}
                        ),
                    },
                ],
                metadata={
                    "runtime_event_id": event.id,
                    "runtime_event_type": event.type,
                    "runtime_turn_id": event.turn_id,
                    **(
                        {"client_turn_id": event.payload["client_turn_id"]}
                        if event.payload.get("client_turn_id")
                        else {}
                    ),
                },
            ),
            False,
        )

    if event.type == "tool.call.completed":
        return (
            LegacyMsg(
                name="Friday",
                role="assistant",
                content=[
                    {
                        "type": "tool_result",
                        "id": event.payload.get("id") or event.item_id,
                        "name": event.payload.get("name")
                        or event.payload.get("tool_name"),
                        "output": event.payload.get("output"),
                        "ok": event.payload.get("ok"),
                        "status": event.status,
                        "error": event.payload.get("error"),
                        **(
                            {
                                "presentation": event.presentation.model_dump(
                                    mode="json",
                                ),
                            }
                            if event.presentation is not None
                            else {}
                        ),
                    },
                ],
                metadata={
                    "runtime_event_id": event.id,
                    "runtime_event_type": event.type,
                    "runtime_turn_id": event.turn_id,
                    **(
                        {"client_turn_id": event.payload["client_turn_id"]}
                        if event.payload.get("client_turn_id")
                        else {}
                    ),
                },
            ),
            False,
        )

    if event.type == "approval.requested":
        tool_name = str(event.payload.get("tool_name") or "tool")
        request_id = str(event.payload.get("request_id") or event.item_id or "")
        guard = event.payload.get("guard")
        findings_count = 0
        findings_summary = str(event.payload.get("reason") or "")
        severity = "medium"
        if isinstance(guard, dict):
            findings_count = int(guard.get("findings_count") or 0)
            severity = str(guard.get("max_severity") or severity)
            findings = guard.get("findings")
            if isinstance(findings, list) and findings:
                lines = []
                for finding in findings[:3]:
                    if isinstance(finding, dict):
                        lines.append(str(finding.get("description") or ""))
                findings_summary = "\n".join(line for line in lines if line)

        text = (
            f"**工具需要审批**\n\n"
            f"- 工具：`{tool_name}`\n"
            f"- 风险：`{severity}`\n"
            f"- 请求：`{request_id}`\n\n"
            f"{findings_summary or event.summary}"
        )
        return (
            LegacyMsg(
                name="Friday",
                role="assistant",
                content=[{"type": "text", "text": text}],
                metadata={
                    "message_type": "tool_guard_approval",
                    "approval_request_id": request_id,
                    "session_id": event.session_id,
                    "tool_name": tool_name,
                    "severity": severity,
                    "findings_count": findings_count,
                    "findings_summary": findings_summary,
                    "tool_params": event.payload.get("arguments", {}),
                },
            ),
            False,
        )

    return None


_LEGACY_SAFE_IDENTITY_TOOLS = frozenset(
    {
        "chat_with_agent",
        "submit_to_agent",
        "collaborate_with_agents",
    }
)
_LEGACY_COLLABORATION_BATCH_TOOL = "collaborate_with_agents"


def _legacy_safe_collaboration_arguments(
    tool_name: str,
    arguments: Any,
) -> dict[str, Any]:
    """Return the bounded public identity projection for collaboration calls."""

    if tool_name not in _LEGACY_SAFE_IDENTITY_TOOLS or not isinstance(arguments, dict):
        return {}
    if tool_name != _LEGACY_COLLABORATION_BATCH_TOOL:
        to_agent = str(arguments.get("to_agent") or "").strip()
        return {"to_agent": to_agent} if to_agent else {}

    safe_tasks: list[dict[str, str]] = []
    tasks = arguments.get("tasks")
    if not isinstance(tasks, list):
        return {}
    for task in tasks:
        if not isinstance(task, dict):
            continue
        to_agent = str(task.get("to_agent") or "").strip()
        label = str(task.get("label") or "").strip()
        if not to_agent:
            continue
        safe_task = {"to_agent": to_agent}
        if label:
            safe_task["label"] = label
        safe_tasks.append(safe_task)
    return {"tasks": safe_tasks} if safe_tasks else {}


def _tool_payload(block: ContentBlock) -> dict[str, Any]:
    payload = dict(block.data)
    tool_name = str(payload.get("name") or "")
    payload["tool_name"] = tool_name
    if block.type != "tool_call":
        return payload

    raw_arguments = payload.pop("arguments", {})
    argument_keys = (
        sorted(str(key) for key in raw_arguments)
        if isinstance(raw_arguments, dict)
        else []
    )
    payload["argument_keys"] = argument_keys
    payload["arguments_redacted"] = bool(argument_keys)
    safe_arguments = _legacy_safe_collaboration_arguments(
        tool_name,
        raw_arguments,
    )
    if safe_arguments:
        payload["arguments"] = safe_arguments
    return payload


def _runtime_request_presentation_context(
    request: RuntimeRequestContext,
) -> dict[str, str]:
    metadata = request.metadata if isinstance(request.metadata, dict) else {}
    legacy_context = metadata.get("legacy_request_context")
    context = legacy_context if isinstance(legacy_context, dict) else {}
    result = {"agent_id": str(request.agent_id or "")}
    for key in (
        "presentation_context",
        "skill_exposure",
        "skill_invocation_origin",
    ):
        value = context.get(key)
        if isinstance(value, str) and value.strip():
            result[key] = value.strip()
    return {key: value for key, value in result.items() if value}


def _legacy_tool_presentation(
    tool_name: str,
    payload: dict[str, Any],
) -> RuntimePresentation:
    """Use the product sub-Agent surface without changing unrelated legacy tools."""

    product_presentation = tool_presentation_for_tool_name(
        tool_name,
        payload=payload,
    )
    if (
        product_presentation.surface == "subagent"
        or product_presentation.visibility == "hidden"
    ):
        return RuntimePresentation.model_validate(
            product_presentation.model_dump(mode="json"),
        )
    return RuntimePresentation(
        surface="tool",
        display_name=tool_name or "tool",
    )


def legacy_msg_to_runtime_events(
    msg: LegacyMsg,
    *,
    request: RuntimeRequestContext,
    run_id: str | None = None,
    seq_start: int = 0,
    last: bool = True,
) -> list[RuntimeEvent]:
    """Convert one printed legacy ``LegacyMsg`` into RuntimeEvent rows."""

    legacy_metadata = getattr(msg, "metadata", None)
    if (
        isinstance(legacy_metadata, dict)
        and legacy_metadata.get("message_type") == "tool_guard_approval"
    ):
        # A legacy ToolGuard prompt is a control-plane event, not an assistant
        # reply. Treating it as text makes the web client print request IDs and
        # command instructions in the transcript instead of showing its
        # ApprovalCard. Keep the legacy text available to non-web channels via
        # the reverse converter, but preserve the Runtime contract here.
        request_id = str(
            legacy_metadata.get("approval_request_id")
            or getattr(msg, "id", None)
            or ""
        ).strip()
        tool_name = str(legacy_metadata.get("tool_name") or "tool")
        severity = str(legacy_metadata.get("severity") or "medium")
        presentation_risk = (
            severity.lower() if severity.lower() in {"low", "medium", "high"} else "high"
        )
        findings_count = legacy_metadata.get("findings_count")
        payload: dict[str, Any] = {
            "request_id": request_id or None,
            "approval_request_id": request_id or None,
            "tool_name": tool_name,
            "severity": severity,
            "findings_count": (
                int(findings_count) if isinstance(findings_count, int) else None
            ),
            "arguments": legacy_metadata.get("tool_params", {}),
            "phase": "approval",
            "trace_title": f"审批：{tool_name}",
            "trace_detail": str(legacy_metadata.get("findings_summary") or ""),
            "root_session_id": request.root_session_id or request.session_id,
            "session_id": str(
                legacy_metadata.get("session_id") or request.session_id
            ),
            "agent_id": str(legacy_metadata.get("agent_id") or request.agent_id),
        }
        for key in (
            "policy_decision",
            "approval_options",
            "rememberable",
            "permission_scope",
            "source_run_id",
            "source_agent_name",
        ):
            value = legacy_metadata.get(key)
            if value is not None:
                payload[key] = value
        return [
            RuntimeEvent(
                id=str(uuid4()),
                seq=seq_start + 1,
                type="approval.requested",
                status="running",
                source="system",
                session_id=str(
                    legacy_metadata.get("session_id") or request.session_id
                ),
                turn_id=run_id or request.run_id,
                item_id=request_id or None,
                summary=f"工具 {tool_name} 等待审批",
                payload={key: value for key, value in payload.items() if value is not None},
                presentation=RuntimePresentation(
                    surface="approval",
                    risk_level=presentation_risk,
                    display_name=tool_name,
                    summary=str(legacy_metadata.get("findings_summary") or ""),
                ),
            )
        ]

    runtime_messages = legacy_msg_to_runtime_messages(msg)
    events: list[RuntimeEvent] = []
    seq = seq_start
    turn_id = run_id or request.run_id
    presentation_context = _runtime_request_presentation_context(request)
    for runtime_message in runtime_messages:
        source_message_id = runtime_message.id
        runtime_message.metadata = {
            **dict(runtime_message.metadata or {}),
            **presentation_context,
            **(
                {"source_message_id": source_message_id}
                if source_message_id
                else {}
            ),
        }
        message_payload = runtime_message.model_dump(mode="json")
        for block in runtime_message.content:
            if block.type == "tool_call":
                payload = _tool_payload(block)
                payload["metadata"] = {
                    **(
                        dict(payload.get("metadata") or {})
                        if isinstance(payload.get("metadata"), dict)
                        else {}
                    ),
                    **presentation_context,
                }
                seq += 1
                events.append(
                    RuntimeEvent(
                        id=str(uuid4()),
                        seq=seq,
                        type="tool.call.started",
                        status="running",
                        source="tool",
                        session_id=request.session_id,
                        turn_id=turn_id,
                        item_id=str(payload.get("id") or ""),
                        summary=str(payload.get("name") or "tool call"),
                        payload=payload,
                        presentation=_legacy_tool_presentation(
                            str(payload.get("name") or ""),
                            payload,
                        ),
                    ),
                )
                continue
            if block.type == "tool_result":
                payload = _tool_payload(block)
                payload["metadata"] = {
                    **(
                        dict(payload.get("metadata") or {})
                        if isinstance(payload.get("metadata"), dict)
                        else {}
                    ),
                    **presentation_context,
                }
                result_status = (
                    "failed"
                    if _legacy_tool_result_failed(payload)
                    else str(payload.get("status") or "completed")
                )
                seq += 1
                events.append(
                    RuntimeEvent(
                        id=str(uuid4()),
                        seq=seq,
                        type="tool.call.completed",
                        status=result_status,
                        source="tool",
                        session_id=request.session_id,
                        turn_id=turn_id,
                        item_id=str(payload.get("id") or ""),
                        summary=str(payload.get("name") or "tool result"),
                        payload=payload,
                        presentation=_legacy_tool_presentation(
                            str(payload.get("name") or ""),
                            payload,
                        ),
                    ),
                )
                continue

        if any(block.type not in {"tool_call", "tool_result"} for block in runtime_message.content):
            seq += 1
            events.append(
                RuntimeEvent(
                    id=str(uuid4()),
                    seq=seq,
                    type="message.completed" if last else "message.delta",
                    status="completed" if last else "running",
                    source=(
                        "user"
                        if runtime_message.role == "user"
                        else "assistant"
                        if runtime_message.role == "assistant"
                        else "system"
                    ),
                    session_id=request.session_id,
                    turn_id=turn_id,
                    item_id=runtime_message.id,
                    summary=runtime_message.text_content(),
                    payload={
                        "message": message_payload,
                        **(
                            {"source_message_id": source_message_id}
                            if source_message_id
                            else {}
                        ),
                    },
                ),
            )
    return events
