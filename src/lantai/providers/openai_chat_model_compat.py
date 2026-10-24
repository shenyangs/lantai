# -*- coding: utf-8 -*-
"""OpenAI chat model compatibility wrappers."""

from __future__ import annotations

import json
from datetime import datetime
from types import SimpleNamespace
from typing import Any, AsyncGenerator, Type

import openai
from pydantic import BaseModel

from lantai.local_models.tag_parser import (
    parse_tool_calls_from_text,
    text_contains_tool_call_tag,
)
from lantai.providers.openai_http import create_openai_async_http_client
from lantai.providers.reasoning import get_reasoning_capability
from lantai.providers.reasoning_continuity import (
    compatible_fixed_reasoning_tool_choice,
    resolve_reasoning_continuation_policy,
)
from lantai.providers.request_profiles import (
    KIMI_CODING_BASE_URL,
    is_kimi_coding_base_url,
)
from lantai.providers.stream_normalizer import (
    extract_text_delta,
    extract_reasoning_delta,
    normalize_stream_chunk,
    strip_reasoning_blocks,
)
from lantai.providers.tool_choice_compatibility import ToolChoiceCompatibility


def _clone_with_overrides(obj: Any, **overrides: Any) -> Any:
    """Clone a stream object into a mutable namespace with overrides."""
    data = dict(getattr(obj, "__dict__", {}))
    data.update(overrides)
    return SimpleNamespace(**data)


def _sanitize_tool_call(tool_call: Any) -> Any | None:
    """Normalize a tool call for parser safety, or drop it if unusable."""
    if not hasattr(tool_call, "index"):
        return None

    function = getattr(tool_call, "function", None)
    if function is None:
        return None

    has_name = hasattr(function, "name")
    has_arguments = hasattr(function, "arguments")

    raw_name = getattr(function, "name", "")
    if isinstance(raw_name, str):
        safe_name = raw_name
    elif raw_name is None:
        safe_name = ""
    else:
        safe_name = str(raw_name)

    raw_arguments = getattr(function, "arguments", "")
    if isinstance(raw_arguments, str):
        safe_arguments = raw_arguments
    elif raw_arguments is None:
        safe_arguments = ""
    else:
        try:
            safe_arguments = json.dumps(raw_arguments, ensure_ascii=False)
        except (TypeError, ValueError):
            safe_arguments = str(raw_arguments)

    if (
        has_name
        and has_arguments
        and isinstance(raw_name, str)
        and isinstance(
            raw_arguments,
            str,
        )
    ):
        return tool_call

    safe_function = SimpleNamespace(
        name=safe_name,
        arguments=safe_arguments,
    )
    return _clone_with_overrides(tool_call, function=safe_function)
def _sanitize_chunk(chunk: Any) -> Any:
    """Drop/normalize malformed tool-calls in a streaming chunk."""
    choices = getattr(chunk, "choices", None)
    if not choices:
        return chunk

    sanitized_choices: list[Any] = []
    changed = False

    for choice in choices:
        delta = getattr(choice, "delta", None)
        if delta is None:
            sanitized_choices.append(choice)
            continue

        sanitized_delta = delta
        raw_tool_calls = getattr(delta, "tool_calls", None) or []
        choice_changed = False

        # Some OpenAI-compatible gateways use camelCase for reasoning chunks.
        # Legacy parser reads ``reasoning_content``/``reasoning`` only, so normalize
        # here before delegating to the upstream parser.
        reasoning = extract_reasoning_delta(delta)
        existing_reasoning = getattr(delta, "reasoning_content", None)
        raw_content = getattr(delta, "content", None)
        sanitized_content = strip_reasoning_blocks(raw_content)
        if isinstance(raw_content, list):
            sanitized_content = extract_text_delta(
                _clone_with_overrides(delta, content=sanitized_content),
            ) or None
        if sanitized_content is not raw_content:
            sanitized_delta = _clone_with_overrides(
                sanitized_delta,
                content=sanitized_content,
            )
            choice_changed = True
        if reasoning and not isinstance(existing_reasoning, str):
            sanitized_delta = _clone_with_overrides(
                sanitized_delta,
                reasoning_content=reasoning,
            )
            choice_changed = True

        sanitized_tool_calls: list[Any] = []
        for tool_call in raw_tool_calls:
            sanitized = _sanitize_tool_call(tool_call)
            if sanitized is not tool_call:
                choice_changed = True
            if sanitized is not None:
                sanitized_tool_calls.append(sanitized)

        if raw_tool_calls and choice_changed:
            sanitized_delta = _clone_with_overrides(
                sanitized_delta,
                tool_calls=sanitized_tool_calls,
            )
        if choice_changed:
            changed = True
            sanitized_choice = _clone_with_overrides(
                choice,
                delta=sanitized_delta,
            )
            sanitized_choices.append(sanitized_choice)
            continue

        sanitized_choices.append(choice)

    if not changed:
        return chunk
    return _clone_with_overrides(chunk, choices=sanitized_choices)


