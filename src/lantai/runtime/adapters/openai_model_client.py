# -*- coding: utf-8 -*-
"""Native OpenAI-compatible Lantai model client."""

from __future__ import annotations

from lantai.utils.async_iterator import close_async_iterator

import json
from copy import deepcopy
from collections.abc import AsyncIterator
from contextlib import aclosing
from typing import Any
from urllib.parse import urlsplit, urlunsplit
from uuid import uuid4

from openai import AsyncOpenAI
from openai._streaming import SSEDecoder, ServerSentEvent

from ...local_models.tag_parser import parse_tool_calls_from_text, text_contains_tool_call_tag
from ...providers.model_endpoint import ProviderModelEndpoint
from ...providers.reasoning import (
    apply_current_reasoning_options,
    get_reasoning_capability,
    get_reasoning_options,
)
from ...providers.reasoning_continuity import (
    ReasoningTurnPlan,
    is_missing_reasoning_content_error,
    plan_reasoning_turn,
    project_disabled_tool_turn_reasoning_content,
    repair_missing_reasoning_content,
    resolve_reasoning_continuation_policy,
)
from ...providers.reasoning_output import (
    ReasoningContentParser,
    resolve_reasoning_output_profile,
)
from ...providers.request_profiles import is_kimi_coding_base_url
from ...providers.stream_normalizer import (
    ProviderStreamNormalizer,
    normalize_stream_chunk,
    provider_stream_profile,
)
from ...providers.tool_choice_compatibility import (
    ToolChoiceCall,
    ToolChoiceCompatibility,
)
from ...providers.openai_chat_model_compat import (
    _clean_kimi_coding_messages,
    _is_empty_message,
    _normalize_strict_text_message,
    _sanitize_tool_call,
)
from ...providers.openai_http import (
    OPENAI_SDK_MAX_RETRIES,
    create_openai_async_http_client,
    openai_endpoint_policy_receipt,
)
from ..model_normalizer import (
    media_payload_url,
    normalize_model_messages_for_provider,
    repair_incomplete_tool_transactions,
)
from ..attachment_context import attachment_context_text
from ..context_guard import prepare_model_request_for_context
from ..instruction_trust import (
    project_untrusted_tool_evidence,
    untrusted_tool_evidence_notice,
)
from ..models import (
    LantaiModelClient,
    ModelContentBlock,
    ModelMessage,
    ModelRequest,
    ModelResponse,
    ModelStreamEvent,
    ModelUsage,
)
from ..tools import ToolCall, ToolSpec

_STREAM_OPTIONS_UNSUPPORTED: set[tuple[str, str]] = set()
_REASONING_SPLIT_UNSUPPORTED: set[tuple[str, str, str]] = set()
_TOOL_CALL_TAG = "<tool_call>"
_TRANSPORT_ACTIVITY_FIELD = "x_lantai_transport_activity"
_PROTECTED_PAYLOAD_KEYS = {
    "model",
    "messages",
    "stream",
    "tools",
    "tool_choice",
    "stream_options",
}


class _LantaiObservableSSEDecoder(SSEDecoder):
    """Preserve observed SSE comments as non-semantic transport activity.

    The pinned OpenAI SDK intentionally drops comment frames before its async
    iterator yields.  Mapping an actually observed comment to a typed empty
    chunk lets the adapter distinguish transport bytes from decoded model
    output; it does not manufacture a timer heartbeat or claim model progress.
    """

    def decode(self, line: str) -> ServerSentEvent | None:
        if line.startswith(":"):
            return ServerSentEvent(
                data=json.dumps(
                    {
                        "id": "lantai-transport-activity",
                        "object": "chat.completion.chunk",
                        "created": 0,
                        "model": "",
                        "choices": [],
                        _TRANSPORT_ACTIVITY_FIELD: "sse_comment",
                    },
                    ensure_ascii=True,
                    separators=(",", ":"),
                )
            )
        event = super().decode(line)
        if event is not None and event.data.startswith("[DONE]"):
            return ServerSentEvent(
                data=json.dumps(
                    {
                        "id": "lantai-stream-terminal",
                        "object": "chat.completion.chunk",
                        "created": 0,
                        "model": "",
                        "choices": [],
                        _TRANSPORT_ACTIVITY_FIELD: "sse_done",
                    },
                    ensure_ascii=True,
                    separators=(",", ":"),
                )
            )
        return event


class _LantaiObservableAsyncOpenAI(AsyncOpenAI):
    def _make_sse_decoder(self) -> SSEDecoder:
        return _LantaiObservableSSEDecoder()


def _safe_base_url(raw_url: str) -> str:
    if not raw_url:
        return ""
    parsed = urlsplit(raw_url)
    if not parsed.scheme or not parsed.netloc:
        return raw_url.split("?", 1)[0].split("#", 1)[0]
    return urlunsplit((parsed.scheme, parsed.netloc, parsed.path, "", ""))


def _provider_metadata(
    endpoint: ProviderModelEndpoint,
    *,
    client_class: str,
    model_class: str | None = None,
) -> dict[str, str]:
    metadata = {
        "provider_id": endpoint.provider_id,
        "model": endpoint.model_id,
        "base_url": _safe_base_url(endpoint.base_url),
        "native_kind": endpoint.native_kind,
        "client_class": client_class,
    }
    if model_class:
        metadata["model_class"] = model_class
    return metadata