def _sanitize_stream_item(item: Any) -> Any:
    """Sanitize either plain stream chunks or structured stream items."""
    if hasattr(item, "chunk"):
        chunk = item.chunk
        sanitized_chunk = _sanitize_chunk(chunk)
        if sanitized_chunk is chunk:
            return item
        return _clone_with_overrides(item, chunk=sanitized_chunk)

    return _sanitize_chunk(item)


class _SanitizedStream:
    """Proxy OpenAI async stream that sanitizes each emitted item and
    captures ``extra_content`` from tool-call chunks (used by Gemini
    thinking models to carry ``thought_signature``)."""

    def __init__(self, stream: Any):
        self._stream = stream
        self._ctx_stream: Any | None = None
        self.extra_contents: dict[str, Any] = {}

    async def __aenter__(self) -> "_SanitizedStream":
        if hasattr(self._stream, "__aenter__"):
            self._ctx_stream = await self._stream.__aenter__()
        else:
            self._ctx_stream = self._stream.__aiter__()
        return self

    async def __aexit__(
        self,
        exc_type: Any,
        exc: Any,
        tb: Any,
    ) -> bool | None:
        if hasattr(self._stream, "__aexit__"):
            return await self._stream.__aexit__(exc_type, exc, tb)
        if hasattr(self._stream, "aclose"):
            await self._stream.aclose()
        return False

    def __aiter__(self) -> "_SanitizedStream":
        return self

    async def __anext__(self) -> Any:
        if self._ctx_stream is None:
            raise StopAsyncIteration
        item = await self._ctx_stream.__anext__()
        self._capture_extra_content(item)
        return _sanitize_stream_item(item)

    def _capture_extra_content(self, item: Any) -> None:
        """Store ``extra_content`` keyed by tool-call id."""
        chunk = getattr(item, "chunk", item)
        choices = getattr(chunk, "choices", None) or []
        for choice in choices:
            delta = getattr(choice, "delta", None)
            if not delta:
                continue
            for tc in getattr(delta, "tool_calls", None) or []:
                tc_id = getattr(tc, "id", None)
                if not tc_id:
                    continue
                extra = getattr(tc, "extra_content", None)
                if extra is None:
                    model_extra = getattr(tc, "model_extra", None)
                    if isinstance(model_extra, dict):
                        extra = model_extra.get("extra_content")
                if extra:
                    self.extra_contents[tc_id] = extra


def _is_empty_message(msg: dict) -> bool:
    """Check if a message has effectively empty content.

    Some APIs (e.g. Kimi coding endpoint) reject messages with empty
    text content with 400 "text content is empty".  We drop assistant
    messages whose content is empty or only whitespace, but keep
    messages that carry tool_calls (otherwise downstream tool
    messages lose their matching ids).
    """
    role = msg.get("role", "")
    if role != "assistant":
        return False
    # Never drop a message that carries tool_calls – even if its
    # content is empty, the tool_calls array is required for the
    # subsequent "tool" role messages to have valid tool_call_ids.
    if msg.get("tool_calls"):
        return False
    content = msg.get("content")
    if content is None:
        return True
    if isinstance(content, str):
        return not content.strip()
    if isinstance(content, list):
        # Legacy content blocks: [{"type": "text", "text": ""}]
        for block in content:
            if isinstance(block, dict):
                text = block.get("text") or block.get("thinking") or ""
                if text and str(text).strip():
                    return False
            elif block:
                return False
        return True
    return False


_KIMI_MULTIMODAL_BLOCK_TYPES = {
    "image",
    "image_url",
    "input_image",
    "video",
    "video_url",
    "input_video",
    "audio",
    "input_audio",
}
_KIMI_MULTIMODAL_KEYS = {
    "image_url",
    "input_image",
    "video_url",
    "input_video",
    "audio_url",
    "input_audio",
}


def _content_block_is_multimodal(block: Any) -> bool:
    """Return True when a content block carries media."""
    if not isinstance(block, dict):
        return False
    block_type = str(block.get("type") or "")
    return block_type in _KIMI_MULTIMODAL_BLOCK_TYPES or any(
        key in block for key in _KIMI_MULTIMODAL_KEYS
    )


def _content_has_multimodal_blocks(content: Any) -> bool:
    """Return True when a message content value contains media blocks."""
    if isinstance(content, list):
        return any(_content_block_is_multimodal(block) for block in content)
    return _content_block_is_multimodal(content)


def _text_block(text: str) -> dict:
    return {"type": "text", "text": text}


def _normalize_content_to_plain_text(msg: dict) -> dict:
    """Collapse OpenAI content blocks into plain text for strict backends.

    Many OpenAI-compatible local runtimes (Ollama, LM Studio, llama.cpp)
    implement only the text subset of the chat-completions schema and reject
    content arrays.
    """
    content = msg.get("content")
    if not isinstance(content, list):
        return msg

    texts: list[str] = []
    for block in content:
        if isinstance(block, dict):
            text = block.get("text") or block.get("thinking") or ""
            if text:
                texts.append(str(text))
        elif block:
            texts.append(str(block))

    new_msg = dict(msg)
    new_msg["content"] = "\n".join(texts)
    return new_msg


def _normalize_strict_text_message(msg: dict) -> dict:
    """Keep only broadly supported OpenAI chat message fields.

    Several OpenAI-compatible local runtimes reject content arrays and
    provider-specific extras such as ``reasoning_content``. Tool fields are
    preserved so capable strict backends can still participate in tool calls.
    """
    msg = _normalize_content_to_plain_text(msg)
    allowed = {"role", "content", "tool_calls", "tool_call_id"}
    strict = {key: msg[key] for key in allowed if key in msg}
    name = msg.get("name")
    if name:
        strict["name"] = name
    return strict


def _normalize_kimi_coding_message(msg: dict) -> dict:
    """Sanitize a Kimi coding message while preserving media content.

    Kimi's coding endpoint accepts multimodal content arrays, but it is still
    sensitive to unrelated provider-specific top-level fields from mixed
    histories.  Kimi's own ``reasoning_content`` is part of the assistant
    message contract and must survive multi-step tool continuations together
    with ``tool_calls`` and ``tool_call_id``.  Text-only content arrays are
    collapsed for compatibility; arrays carrying image/video blocks are kept
    intact.
    """
    if _content_has_multimodal_blocks(msg.get("content")):
        allowed = {"role", "content"}
        strict = {key: msg[key] for key in allowed if key in msg}
        name = msg.get("name")
        if name:
            strict["name"] = name
        return strict
    msg = _normalize_content_to_plain_text(msg)
    allowed = {
        "role",
        "content",
        "tool_calls",
        "tool_call_id",
        "reasoning_content",
    }
    strict = {key: msg[key] for key in allowed if key in msg}
    name = msg.get("name")
    if name:
        strict["name"] = name
    return strict