def _jsonish(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    try:
        return json.dumps(value, ensure_ascii=False)
    except (TypeError, ValueError):
        return str(value)


def _source_url(data: dict[str, Any]) -> str:
    source = data.get("source")
    if isinstance(source, dict):
        if source.get("type") == "base64":
            media_type = str(source.get("media_type") or "")
            payload = str(source.get("data") or "")
            return f"data:{media_type};base64,{payload}" if media_type else ""
        url = source.get("url")
        if isinstance(url, str):
            return url
    for key in ("url", "path"):
        value = data.get(key)
        if isinstance(value, str):
            return value
    return ""


def _media_block(block: ModelContentBlock) -> dict[str, Any] | None:
    if block.type == "file":
        return {
            "type": "text",
            "text": attachment_context_text(block.data),
        }
    url = media_payload_url(block.type, block.data)
    if not url:
        return None
    if block.type == "image":
        return {"type": "image_url", "image_url": {"url": url}}
    if block.type == "video":
        return {"type": "video_url", "video_url": {"url": url}}
    if block.type == "audio":
        return {"type": "input_audio", "input_audio": {"url": url}}
    return None


def _content_to_text(block: ModelContentBlock) -> str:
    if block.text:
        return block.text
    if block.type == "tool_call":
        return (
            f"Tool call requested: {block.data.get('name') or 'unknown_tool'}\n"
            f"Input: {_jsonish(block.data.get('arguments'))}"
        )
    if block.type == "tool_result":
        return (
            f"Tool result from {block.data.get('name') or 'unknown_tool'}:\n"
            f"{_jsonish(block.data.get('output'))}"
        )
    if block.type in {"image", "audio", "video", "file"}:
        url = _source_url(block.data)
        return f"[{block.type}: {url}]" if url else f"[{block.type}]"
    if block.data:
        return _jsonish(block.data)
    return ""


def _tool_result_output_to_text(output: Any) -> str:
    if isinstance(output, str):
        return output
    if not isinstance(output, list):
        return _jsonish(output)

    parts: list[str] = []
    for item in output:
        if not isinstance(item, dict):
            parts.append(str(item))
            continue
        item_type = str(item.get("type") or "text")
        if item_type == "text":
            parts.append(str(item.get("text") or ""))
        elif item_type == "file":
            url = _source_url(item)
            name = item.get("filename") or item.get("name") or url.rsplit("/", 1)[-1]
            parts.append(f"The returned file '{name or 'file'}' can be found at: {url}")
        elif item_type in {"image", "audio", "video"}:
            url = _source_url(item)
            parts.append(f"[{item_type}: {url}]" if url else f"[{item_type}]")
        else:
            parts.append(_jsonish(item))
    return "\n".join(part for part in parts if part)


def _tool_result_media_blocks(output: Any) -> list[ModelContentBlock]:
    if not isinstance(output, list):
        return []
    blocks: list[ModelContentBlock] = []
    for item in output:
        if not isinstance(item, dict):
            continue
        item_type = item.get("type")
        if item_type in {"image", "video"}:
            blocks.append(
                ModelContentBlock(
                    type=item_type,
                    data={key: value for key, value in item.items() if key != "type"},
                ),
            )
    return blocks


def _promoted_media_message(
    tool_name: str,
    media_blocks: list[ModelContentBlock],
) -> dict[str, Any] | None:
    if not media_blocks:
        return None
    content: list[dict[str, Any]] = [
        {
            "type": "text",
            "text": (
                untrusted_tool_evidence_notice(tool_name)
                + "\nThe following media came from this tool result:"
            ),
        },
    ]
    for block in media_blocks:
        url = _source_url(block.data)
        content.append(
            {
                "type": "text",
                "text": f"\n- The {block.type} from '{url}': ",
            },
        )
        formatted = _media_block(block)
        if formatted is not None:
            content.append(formatted)
    content.append({"type": "text", "text": "[END_LANTAI_TOOL_EVIDENCE]"})
    return {"role": "user", "content": content}


def _format_content_blocks(
    message: ModelMessage,
) -> tuple[Any, list[dict[str, Any]], str]:
    content_blocks: list[dict[str, Any]] = []
    text_parts: list[str] = []
    reasoning_parts: list[str] = []

    for block in message.content:
        if block.type == "text":
            if block.text:
                content_blocks.append({"type": "text", "text": block.text})
                text_parts.append(block.text)
        elif block.type == "thinking":
            thinking = block.text or block.data.get("thinking") or ""
            if thinking:
                reasoning_parts.append(str(thinking))
        elif block.type in {"image", "audio", "video", "file"}:
            formatted = _media_block(block)
            if formatted is not None:
                content_blocks.append(formatted)
            text = _content_to_text(block)
            if text:
                text_parts.append(text)
        elif block.type not in {"tool_call", "tool_result"}:
            text = _content_to_text(block)
            if text:
                content_blocks.append({"type": "text", "text": text})
                text_parts.append(text)

    if not content_blocks:
        content: Any = "\n".join(part for part in text_parts if part)
    elif all(block.get("type") == "text" for block in content_blocks):
        content = "\n".join(
            str(block.get("text") or "")
            for block in content_blocks
            if block.get("text")
        )
    else:
        content = content_blocks

    return content, content_blocks, "\n".join(reasoning_parts)


def format_openai_messages(
    messages: list[ModelMessage],
    *,
    normalize_content_to_text: bool = False,
    kimi_coding: bool = False,
    supports_multimodal: bool | None = None,
    target_family: str = "openai",
    reasoning_history_tag_pair: tuple[str, str] | None = None,
    repair_incomplete_tool_history: bool = False,
) -> list[dict[str, Any]]:
    """Format native model messages into OpenAI-compatible chat messages."""

    messages = normalize_model_messages_for_provider(
        messages,
        supports_multimodal=supports_multimodal,
        target_family=target_family,
    )
    if repair_incomplete_tool_history:
        messages = repair_incomplete_tool_transactions(messages)

    formatted: list[dict[str, Any]] = []
    deferred_tool_media: list[dict[str, Any]] = []

    def flush_deferred_tool_media() -> None:
        # OpenAI-compatible providers require every result for one assistant
        # tool-call batch to stay contiguous.  A promoted image is a new user
        # message, so inserting it between two tool results makes the second
        # result orphaned and some providers reject the continuation with 422.
        if deferred_tool_media:
            formatted.extend(deferred_tool_media)
            deferred_tool_media.clear()

    for message in messages:
        tool_result_blocks = [
            block for block in message.content if block.type == "tool_result"
        ]
        if message.role == "tool" or message.tool_call_id:
            media_promotions: list[ModelContentBlock] = []
            if tool_result_blocks:
                first_result = tool_result_blocks[0]
                content = _tool_result_output_to_text(
                    first_result.data.get("output"),
                )
                content = project_untrusted_tool_evidence(
                    str(first_result.data.get("name") or message.name or ""),
                    content,
                )
                # Tool-result media becomes a new user message.  Unknown
                # capability must fail closed here: otherwise a successful
                # view_image call can poison the next request to a text-only
                # OpenAI-compatible provider (observed as a provider 400 after
                # an otherwise completed deliverable).
                if not normalize_content_to_text and supports_multimodal is True:
                    media_promotions = _tool_result_media_blocks(
                        first_result.data.get("output"),
                    )
                tool_call_id = (
                    first_result.data.get("id")
                    or message.tool_call_id
                    or message.metadata.get("tool_call_id")
                    or message.id
                    or ""
                )
                tool_name = str(first_result.data.get("name") or message.name or "")
            else:
                content, _blocks, _reasoning = _format_content_blocks(message)
                tool_call_id = (
                    message.tool_call_id
                    or message.metadata.get("tool_call_id")
                    or message.id
                    or ""
                )
                tool_name = message.name or ""
            formatted.append(
                {
                    "role": "tool",
                    "tool_call_id": tool_call_id,
                    "content": content if isinstance(content, str) else _jsonish(content),
                },
            )
            promoted = _promoted_media_message(tool_name, media_promotions)
            if promoted is not None:
                deferred_tool_media.append(promoted)
            continue
        if tool_result_blocks and message.role != "assistant":
            for block in tool_result_blocks:
                output = block.data.get("output")
                tool_name = str(block.data.get("name") or message.name or "")
                formatted.append(
                    {
                        "role": "tool",
                        "tool_call_id": str(block.data.get("id") or ""),
                        "name": block.data.get("name"),
                        "content": project_untrusted_tool_evidence(
                            tool_name,
                            _tool_result_output_to_text(output),
                        ),
                    },
                )
                if not normalize_content_to_text and supports_multimodal is True:
                    promoted = _promoted_media_message(
                        tool_name,
                        _tool_result_media_blocks(output),
                    )
                    if promoted is not None:
                        deferred_tool_media.append(promoted)
            continue

        flush_deferred_tool_media()
        content, _blocks, reasoning = _format_content_blocks(message)
        row: dict[str, Any] = {
            "role": message.role,
            "content": content,
        }
        if message.name:
            row["name"] = message.name
        if reasoning and message.role == "assistant":
            if reasoning_history_tag_pair is not None:
                start_tag, end_tag = reasoning_history_tag_pair
                inline_reasoning = f"{start_tag}{reasoning}{end_tag}"
                if isinstance(content, str):
                    row["content"] = inline_reasoning + content
                elif isinstance(content, list):
                    row["content"] = [
                        {"type": "text", "text": inline_reasoning},
                        *content,
                    ]
                else:
                    row["content"] = inline_reasoning
            else:
                row["reasoning_content"] = reasoning

        tool_calls = []
        for block in message.content:
            if block.type != "tool_call":
                continue
            tool_call = {
                "id": block.data.get("id") or f"call_{uuid4().hex}",
                "type": "function",
                "function": {
                    "name": block.data.get("name") or "unknown_tool",
                    "arguments": _jsonish(block.data.get("arguments", {})),
                },
            }
            if target_family == "gemini" and block.data.get("extra_content"):
                tool_call["extra_content"] = deepcopy(
                    block.data["extra_content"],
                )
            tool_calls.append(tool_call)
        if tool_calls:
            row["tool_calls"] = tool_calls
            if not row.get("content"):
                row["content"] = None
        formatted.append(row)

    flush_deferred_tool_media()
    formatted = [msg for msg in formatted if not _is_empty_message(msg)]
    if normalize_content_to_text:
        formatted = [_normalize_strict_text_message(msg) for msg in formatted]
    if kimi_coding:
        formatted = _clean_kimi_coding_messages(formatted)
    return formatted


def format_openai_tools(tools: list[ToolSpec]) -> list[dict[str, Any]]:
    """Project runtime ToolSpec rows into OpenAI tool schemas."""

    result = []
    for spec in tools:
        if not spec.enabled:
            continue
        schema = spec.input_schema or {"type": "object", "properties": {}}
        result.append(
            {
                "type": "function",
                "function": {
                    "name": spec.name,
                    "description": spec.description,
                    "parameters": schema,
                },
            },
        )
    return result


def _usage_number(source: Any, *keys: str) -> int:
    mapping = source if isinstance(source, dict) else None
    for key in keys:
        raw = mapping.get(key) if mapping is not None else getattr(source, key, None)
        if raw is None:
            continue
        try:
            return int(raw)
        except (TypeError, ValueError):
            continue
    return 0


def _usage_optional_number(source: Any, *keys: str) -> int | None:
    mapping = source if isinstance(source, dict) else None
    for key in keys:
        raw = mapping.get(key) if mapping is not None else getattr(source, key, None)
        if raw is None:
            continue
        try:
            return int(raw)
        except (TypeError, ValueError):
            continue
    return None


def normalize_model_usage(usage: Any) -> ModelUsage | None:
    """Normalize provider usage objects into ModelUsage."""

    if usage is None:
        return None
    raw = usage.model_dump() if hasattr(usage, "model_dump") else usage
    if not isinstance(raw, dict):
        raw = dict(getattr(usage, "__dict__", {}))
    input_tokens = _usage_number(raw, "prompt_tokens", "input_tokens")
    output_tokens = _usage_number(raw, "completion_tokens", "output_tokens")
    total_tokens = _usage_number(raw, "total_tokens") or input_tokens + output_tokens
    cached_tokens = _usage_optional_number(
        raw,
        "cached_tokens",
        "cache_read_input_tokens",
        "cached_input_tokens",
    )
    if cached_tokens is None:
        details = (
            raw.get("prompt_tokens_details")
            or raw.get("input_tokens_details")
            or {}
        )
        cached_tokens = _usage_optional_number(
            details,
            "cached_tokens",
            "cache_read_input_tokens",
        )
    return ModelUsage(
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        total_tokens=total_tokens,
        cached_tokens=cached_tokens,
        raw=raw,
    )


def _safe_generation_kwargs(kwargs: dict[str, Any]) -> dict[str, Any]:
    return {
        key: value
        for key, value in kwargs.items()
        if key not in _PROTECTED_PAYLOAD_KEYS
    }


class _ToolCallAccumulator:
    def __init__(self) -> None:
        self._calls: dict[tuple[int, int, str], dict[str, Any]] = {}
        self._keys_by_position: dict[tuple[int, int], tuple[int, int, str]] = {}

    def update(self, raw_calls: list[Any], *, choice_index: int = 0) -> list[ToolCall]:
        deltas: list[ToolCall] = []
        for raw in raw_calls:
            sanitized = _sanitize_tool_call(raw)
            if sanitized is None:
                continue
            try:
                tool_index = int(getattr(sanitized, "index", 0) or 0)
            except (TypeError, ValueError):
                tool_index = 0
            key = self._key_for_call(
                choice_index,
                tool_index,
                getattr(sanitized, "id", None),
            )
            current = self._calls.setdefault(
                key,
                {
                    "id": "",
                    "name": "",
                    "arguments": "",
                    "extra_content": None,
                },
            )
            call_id = getattr(sanitized, "id", None)
            if call_id:
                current["id"] = str(call_id)
            function = getattr(sanitized, "function", None)
            name = getattr(function, "name", None)
            if name:
                current["name"] = str(name)
            args = getattr(function, "arguments", None)
            if args:
                current["arguments"] = str(current.get("arguments") or "") + str(args)
            extra_content = getattr(sanitized, "extra_content", None)
            if extra_content is None:
                model_extra = getattr(sanitized, "model_extra", None)
                if isinstance(model_extra, dict):
                    extra_content = model_extra.get("extra_content")
            if extra_content:
                current["extra_content"] = deepcopy(extra_content)
            tool_call = self._to_tool_call(key, partial=True)
            if tool_call is not None:
                deltas.append(tool_call)
        return deltas

    def completed(self) -> list[ToolCall]:
        completed = []
        for key in sorted(self._calls):
            tool_call = self._to_tool_call(key, partial=False)
            if tool_call is not None:
                completed.append(tool_call)
        return completed

    def _key_for_call(
        self,
        choice_index: int,
        tool_index: int,
        call_id: Any,
    ) -> tuple[int, int, str]:
        position = (choice_index, tool_index)
        normalized_id = str(call_id) if call_id else ""
        if normalized_id:
            explicit_key = (choice_index, tool_index, normalized_id)
            existing_key = self._keys_by_position.get(position)
            if existing_key is None:
                self._keys_by_position[position] = explicit_key
                return explicit_key

            existing = self._calls.get(existing_key, {})
            existing_id = str(existing.get("id") or "")
            if existing_key == explicit_key or existing_id in {"", normalized_id}:
                if existing_key != explicit_key and existing_key in self._calls:
                    self._calls[explicit_key] = self._calls.pop(existing_key)
                    self._keys_by_position[position] = explicit_key
                return explicit_key

            return explicit_key

        existing_key = self._keys_by_position.get(position)
        if existing_key is not None:
            return existing_key

        provisional_key = (
            choice_index,
            tool_index,
            f"provisional:{choice_index}:{tool_index}",
        )
        self._keys_by_position[position] = provisional_key
        return provisional_key

    def _to_tool_call(
        self,
        key: tuple[int, int, str],
        *,
        partial: bool,
    ) -> ToolCall | None:
        choice_index, tool_index, _call_key = key
        current = self._calls[key]
        raw_args = str(current.get("arguments") or "")
        try:
            parsed_arguments = json.loads(raw_args) if raw_args else {}
        except (TypeError, ValueError):
            if not partial:
                return None
            arguments = {"_partial": raw_args}
        else:
            if isinstance(parsed_arguments, dict):
                arguments = parsed_arguments
            elif partial:
                arguments = {"_partial": raw_args}
            else:
                return None
        fallback_id = (
            f"call_{tool_index}"
            if choice_index == 0
            else f"call_{choice_index}_{tool_index}"
        )
        return ToolCall(
            id=str(current.get("id") or fallback_id),
            name=str(current.get("name") or ""),
            arguments=arguments,
            metadata={
                "partial": partial,
                "raw_arguments": raw_args,
                "choice_index": choice_index,
                "tool_index": tool_index,
                **(
                    {"extra_content": deepcopy(current["extra_content"])}
                    if current.get("extra_content")
                    else {}
                ),
            },
        )


class _ToolCallTagFilter:
    """Hide inline tool-call tags from visible deltas while preserving calls."""

    def __init__(self, source: str) -> None:
        self._source = source
        self._buffer = ""
        self._raw_tool_text = ""
        self._suppressing = False

    def feed(self, delta: str) -> str:
        """Return user-visible text from *delta*.

        Local/OpenAI-compatible models sometimes emit XML tool calls as plain
        text. Once a tool-call tag begins, the legacy compat path
        trims everything from that tag onward and turns it into a tool call;
        the native path must do the same before deltas reach the UI.
        """

        if not delta:
            return ""
        if self._suppressing:
            self._raw_tool_text += delta
            return ""

        combined = self._buffer + delta
        self._buffer = ""
        tag_index = combined.find(_TOOL_CALL_TAG)
        if tag_index >= 0:
            self._suppressing = True
            self._raw_tool_text = combined[tag_index:]
            return combined[:tag_index].rstrip()

        hold = self._partial_tag_suffix_len(combined)
        if hold:
            self._buffer = combined[-hold:]
            return combined[:-hold]
        return combined

    def flush(self) -> str:
        """Return any buffered visible text when no tool tag completed."""

        if self._suppressing or not self._buffer:
            self._buffer = ""
            return ""
        visible = self._buffer
        self._buffer = ""
        return visible

    def completed(self) -> list[ToolCall]:
        """Return tool calls parsed from suppressed tag text."""

        if not self._raw_tool_text or not text_contains_tool_call_tag(
            self._raw_tool_text,
        ):
            return []
        parsed = parse_tool_calls_from_text(self._raw_tool_text)
        calls: list[ToolCall] = []
        for index, parsed_call in enumerate(parsed.tool_calls):
            calls.append(
                ToolCall(
                    id=f"tag_call_{self._source}_{index}",
                    name=parsed_call.name,
                    arguments=parsed_call.arguments,
                    metadata={
                        "raw_arguments": parsed_call.raw_arguments,
                        "source": self._source,
                    },
                ),
            )
        return calls

    @staticmethod
    def _partial_tag_suffix_len(text: str) -> int:
        max_suffix = min(len(text), len(_TOOL_CALL_TAG) - 1)
        for size in range(max_suffix, 0, -1):
            if _TOOL_CALL_TAG.startswith(text[-size:]):
                return size
        return 0


class OpenAICompatibleModelClient:
    """Native model client for OpenAI-compatible chat-completions APIs."""

    def __init__(self, endpoint: ProviderModelEndpoint) -> None:
        self.endpoint = endpoint
        self.provider_id = endpoint.provider_id
        self.model_name = endpoint.model_id
        self.model_transport_policy_receipt = openai_endpoint_policy_receipt(
            provider_id=endpoint.provider_id,
            model_id=endpoint.model_id,
            base_url=endpoint.base_url,
            protocol="openai_chat_completions",
        )
        kwargs: dict[str, Any] = {
            "base_url": endpoint.base_url,
            "api_key": endpoint.api_key,
            "http_client": create_openai_async_http_client(),
            "max_retries": OPENAI_SDK_MAX_RETRIES,
        }
        if endpoint.default_headers:
            kwargs["default_headers"] = endpoint.default_headers
        self._client = _LantaiObservableAsyncOpenAI(**kwargs)
        self._tool_choice_compatibility = ToolChoiceCompatibility()

    def _reasoning_turn_plan(self, request: ModelRequest) -> ReasoningTurnPlan:
        capability = get_reasoning_capability(
            self.endpoint.provider_id,
            request.model or self.endpoint.model_id,
            base_url=self.endpoint.base_url,
            chat_model=self.endpoint.chat_model,
        )
        options = get_reasoning_options()
        requested_enabled = (
            options.enabled
            if options is not None
            else capability.default_enabled
        )
        policy = resolve_reasoning_continuation_policy(
            self.endpoint.provider_id,
            request.model or self.endpoint.model_id,
            base_url=self.endpoint.base_url,
            chat_model=self.endpoint.chat_model,
        )
        return plan_reasoning_turn(
            capability=capability,
            policy=policy,
            requested_enabled=requested_enabled,
            tool_choice=request.tool_choice,
            has_tools=bool(request.tools),
            messages=request.messages,
        )

    def _payload(
        self,
        request: ModelRequest,
        *,
        include_usage: bool,
        include_reasoning_split: bool = True,
    ) -> dict[str, Any]:
        tools = format_openai_tools(request.tools)
        reasoning_output_profile = resolve_reasoning_output_profile(
            self.endpoint.provider_id,
            request.model or self.endpoint.model_id,
            base_url=self.endpoint.base_url,
            chat_model=self.endpoint.chat_model,
        )
        payload: dict[str, Any] = {
            "model": request.model or self.endpoint.model_id,
            "messages": format_openai_messages(
                request.messages,
                normalize_content_to_text=self.endpoint.normalize_content_to_text,
                kimi_coding=is_kimi_coding_base_url(self.endpoint.base_url),
                supports_multimodal=self.endpoint.supports_multimodal,
                target_family=self.endpoint.family,
                reasoning_history_tag_pair=(
                    reasoning_output_profile.history_tag_pair
                ),
                repair_incomplete_tool_history=True,
            ),
            "stream": True,
        }
        if tools:
            payload["tools"] = tools
        if request.tool_choice and request.tool_choice != "auto":
            payload["tool_choice"] = request.tool_choice
        if include_usage:
            payload["stream_options"] = {"include_usage": True}
        payload.update(_safe_generation_kwargs(self.endpoint.generate_kwargs))
        payload.update(_safe_generation_kwargs(request.generation))
        if is_kimi_coding_base_url(self.endpoint.base_url):
            session_id = str(request.metadata.get("session_id") or "").strip()
            if session_id:
                payload["prompt_cache_key"] = session_id
        if (
            include_reasoning_split
            and reasoning_output_profile.request_reasoning_split
        ):
            extra_body = payload.get("extra_body")
            merged_extra_body = (
                dict(extra_body)
                if isinstance(extra_body, dict)
                else {}
            )
            merged_extra_body["reasoning_split"] = True
            payload["extra_body"] = merged_extra_body
        payload = apply_current_reasoning_options(
            payload,
            provider_id=self.endpoint.provider_id,
            model_id=request.model or self.endpoint.model_id,
            base_url=self.endpoint.base_url,
            chat_model=self.endpoint.chat_model,
        )
        reasoning_plan = self._reasoning_turn_plan(request)
        if reasoning_plan.tool_choice_adjustment_reason:
            payload["tool_choice"] = reasoning_plan.effective_tool_choice
        if reasoning_plan.forced_disabled_reason:
            # Required-tool protocols and open tool transactions must use one
            # effective thinking mode.  Re-enable only after tools are fully
            # withdrawn (for example, the parent-agent synthesis round).
            extra_body = payload.get("extra_body")
            merged_extra_body = (
                dict(extra_body) if isinstance(extra_body, dict) else {}
            )
            merged_extra_body["thinking"] = {"type": "disabled"}
            payload["extra_body"] = merged_extra_body
            payload.pop("reasoning_effort", None)
        elif (
            reasoning_plan.effective_enabled
            and reasoning_plan.policy.protocol == "openai_reasoning_content"
        ):
            project_disabled_tool_turn_reasoning_content(
                request.messages,
                payload["messages"],
            )
        return payload

    async def _create_stream(self, request: ModelRequest) -> ToolChoiceCall:
        request = prepare_model_request_for_context(request, self.endpoint)
        cache_key = (self.endpoint.provider_id, self.endpoint.base_url)
        split_cache_key = (
            self.endpoint.provider_id,
            self.endpoint.base_url,
            request.model or self.endpoint.model_id,
        )
        include_usage = cache_key not in _STREAM_OPTIONS_UNSUPPORTED
        include_reasoning_split = (
            split_cache_key not in _REASONING_SPLIT_UNSUPPORTED
        )
        payload = self._payload(
            request,
            include_usage=include_usage,
            include_reasoning_split=include_reasoning_split,
        )
        reasoning_repair_attempted = False
        repaired_messages: list[dict[str, Any]] | None = None

        async def create(**wire_payload: Any) -> Any:
            nonlocal reasoning_repair_attempted, repaired_messages
            if repaired_messages is not None:
                wire_payload["messages"] = deepcopy(repaired_messages)
            if cache_key in _STREAM_OPTIONS_UNSUPPORTED:
                wire_payload.pop("stream_options", None)
            if split_cache_key in _REASONING_SPLIT_UNSUPPORTED:
                extra_body = dict(wire_payload.get("extra_body") or {})
                extra_body.pop("reasoning_split", None)
                if extra_body:
                    wire_payload["extra_body"] = extra_body
                else:
                    wire_payload.pop("extra_body", None)
            for _attempt in range(3):
                try:
                    return await self._client.chat.completions.create(**wire_payload)
                except Exception as exc:
                    error_text = str(exc).lower()
                    if (
                        not reasoning_repair_attempted
                        and is_missing_reasoning_content_error(exc)
                    ):
                        candidate = deepcopy(wire_payload.get("messages"))
                        if repair_missing_reasoning_content(candidate):
                            reasoning_repair_attempted = True
                            repaired_messages = candidate
                            return await self._client.chat.completions.create(
                                **{**wire_payload, "messages": repaired_messages},
                            )
                    if (
                        "stream_options" in wire_payload
                        and getattr(exc, "status_code", None) == 400
                        and "stream_options" in error_text
                    ):
                        _STREAM_OPTIONS_UNSUPPORTED.add(cache_key)
                        wire_payload.pop("stream_options", None)
                        continue
                    if (
                        "reasoning_split" in (wire_payload.get("extra_body") or {})
                        and getattr(exc, "status_code", None) == 400
                        and "reasoning_split" in error_text
                    ):
                        _REASONING_SPLIT_UNSUPPORTED.add(split_cache_key)
                        extra_body = dict(wire_payload["extra_body"])
                        extra_body.pop("reasoning_split", None)
                        wire_payload["extra_body"] = extra_body
                        continue
                    raise
            raise RuntimeError("model stream compatibility retries exhausted")

        return await self._tool_choice_compatibility.create(
            create,
            payload,
            provider_id=self.endpoint.provider_id,
            base_url=self.endpoint.base_url,
            allow_recovery=request.metadata.get("surface") != "model_compatibility_probe",
        )

    async def stream(
        self,
        request: ModelRequest,
    ) -> AsyncIterator[ModelStreamEvent]:
        message_id = request.metadata.get("message_id") or f"msg_{uuid4().hex}"
        reasoning_plan = self._reasoning_turn_plan(request)
        provider_call = await self._create_stream(request)
        stream_started = False
        try:
            yield ModelStreamEvent(
                kind="message_start",
                message_id=message_id,
                metadata={
                    "provider": _provider_metadata(
                        self.endpoint,
                        client_class=self.__class__.__name__,
                    ),
                },
            )
            accumulator = _ToolCallAccumulator()
            reasoning_content_parser = ReasoningContentParser(
                resolve_reasoning_output_profile(
                    self.endpoint.provider_id,
                    request.model or self.endpoint.model_id,
                    base_url=self.endpoint.base_url,
                    chat_model=self.endpoint.chat_model,
                ),
            )
            text_tag_filter = _ToolCallTagFilter("text")
            thinking_tag_filter = _ToolCallTagFilter("thinking")
            text_parts: list[str] = []
            thinking_parts: list[str] = []
            stream_profile = provider_stream_profile(
                provider_id=self.endpoint.provider_id,
                model_id=request.model or self.endpoint.model_id,
                family=self.endpoint.family,
            )
            provider_normalizer = ProviderStreamNormalizer(stream_profile)
            last_usage: ModelUsage | None = None
            finish_reason: str | None = None
            stream_terminal_observed = False

            async with aclosing(provider_call.iter_stream()) as stream:
                stream_started = True
                async for chunk in stream:
                    chunk_emitted_event = False
                    model_extra = getattr(chunk, "model_extra", None)
                    transport_activity = (
                        model_extra.get(_TRANSPORT_ACTIVITY_FIELD)
                        if isinstance(model_extra, dict)
                        else None
                    )
                    if transport_activity == "sse_comment":
                        yield ModelStreamEvent(
                            kind="provider_activity",
                            message_id=message_id,
                            metadata={
                                "provider_chunk": False,
                                "activity_scope": "transport_sse_comment",
                                "semantic_output": False,
                                "upstream_model_confirmed": False,
                            },
                        )
                        continue
                    if transport_activity == "sse_done":
                        stream_terminal_observed = True
                        continue
                    choices = getattr(chunk, "choices", None) or []
                    usage = normalize_model_usage(
                        getattr(chunk, "usage", None)
                    )
                    if usage is not None:
                        last_usage = usage
                    for choice_offset, choice in enumerate(choices):
                        raw_choice_index = getattr(
                            choice, "index", choice_offset
                        )
                        try:
                            choice_index = int(raw_choice_index)
                        except (TypeError, ValueError):
                            choice_index = choice_offset
                        delta = getattr(choice, "delta", None)
                        finish_reason = (
                            getattr(choice, "finish_reason", None)
                            or finish_reason
                        )
                        if getattr(choice, "finish_reason", None):
                            stream_terminal_observed = True
                        if delta is None:
                            continue
                        normalized = normalize_stream_chunk(
                            delta,
                            usage=getattr(chunk, "usage", None),
                            finish_reason=getattr(
                                choice, "finish_reason", None
                            ),
                            profile=stream_profile,
                        )
                        if normalized.thinking_delta:
                            thinking_delta = provider_normalizer.text_delta(
                                normalized.thinking_delta,
                                channel="thinking",
                                block_identity=str(choice_index),
                            )
                            visible_thinking = thinking_tag_filter.feed(
                                thinking_delta,
                            )
                            if visible_thinking:
                                thinking_parts.append(visible_thinking)
                                chunk_emitted_event = True
                                yield ModelStreamEvent(
                                    kind="thinking_delta",
                                    message_id=message_id,
                                    delta=visible_thinking,
                                    content_block=ModelContentBlock(
                                        type="thinking",
                                        text=visible_thinking,
                                    ),
                                )
                        if normalized.text_delta:
                            text_delta = provider_normalizer.text_delta(
                                normalized.text_delta,
                                channel="text",
                                block_identity=str(choice_index),
                            )
                            for segment in reasoning_content_parser.feed(
                                text_delta
                            ):
                                tag_filter = (
                                    thinking_tag_filter
                                    if segment.kind == "thinking"
                                    else text_tag_filter
                                )
                                visible = tag_filter.feed(segment.text)
                                if not visible:
                                    continue
                                if segment.kind == "thinking":
                                    thinking_parts.append(visible)
                                    chunk_emitted_event = True
                                    yield ModelStreamEvent(
                                        kind="thinking_delta",
                                        message_id=message_id,
                                        delta=visible,
                                        content_block=ModelContentBlock(
                                            type="thinking",
                                            text=visible,
                                        ),
                                    )
                                else:
                                    text_parts.append(visible)
                                    chunk_emitted_event = True
                                    yield ModelStreamEvent(
                                        kind="text_delta",
                                        message_id=message_id,
                                        delta=visible,
                                        content_block=ModelContentBlock(
                                            type="text",
                                            text=visible,
                                        ),
                                    )
                        for call in accumulator.update(
                            normalized.tool_call_delta,
                            choice_index=choice_index,
                        ):
                            chunk_emitted_event = True
                            yield ModelStreamEvent(
                                kind="tool_call_delta",
                                message_id=message_id,
                                tool_call=call,
                            )
                    if not chunk_emitted_event:
                        yield ModelStreamEvent(
                            kind="provider_activity",
                            message_id=message_id,
                            metadata={"provider_chunk": True},
                        )

            if not stream_terminal_observed:
                raise RuntimeError(
                    "Provider stream ended before a protocol terminal marker; "
                    "partial text and tool arguments were not committed as a "
                    "completed model response"
                )

            for segment in reasoning_content_parser.flush():
                tag_filter = (
                    thinking_tag_filter
                    if segment.kind == "thinking"
                    else text_tag_filter
                )
                visible = tag_filter.feed(segment.text)
                if not visible:
                    continue
                if segment.kind == "thinking":
                    thinking_parts.append(visible)
                    yield ModelStreamEvent(
                        kind="thinking_delta",
                        message_id=message_id,
                        delta=visible,
                        content_block=ModelContentBlock(
                            type="thinking",
                            text=visible,
                        ),
                    )
                else:
                    text_parts.append(visible)
                    yield ModelStreamEvent(
                        kind="text_delta",
                        message_id=message_id,
                        delta=visible,
                        content_block=ModelContentBlock(
                            type="text",
                            text=visible,
                        ),
                    )

            trailing_thinking = thinking_tag_filter.flush()
            if trailing_thinking:
                thinking_parts.append(trailing_thinking)
                yield ModelStreamEvent(
                    kind="thinking_delta",
                    message_id=message_id,
                    delta=trailing_thinking,
                    content_block=ModelContentBlock(
                        type="thinking",
                        text=trailing_thinking,
                    ),
                )
            trailing_text = text_tag_filter.flush()
            if trailing_text:
                text_parts.append(trailing_text)
                yield ModelStreamEvent(
                    kind="text_delta",
                    message_id=message_id,
                    delta=trailing_text,
                    content_block=ModelContentBlock(
                        type="text", text=trailing_text
                    ),
                )

            completed_tool_calls: list[ToolCall] = []
            completed_tool_call_ids: set[str] = set()

            def remember_completed_tool_call(call: ToolCall) -> None:
                if call.id in completed_tool_call_ids:
                    return
                completed_tool_call_ids.add(call.id)
                completed_tool_calls.append(call)

            for call in accumulator.completed():
                if call.name:
                    remember_completed_tool_call(call)
                    yield ModelStreamEvent(
                        kind="tool_call_completed",
                        message_id=message_id,
                        tool_call=call,
                    )

            for tag_filter in (thinking_tag_filter, text_tag_filter):
                for parsed_call in tag_filter.completed():
                    remember_completed_tool_call(parsed_call)
                    yield ModelStreamEvent(
                        kind="tool_call_completed",
                        message_id=message_id,
                        tool_call=parsed_call,
                    )

            content = []
            if thinking_parts:
                content.append(
                    ModelContentBlock(
                        type="thinking",
                        text="".join(thinking_parts),
                    ),
                )
            if text_parts:
                content.append(
                    ModelContentBlock(type="text", text="".join(text_parts))
                )
            for call in completed_tool_calls:
                content.append(
                    ModelContentBlock(
                        type="tool_call",
                        data={
                            "id": call.id,
                            "name": call.name,
                            "arguments": call.arguments,
                        },
                        metadata=dict(call.metadata),
                    ),
                )
            response = ModelResponse(
                message=ModelMessage(
                    id=message_id,
                    role="assistant",
                    content=content,
                    metadata=(
                        {
                            "reasoning_status": reasoning_plan.status_metadata(
                                returned=bool("".join(thinking_parts).strip()),
                            ),
                        }
                        if reasoning_plan.policy.protocol != "none"
                        else {}
                    ),
                ),
                usage=last_usage,
                finish_reason=finish_reason,
            )
            response.message.metadata.update(provider_call.metadata())
            yield ModelStreamEvent(
                kind="message_completed",
                message_id=message_id,
                response=response,
                usage=last_usage,
                finish_reason=finish_reason,
            )
        finally:
            if not stream_started:
                await close_async_iterator(provider_call.response)

    async def complete(self, request: ModelRequest) -> ModelResponse:
        last: ModelResponse | None = None
        async for event in self.stream(request):
            if event.response is not None:
                last = event.response
        if last is None:
            return ModelResponse(
                message=ModelMessage(id=f"msg_{uuid4().hex}", role="assistant"),
            )
        return last


__all__ = [
    "OpenAICompatibleModelClient",
    "format_openai_messages",
    "format_openai_tools",
    "normalize_model_usage",
]