def _get_field(obj: Any, key: str, default: Any = None) -> Any:
    if isinstance(obj, dict):
        return obj.get(key, default)
    return getattr(obj, key, default)


def _jsonish_to_text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    try:
        return json.dumps(value, ensure_ascii=False)
    except (TypeError, ValueError):
        return str(value)


def _tool_call_name_and_input(tool_call: Any) -> tuple[str, str]:
    function = _get_field(tool_call, "function", {}) or {}
    name = _get_field(function, "name") or _get_field(tool_call, "name") or ""
    arguments = _get_field(function, "arguments", None)
    if arguments is None:
        arguments = _get_field(tool_call, "input", None)
    if arguments is None:
        arguments = _get_field(tool_call, "raw_input", None)
    return str(name or "unknown_tool"), _jsonish_to_text(arguments)


def _tool_result_output_to_text(output: Any) -> str:
    if isinstance(output, list):
        parts = []
        for item in output:
            if isinstance(item, dict):
                parts.append(_content_block_to_plain_text(item))
            else:
                parts.append(_jsonish_to_text(item))
        return "\n".join(part for part in parts if part)
    if isinstance(output, dict):
        return _content_block_to_plain_text(output)
    return _jsonish_to_text(output)


def _content_block_to_plain_text(block: dict) -> str:
    block_type = block.get("type")
    if block_type == "text":
        return str(block.get("text") or "")
    if block_type == "thinking":
        return str(block.get("thinking") or "")
    if block_type == "tool_use":
        name = str(block.get("name") or "unknown_tool")
        raw_input = block.get("raw_input")
        if raw_input is None:
            raw_input = block.get("input")
        return (
            f"Tool call requested: {name}\n"
            f"Input: {_jsonish_to_text(raw_input)}"
        )
    if block_type == "tool_result":
        name = str(block.get("name") or "unknown_tool")
        output = block.get("output")
        if output is None:
            output = block.get("content")
        return (
            f"Tool result from {name}:\n"
            f"{_tool_result_output_to_text(output)}"
        )
    if "text" in block:
        return str(block.get("text") or "")
    return _jsonish_to_text(block)


def _content_to_plain_text(content: Any) -> str:
    if isinstance(content, list):
        parts = []
        for block in content:
            if isinstance(block, dict):
                parts.append(_content_block_to_plain_text(block))
            else:
                parts.append(_jsonish_to_text(block))
        return "\n".join(part for part in parts if part)
    return _jsonish_to_text(content)


def _content_to_plain_text_without_tools(content: Any) -> str:
    if not isinstance(content, list):
        return _jsonish_to_text(content)
    parts = []
    for block in content:
        if isinstance(block, dict) and block.get("type") in {
            "tool_use",
            "tool_result",
        }:
            continue
        if isinstance(block, dict):
            parts.append(_content_block_to_plain_text(block))
        else:
            parts.append(_jsonish_to_text(block))
    return "\n".join(part for part in parts if part)


def _has_content_block_type(content: Any, block_types: set[str]) -> bool:
    return isinstance(content, list) and any(
        isinstance(block, dict) and block.get("type") in block_types
        for block in content
    )


def _is_legacy_tool_call_echo(msg: dict) -> bool:
    if msg.get("role") != "assistant":
        return False
    text = _content_to_plain_text(msg.get("content")).strip()
    return text.startswith("Tool call requested:") and "Input:" in text


def _flatten_tool_role_message(msg: dict) -> dict:
    name = str(msg.get("name") or "unknown_tool")
    label = "Previous tool execution result"
    if name and name != "unknown_tool":
        label += f" from {name}"

    content = msg.get("content")
    if _content_has_multimodal_blocks(content):
        output_blocks = content if isinstance(content, list) else [content]
        return {
            "role": "user",
            "content": [
                _text_block(f"{label}:"),
                *output_blocks,
            ],
        }

    output = _content_to_plain_text(content)
    return {"role": "user", "content": f"{label}:\n{output}".strip()}


def _assistant_text_without_tool_calls(msg: dict) -> dict | None:
    content_text = _content_to_plain_text_without_tools(msg.get("content"))
    if not content_text.strip():
        return None
    return {"role": "assistant", "content": content_text.strip()}


def _flatten_embedded_tool_result_message(content: Any) -> dict | None:
    """Flatten legacy-style tool_result blocks into a user message.

    Text-only tool results are returned as plain text. Tool results that carry
    media keep their image/video blocks so Kimi can still inspect them after
    the unsafe historical tool pairing has been removed.
    """
    if not isinstance(content, list):
        return None

    output: list[dict] = []
    text_parts: list[str] = []

    def flush_text() -> None:
        if text_parts:
            output.append(_text_block("\n".join(text_parts)))
            text_parts.clear()

    for block in content:
        if not isinstance(block, dict) or block.get("type") != "tool_result":
            if _content_block_is_multimodal(block):
                flush_text()
                output.append(block)
            else:
                text = (
                    _content_block_to_plain_text(block)
                    if isinstance(block, dict)
                    else _jsonish_to_text(block)
                )
                if text:
                    text_parts.append(text)
            continue

        name = str(block.get("name") or "unknown_tool")
        label = f"Tool result from {name}:"
        result_output = block.get("output")
        if result_output is None:
            result_output = block.get("content")

        if _content_has_multimodal_blocks(result_output):
            flush_text()
            output.append(_text_block(label))
            if isinstance(result_output, list):
                output.extend(result_output)
            elif isinstance(result_output, dict):
                output.append(result_output)
        else:
            text_parts.append(f"{label}\n{_tool_result_output_to_text(result_output)}")

    flush_text()
    if not output:
        return None
    if _content_has_multimodal_blocks(output):
        return {"role": "user", "content": output}
    text = "\n".join(
        block.get("text", "")
        for block in output
        if isinstance(block, dict) and block.get("type") == "text"
    ).strip()
    if not text:
        return None
    return {"role": "user", "content": text}


def _clean_kimi_coding_messages(messages: list[dict]) -> list[dict]:
    """Return a Kimi-coding-safe history.

    Preserve well-formed OpenAI tool-call pairs and the assistant's
    ``reasoning_content``.  Only malformed or legacy tool-result rows are
    flattened to ordinary user text; flattening every historical pair breaks
    Kimi's preserved-thinking protocol and can make continuation reasoning
    appear in the visible answer channel.
    """
    paired_tool_call_ids: set[str] = set()
    for message in messages:
        for tool_call in message.get("tool_calls") or []:
            call_id = str(_get_field(tool_call, "id") or "").strip()
            if call_id:
                paired_tool_call_ids.add(call_id)

    cleaned: list[dict] = []
    for msg in messages:
        if _is_legacy_tool_call_echo(msg):
            continue

        if msg.get("role") == "tool" or msg.get("tool_call_id") is not None:
            tool_call_id = str(msg.get("tool_call_id") or "").strip()
            if tool_call_id and tool_call_id in paired_tool_call_ids:
                normalized = _normalize_kimi_coding_message(msg)
                if not _is_empty_message(normalized):
                    cleaned.append(normalized)
                continue
            flattened = _flatten_tool_role_message(msg)
            if flattened["content"]:
                cleaned.append(flattened)
            continue

        content = msg.get("content")
        if _has_content_block_type(content, {"tool_result"}):
            flattened = _flatten_embedded_tool_result_message(content)
            if flattened is not None:
                cleaned.append(flattened)
            continue

        if (
            msg.get("role") == "assistant"
            and (
                msg.get("tool_calls")
                or _has_content_block_type(content, {"tool_use"})
            )
        ):
            if msg.get("tool_calls"):
                normalized = _normalize_kimi_coding_message(msg)
                if not _is_empty_message(normalized):
                    cleaned.append(normalized)
                continue
            text_only = _assistant_text_without_tool_calls(msg)
            if text_only is not None:
                cleaned.append(text_only)
            continue

        msg = _normalize_kimi_coding_message(msg)
        if _is_empty_message(msg):
            continue
        cleaned.append(msg)
    return cleaned


class _LegacyToolCallAccumulator:
    """Accumulate OpenAI tool-call deltas into legacy tool_use blocks."""

    def __init__(self) -> None:
        self._calls: dict[int, dict[str, Any]] = {}
        self._sequence = 0

    def update(self, raw_calls: list[Any]) -> None:
        for raw in raw_calls:
            sanitized = _sanitize_tool_call(raw)
            if sanitized is None:
                continue
            index = int(getattr(sanitized, "index", 0) or 0)
            current = self._calls.setdefault(
                index,
                {"id": "", "name": "", "arguments": "", "order": 0},
            )
            self._sequence += 1
            current["order"] = self._sequence
            call_id = getattr(sanitized, "id", None)
            if call_id:
                current["id"] = str(call_id)
            function = getattr(sanitized, "function", None)
            name = getattr(function, "name", None)
            if name:
                current["name"] = str(name)
            arguments = getattr(function, "arguments", None)
            if arguments:
                current["arguments"] = (
                    str(current.get("arguments") or "") + str(arguments)
                )

    def blocks(self) -> list[dict[str, Any]]:
        result: list[dict[str, Any]] = []
        ordered = sorted(
            self._calls.items(),
            key=lambda item: int(item[1].get("order") or 0),
        )
        for index, current in ordered:
            raw_arguments = str(current.get("arguments") or "")
            try:
                parsed_arguments = (
                    json.loads(raw_arguments) if raw_arguments else {}
                )
            except (TypeError, ValueError):
                parsed_arguments = {"_raw": raw_arguments}
            result.append(
                {
                    "type": "tool_use",
                    "id": str(current.get("id") or f"call_{index}"),
                    "name": str(current.get("name") or ""),
                    "input": parsed_arguments,
                    "raw_input": raw_arguments,
                },
            )
        return result


class OpenAIChatModelCompat:
    """OpenAI-compatible chat model used by the legacy agent path.

    The class keeps the small surface the legacy ReAct loop expects
    (``model_name``, ``stream`` and async ``__call__``) while avoiding an
    inheritance dependency on an external chat model base class.
    """

    def __init__(
        self,
        model_name: str,
        api_key: str | None = None,
        stream: bool = True,
        reasoning_effort: str | None = None,
        organization: str | None = None,
        stream_tool_parsing: bool = True,
        client_type: str = "openai",
        client_kwargs: dict[str, Any] | None = None,
        generate_kwargs: dict[str, Any] | None = None,
        normalize_content_to_text: bool = False,
        **kwargs: Any,
    ) -> None:
        _ = stream_tool_parsing, kwargs
        self.model_name = model_name
        self.stream = stream
        self.reasoning_effort = reasoning_effort
        self.generate_kwargs = dict(generate_kwargs or {})
        self._normalize_content_to_text = normalize_content_to_text
        self._tool_choice_compatibility = ToolChoiceCompatibility()

        client_options = dict(client_kwargs or {})
        if "http_client" not in client_options:
            client_options["http_client"] = (
                create_openai_async_http_client()
            )
        if api_key is not None:
            client_options["api_key"] = api_key
        if organization is not None:
            client_options["organization"] = organization
        if client_type == "azure":
            self.client = openai.AsyncAzureOpenAI(**client_options)
        else:
            self.client = openai.AsyncOpenAI(**client_options)

    async def __call__(
        self,
        messages: list[dict],
        tools: list[dict] | None = None,
        tool_choice: str | None = None,
        structured_model: Any | None = None,
        **kwargs: Any,
    ) -> Any:
        """Filter empty assistant messages before sending to OpenAI API."""
        filtered = [m for m in messages if not _is_empty_message(m)]
        if self._normalize_content_to_text:
            filtered = [_normalize_strict_text_message(m) for m in filtered]
        # Normalize mixed histories without breaking Kimi's native preserved-
        # thinking and tool-call continuation contract.
        base_url = getattr(getattr(self, "client", None), "base_url", "")
        if is_kimi_coding_base_url(str(base_url)):
            filtered = _clean_kimi_coding_messages(filtered)
        return await self._send_request(
            filtered,
            tools=tools,
            tool_choice=tool_choice,
            structured_model=structured_model,
            **kwargs,
        )

    async def _send_request(
        self,
        messages: list[dict],
        tools: list[dict] | None = None,
        tool_choice: str | None = None,
        structured_model: Any | None = None,
        **kwargs: Any,
    ) -> Any:
        """Send one request to the OpenAI-compatible chat completions API."""

        if structured_model is not None:
            raise NotImplementedError(
                "Structured model output is handled by the legacy tool path.",
            )

        payload: dict[str, Any] = {
            "model": self.model_name,
            "messages": messages,
            "stream": self.stream,
            **self.generate_kwargs,
            **kwargs,
        }
        if self.reasoning_effort and "reasoning_effort" not in payload:
            payload["reasoning_effort"] = self.reasoning_effort
        if tools:
            payload["tools"] = tools
        if tool_choice:
            base_url = str(getattr(self.client, "base_url", "") or "")
            payload["tool_choice"] = compatible_fixed_reasoning_tool_choice(
                capability=get_reasoning_capability(
                    "", self.model_name, base_url=base_url,
                ),
                policy=resolve_reasoning_continuation_policy(
                    "", self.model_name, base_url=base_url,
                ),
                tool_choice="required" if tool_choice == "any" else tool_choice,
                has_tools=bool(payload.get("tools")),
            )
        if self.stream:
            payload["stream_options"] = {"include_usage": True}

        usage_rejected = False

        async def create(**wire_payload: Any) -> Any:
            nonlocal usage_rejected
            if usage_rejected:
                wire_payload.pop("stream_options", None)
            try:
                return await self.client.chat.completions.create(**wire_payload)
            except Exception as exc:
                if (
                    self.stream
                    and "stream_options" in wire_payload
                    and getattr(exc, "status_code", None) == 400
                    and "stream_options" in str(exc)
                ):
                    usage_rejected = True
                    wire_payload.pop("stream_options", None)
                    return await self.client.chat.completions.create(**wire_payload)
                raise

        call = await self._tool_choice_compatibility.create(
            create,
            payload,
            provider_id="",
            base_url=str(getattr(self.client, "base_url", "") or ""),
        )

        if self.stream:
            return self._parse_openai_stream_response(
                datetime.now(),
                call.iter_stream(),
                structured_model=structured_model,
                compatibility_metadata=call.metadata,
            )
        response = self._parse_openai_response(call.response)
        response.metadata = call.metadata()
        return response

    def _parse_openai_response(self, response: Any) -> Any:
        """Convert a non-streaming response into the legacy chunk shape."""

        choices = getattr(response, "choices", None) or []
        message = getattr(choices[0], "message", None) if choices else None
        content: list[dict[str, Any]] = []
        if message is not None:
            reasoning = extract_reasoning_delta(message)
            if reasoning:
                content.append({"type": "thinking", "thinking": reasoning})
            text = extract_text_delta(message)
            if text:
                content.append({"type": "text", "text": text})
            accumulator = _LegacyToolCallAccumulator()
            raw_calls = getattr(message, "tool_calls", None) or []
            accumulator.update(list(raw_calls) if isinstance(raw_calls, list) else [])
            content.extend(accumulator.blocks())
        return SimpleNamespace(
            id=getattr(response, "id", None),
            content=content,
            usage=getattr(response, "usage", None),
            finish_reason=getattr(choices[0], "finish_reason", None) if choices else None,
        )

    # pylint: disable=too-many-branches
    async def _parse_openai_stream_response(
        self,
        start_datetime: datetime,
        response: Any,
        structured_model: Type[BaseModel] | None = None,
        compatibility_metadata: Any = None,
    ) -> AsyncGenerator[Any, None]:
        _ = start_datetime, structured_model
        sanitized_response = _SanitizedStream(response)

        # Stable tag-extracted tool-call blocks across streaming chunks.
        # Keyed by positional strings so IDs stay consistent as chunks
        # accumulate.  Two sources: "thinking" blocks and plain "text" blocks.
        _think_tool_calls: dict[str, dict] = {}
        _text_tool_calls: dict[str, dict] = {}
        tool_accumulator = _LegacyToolCallAccumulator()
        text_parts: list[str] = []
        thinking_parts: list[str] = []
        message_id: str | None = None

        async with sanitized_response as stream:
            async for chunk in stream:
                message_id = str(
                    getattr(chunk, "id", None)
                    or message_id
                    or f"chatcmpl_{id(self)}",
                )
                choices = getattr(chunk, "choices", None) or []
                finish_reason = None
                for choice in choices:
                    reason = getattr(choice, "finish_reason", None)
                    if reason is not None:
                        finish_reason = reason
                    delta = getattr(choice, "delta", None)
                    if delta is None:
                        continue
                    normalized = normalize_stream_chunk(
                        delta,
                        usage=getattr(chunk, "usage", None),
                        finish_reason=getattr(choice, "finish_reason", None),
                    )
                    if normalized.thinking_delta:
                        thinking_parts.append(normalized.thinking_delta)
                    if normalized.text_delta:
                        text_parts.append(normalized.text_delta)
                    if normalized.tool_call_delta:
                        tool_accumulator.update(normalized.tool_call_delta)

                content: list[dict[str, Any]] = []
                thinking_text = "".join(thinking_parts)
                if thinking_text:
                    thinking_block = {
                        "type": "thinking",
                        "thinking": thinking_text,
                    }
                    if text_contains_tool_call_tag(thinking_text):
                        think_parsed = parse_tool_calls_from_text(
                            thinking_text,
                        )
                        thinking_block["thinking"] = (
                            think_parsed.text_before.strip()
                        )
                        _think_tool_calls = {
                            f"thinking_{i}": {
                                "type": "tool_use",
                                "id": f"think_call_{i}",
                                "name": ptc.name,
                                "input": ptc.arguments,
                                "raw_input": ptc.raw_arguments,
                            }
                            for i, ptc in enumerate(think_parsed.tool_calls)
                        }
                    if thinking_block["thinking"]:
                        content.append(thinking_block)

                text = "".join(text_parts)
                if text:
                    text_block = {"type": "text", "text": text}
                    if text_contains_tool_call_tag(text):
                        text_parsed = parse_tool_calls_from_text(text)
                        text_block["text"] = text_parsed.text_before.strip()
                        _text_tool_calls = {
                            f"text_{j}": {
                                "type": "tool_use",
                                "id": f"text_call_{j}",
                                "name": ptc.name,
                                "input": ptc.arguments,
                                "raw_input": ptc.raw_arguments,
                            }
                            for j, ptc in enumerate(text_parsed.tool_calls)
                        }
                    if text_block["text"]:
                        content.append(text_block)

                tool_blocks = tool_accumulator.blocks()
                if tool_blocks:
                    _think_tool_calls.clear()
                    _text_tool_calls.clear()
                content.extend(tool_blocks)

                for block in content:
                    if block.get("type") != "tool_use":
                        continue
                    tool_id = block.get("id")
                    if not isinstance(tool_id, str):
                        continue
                    ec = sanitized_response.extra_contents.get(tool_id)
                    if ec:
                        block["extra_content"] = ec

                content.extend(_think_tool_calls.values())
                content.extend(_text_tool_calls.values())
                usage = getattr(chunk, "usage", None)
                if content or usage is not None or finish_reason is not None:
                    yield SimpleNamespace(
                        id=message_id,
                        content=content,
                        # This adapter has already accumulated wire deltas.
                        text_semantics="snapshot",
                        usage=usage,
                        finish_reason=finish_reason,
                        metadata=(
                            compatibility_metadata() if compatibility_metadata else {}
                        ),
                    )
