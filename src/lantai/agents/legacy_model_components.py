# -*- coding: utf-8 -*-
"""Provider-specific chat models and formatters used by compatibility paths."""

from __future__ import annotations

import base64
import copy
import json
import logging
import os
import time
import warnings
from collections import OrderedDict
from collections.abc import AsyncGenerator, AsyncIterator, Sequence
from dataclasses import dataclass, field
from typing import Any, ClassVar, Literal, Type
from urllib.parse import unquote, urlparse
from uuid import uuid4

import requests
from json_repair import repair_json
from pydantic import BaseModel

from .openai_chat_formatter import LantaiOpenAIChatFormatter
from ..runtime.attachment_context import attachment_context_text

logger = logging.getLogger(__name__)

_TOOL_CHOICE_MODES = ["auto", "none", "required"]


@dataclass
class ChatUsage:
    """Minimal usage object consumed by Lantai token recording wrappers."""

    input_tokens: int = 0
    output_tokens: int = 0
    time: float = 0.0
    metadata: dict[str, Any] | None = None


@dataclass
class ChatResponse:
    """Minimal chat response object used by the compatibility agent path."""

    # Compatibility adapters accumulate wire deltas before yielding content.
    # Keep this transport contract out of the serialized response fields.
    text_semantics: ClassVar[Literal["snapshot"]] = "snapshot"
    finish_reason: ClassVar[str | None] = None
    content: Sequence[dict[str, Any]]
    id: str = field(default_factory=lambda: f"chat_{uuid4().hex}")
    usage: ChatUsage | None = None
    metadata: dict[str, Any] | None = None
    type: str = "chat"


class _MessageShim:
    def __init__(self, *, name: str, role: str, content: Any) -> None:
        self.name = name
        self.role = role
        self.content = content

    def get_content_blocks(self) -> list[dict[str, Any]]:
        if isinstance(self.content, str):
            return [{"type": "text", "text": self.content}]
        if isinstance(self.content, list):
            return [block for block in self.content if isinstance(block, dict)]
        return [{"type": "text", "text": str(self.content)}]


def _json_loads_with_repair(value: Any) -> Any:
    if isinstance(value, (dict, list)):
        return value
    text = str(value or "").strip()
    if not text:
        return {}
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        try:
            return repair_json(text, return_objects=True)
        except (ValueError, TypeError):
            return {}


def _parse_streaming_json_dict(
    partial_json: str,
    last_value: dict[str, Any] | None = None,
) -> dict[str, Any]:
    parsed = _json_loads_with_repair(partial_json)
    if isinstance(parsed, dict):
        return parsed
    return dict(last_value or {})


def _model_tool_from_base_model(model: Type[BaseModel]) -> dict[str, Any]:
    schema = model.model_json_schema()
    name = str(schema.get("title") or model.__name__)
    description = str(schema.get("description") or "")
    return {
        "type": "function",
        "function": {
            "name": name,
            "description": description,
            "parameters": schema,
        },
    }


def _validate_tool_choice(
    tool_choice: str,
    tools: list[dict] | None,
) -> None:
    if not isinstance(tool_choice, str):
        raise TypeError(f"tool_choice must be str, got {type(tool_choice)}")
    if tool_choice in _TOOL_CHOICE_MODES:
        return
    available = [
        str(tool.get("function", {}).get("name"))
        for tool in (tools or [])
        if isinstance(tool, dict) and tool.get("function", {}).get("name")
    ]
    if tool_choice not in available:
        all_options = sorted([*_TOOL_CHOICE_MODES, *available])
        raise ValueError(
            f"Invalid tool_choice '{tool_choice}'. "
            f"Available options: {', '.join(all_options)}",
        )


def _usage_from_anthropic(
    usage: Any,
    started_at: float,
) -> ChatUsage | None:
    if not usage:
        return None
    return ChatUsage(
        input_tokens=int(getattr(usage, "input_tokens", 0) or 0),
        output_tokens=int(getattr(usage, "output_tokens", 0) or 0),
        time=time.monotonic() - started_at,
    )


def _usage_from_gemini(
    usage_metadata: Any,
    started_at: float,
) -> ChatUsage | None:
    if not usage_metadata:
        return None
    prompt_tokens = getattr(usage_metadata, "prompt_token_count", None)
    total_tokens = getattr(usage_metadata, "total_token_count", None)
    output_tokens = getattr(usage_metadata, "candidates_token_count", None)
    if prompt_tokens is None and total_tokens is None and output_tokens is None:
        return None
    prompt = int(prompt_tokens or 0)
    if output_tokens is None and total_tokens is not None:
        output = max(0, int(total_tokens or 0) - prompt)
    else:
        output = int(output_tokens or 0)
    return ChatUsage(
        input_tokens=prompt,
        output_tokens=output,
        time=time.monotonic() - started_at,
    )


def _format_anthropic_tool_schemas(
    schemas: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    formatted = []
    for schema in schemas:
        function = schema.get("function", {})
        formatted.append(
            {
                "name": function.get("name"),
                "description": function.get("description", ""),
                "input_schema": function.get("parameters", {}),
            },
        )
    return formatted


def _format_anthropic_tool_choice(
    tool_choice: Literal["auto", "none", "required"] | str | None,
) -> dict[str, Any] | None:
    if tool_choice is None:
        return None
    mapping = {
        "auto": {"type": "auto"},
        "none": {"type": "none"},
        "required": {"type": "any"},
    }
    if tool_choice in mapping:
        return mapping[tool_choice]
    return {"type": "tool", "name": tool_choice}


def _format_gemini_tool_choice(
    tool_choice: Literal["auto", "none", "required"] | str | None,
) -> dict[str, Any] | None:
    if tool_choice is None:
        return None
    mode_mapping = {
        "auto": "AUTO",
        "none": "NONE",
        "required": "ANY",
    }
    mode = mode_mapping.get(tool_choice)
    if mode:
        return {"function_calling_config": {"mode": mode}}
    return {
        "function_calling_config": {
            "mode": "ANY",
            "allowed_function_names": [tool_choice],
        },
    }


def _flatten_json_schema(schema: dict[str, Any]) -> dict[str, Any]:
    schema = copy.deepcopy(schema)
    defs = schema.pop("$defs", {})

    def _resolve(obj: Any, visited: set[str] | None = None) -> Any:
        if visited is None:
            visited = set()
        if isinstance(obj, list):
            return [_resolve(item, set(visited)) for item in obj]
        if not isinstance(obj, dict):
            return obj
        if "$ref" in obj:
            ref_path = obj["$ref"]
            if ref_path.startswith("#/$defs/"):
                name = ref_path[len("#/$defs/") :]
                if name in visited:
                    logger.warning(
                        "Circular reference detected for '%s' in tool schema",
                        name,
                    )
                    return {}
                if name in defs:
                    visited.add(name)
                    return _resolve(defs[name], visited)
        return {key: _resolve(value, set(visited)) for key, value in obj.items()}

    return _resolve(schema)


def _format_gemini_tool_schemas(
    schemas: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    declarations = []
    for schema in schemas:
        function = dict(schema.get("function") or {})
        if not function:
            continue
        if "parameters" in function:
            function["parameters"] = _flatten_json_schema(function["parameters"])
        declarations.append(function)
    return [{"function_declarations": declarations}] if declarations else []


class AnthropicChatModel:
    """Anthropic Messages API wrapper with the legacy chat model surface."""

    def __init__(
        self,
        model_name: str,
        api_key: str | None = None,
        max_tokens: int = 2048,
        stream: bool = True,
        thinking: dict[str, Any] | None = None,
        stream_tool_parsing: bool = True,
        client_kwargs: dict[str, Any] | None = None,
        generate_kwargs: dict[str, Any] | None = None,
        **kwargs: Any,
    ) -> None:
        client_args = kwargs.pop("client_args", None)
        if client_args is not None and client_kwargs is not None:
            raise ValueError(
                "Cannot specify both 'client_args' and 'client_kwargs'.",
            )
        if client_args is not None:
            logger.warning(
                "'client_args' is deprecated; use 'client_kwargs' instead.",
            )
            client_kwargs = client_args
        if kwargs:
            logger.warning("Unknown keyword arguments ignored: %s", list(kwargs))
        try:
            import anthropic
        except ImportError as exc:  # pragma: no cover - dependency guard
            raise ImportError(
                "Please install the `anthropic` package.",
            ) from exc

        self.model_name = model_name
        self.stream = stream
        self.max_tokens = max_tokens
        self.thinking = thinking
        self.stream_tool_parsing = stream_tool_parsing
        self.generate_kwargs = generate_kwargs or {}
        self.client = anthropic.AsyncAnthropic(
            api_key=api_key,
            **(client_kwargs or {}),
        )

    async def __call__(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        tool_choice: Literal["auto", "none", "required"] | str | None = None,
        structured_model: Type[BaseModel] | None = None,
        **generate_kwargs: Any,
    ) -> ChatResponse | AsyncGenerator[ChatResponse, None]:
        kwargs: dict[str, Any] = {
            "model": self.model_name,
            "max_tokens": self.max_tokens,
            "stream": self.stream,
            **self.generate_kwargs,
            **generate_kwargs,
        }
        if self.thinking and "thinking" not in kwargs:
            kwargs["thinking"] = self.thinking
        if tools:
            kwargs["tools"] = _format_anthropic_tool_schemas(tools)
        if tool_choice:
            if tool_choice == "any":
                warnings.warn(
                    '"any" is deprecated; use "required" instead.',
                    DeprecationWarning,
                    stacklevel=2,
                )
                tool_choice = "required"
            _validate_tool_choice(tool_choice, tools)
            kwargs["tool_choice"] = _format_anthropic_tool_choice(tool_choice)
        if structured_model:
            if tools or tool_choice:
                logger.warning(
                    "structured_model overrides tools and tool_choice.",
                )
            format_tool = _model_tool_from_base_model(structured_model)
            kwargs["tools"] = _format_anthropic_tool_schemas([format_tool])
            kwargs["tool_choice"] = _format_anthropic_tool_choice(
                format_tool["function"]["name"],
            )
        if messages and messages[0].get("role") == "system":
            kwargs["system"] = messages[0].get("content")
            messages = messages[1:]
        kwargs["messages"] = messages

        started_at = time.monotonic()
        response = await self.client.messages.create(**kwargs)
        if self.stream:
            return self._parse_stream_response(
                started_at,
                response,
                structured_model,
            )
        return self._parse_response(started_at, response, structured_model)

    def _parse_response(
        self,
        started_at: float,
        response: Any,
        structured_model: Type[BaseModel] | None = None,
    ) -> ChatResponse:
        blocks: list[dict[str, Any]] = []
        metadata = None
        for block in getattr(response, "content", []) or []:
            block_type = getattr(block, "type", None)
            if block_type == "thinking":
                thinking_block = {
                    "type": "thinking",
                    "thinking": getattr(block, "thinking", ""),
                }
                signature = getattr(block, "signature", None)
                if signature:
                    thinking_block["signature"] = signature
                blocks.append(thinking_block)
            elif block_type == "redacted_thinking":
                # Anthropic requires redacted thinking blocks to be replayed
                # byte-for-byte in the same assistant/tool transaction. Keep
                # it in the canonical thinking slot but mark its wire type so
                # only the Anthropic formatter restores it.
                blocks.append(
                    {
                        "type": "thinking",
                        "thinking": "",
                        "provider_block_type": "redacted_thinking",
                        "data": getattr(block, "data", "") or "",
                    },
                )
            elif block_type == "text":
                blocks.append(
                    {
                        "type": "text",
                        "text": getattr(block, "text", ""),
                    },
                )
            elif block_type == "tool_use":
                input_obj = getattr(block, "input", {}) or {}
                blocks.append(
                    {
                        "type": "tool_use",
                        "id": getattr(block, "id", None),
                        "name": getattr(block, "name", None),
                        "input": input_obj,
                    },
                )
                if structured_model:
                    metadata = input_obj
        result = ChatResponse(
            id=getattr(response, "id", None) or f"chat_{uuid4().hex}",
            content=blocks,
            usage=_usage_from_anthropic(
                getattr(response, "usage", None),
                started_at,
            ),
            metadata=metadata,
        )
        reason = getattr(response, "stop_reason", None)
        if isinstance(reason, str):
            result.finish_reason = reason
        return result

    async def _parse_stream_response(
        self,
        started_at: float,
        response: Any,
        structured_model: Type[BaseModel] | None = None,
    ) -> AsyncGenerator[ChatResponse, None]:
        usage: ChatUsage | None = None
        response_id: str | None = None
        text_buffer = ""
        thinking_buffer = ""
        thinking_signature = ""
        redacted_thinking_blocks: list[dict[str, Any]] = []
        tool_calls: OrderedDict[int, dict[str, Any]] = OrderedDict()
        tool_call_buffers: dict[int, str] = {}
        last_input_objs: dict[str, dict[str, Any]] = {}
        last_content: list[dict[str, Any]] | None = None
        metadata = None
        finish_reason: str | None = None

        async for event in response:
            content_changed = False
            thinking_changed = False
            finish_changed = False
            event_type = getattr(event, "type", None)

            if event_type == "message_start":
                message = getattr(event, "message", None)
                response_id = response_id or getattr(message, "id", None)
                usage = _usage_from_anthropic(
                    getattr(message, "usage", None),
                    started_at,
                )
            elif event_type == "content_block_start":
                content_block = getattr(event, "content_block", None)
                content_block_type = getattr(content_block, "type", None)
                if content_block_type == "redacted_thinking":
                    redacted_thinking_blocks.append(
                        {
                            "type": "thinking",
                            "thinking": "",
                            "provider_block_type": "redacted_thinking",
                            "data": getattr(content_block, "data", "") or "",
                        },
                    )
                    content_changed = True
                elif content_block_type == "tool_use":
                    index = int(getattr(event, "index", 0) or 0)
                    tool_calls[index] = {
                        "type": "tool_use",
                        "id": getattr(content_block, "id", None),
                        "name": getattr(content_block, "name", None),
                        "input": "",
                    }
                    tool_call_buffers[index] = ""
                    content_changed = True
            elif event_type == "content_block_delta":
                index = int(getattr(event, "index", 0) or 0)
                delta = getattr(event, "delta", None)
                delta_type = getattr(delta, "type", None)
                if delta_type == "text_delta":
                    text_buffer += getattr(delta, "text", "") or ""
                    content_changed = True
                elif delta_type == "thinking_delta":
                    thinking_buffer += getattr(delta, "thinking", "") or ""
                    thinking_changed = True
                elif delta_type == "signature_delta":
                    thinking_signature += (
                        getattr(delta, "signature", "") or ""
                    )
                    thinking_changed = True
                elif delta_type == "input_json_delta" and index in tool_calls:
                    tool_call_buffers[index] += getattr(delta, "partial_json", "") or ""
                    tool_calls[index]["input"] = tool_call_buffers[index]
                    content_changed = True
            elif event_type == "message_delta":
                reason = getattr(getattr(event, "delta", None), "stop_reason", None)
                if isinstance(reason, str) and reason != finish_reason:
                    finish_reason = reason
                    finish_changed = True
                event_usage = getattr(event, "usage", None)
                if event_usage and usage is not None:
                    usage.input_tokens = max(
                        usage.input_tokens,
                        int(getattr(event_usage, "input_tokens", 0) or 0),
                    )
                    usage.output_tokens = int(
                        getattr(event_usage, "output_tokens", 0) or 0,
                    )

            if not (thinking_changed or content_changed or finish_changed):
                continue
            content = []
            content.extend(copy.deepcopy(redacted_thinking_blocks))
            if thinking_buffer:
                thinking_block = {
                    "type": "thinking",
                    "thinking": thinking_buffer,
                }
                if thinking_signature:
                    thinking_block["signature"] = thinking_signature
                content.append(thinking_block)
            if text_buffer:
                content.append({"type": "text", "text": text_buffer})
            for tool_call in tool_calls.values():
                input_str = str(tool_call.get("input") or "")
                tool_id = str(tool_call.get("id") or "")
                if self.stream_tool_parsing:
                    repaired_input = _parse_streaming_json_dict(
                        input_str,
                        last_input_objs.get(tool_id),
                    )
                    last_input_objs[tool_id] = repaired_input
                else:
                    repaired_input = {}
                content.append(
                    {
                        "type": "tool_use",
                        "id": tool_call.get("id"),
                        "name": tool_call.get("name"),
                        "input": repaired_input,
                        "raw_input": input_str,
                    },
                )
                if structured_model:
                    metadata = repaired_input
            if content or finish_changed:
                last_content = copy.deepcopy(content)
                result = ChatResponse(
                    id=response_id or f"chat_{uuid4().hex}",
                    content=content,
                    usage=usage,
                    metadata=metadata,
                )
                # Preserve the provider's terminal marker immediately, even
                # for thinking-only output. Tail failures must not hide it.
                # Repeat the cumulative snapshot, never an empty replacement
                # for text or tool content that has already been received.
                result.finish_reason = finish_reason
                yield result

        if not self.stream_tool_parsing and last_content and tool_calls:
            metadata = None
            for block in last_content:
                if block.get("type") != "tool_use":
                    continue
                input_obj = _json_loads_with_repair(block.get("raw_input") or "{}")
                block["input"] = input_obj if isinstance(input_obj, dict) else {}
                if structured_model:
                    metadata = block["input"]
            result = ChatResponse(
                id=response_id or f"chat_{uuid4().hex}",
                content=last_content,
                usage=usage,
                metadata=metadata,
            )
            result.finish_reason = finish_reason
            yield result


class GeminiChatModel:
    """Google Gemini API wrapper with the legacy chat model surface."""

    def __init__(
        self,
        model_name: str,
        api_key: str,
        stream: bool = True,
        thinking_config: dict[str, Any] | None = None,
        client_kwargs: dict[str, Any] | None = None,
        generate_kwargs: dict[str, Any] | None = None,
        **kwargs: Any,
    ) -> None:
        client_args = kwargs.pop("client_args", None)
        if client_args is not None and client_kwargs is not None:
            raise ValueError(
                "Cannot specify both 'client_args' and 'client_kwargs'.",
            )
        if client_args is not None:
            logger.warning(
                "'client_args' is deprecated; use 'client_kwargs' instead.",
            )
            client_kwargs = client_args
        if kwargs:
            logger.warning("Unknown keyword arguments ignored: %s", list(kwargs))
        try:
            from google import genai
        except ImportError as exc:  # pragma: no cover - dependency guard
            raise ImportError("Please install the `google-genai` package.") from exc

        self.model_name = model_name
        self.stream = stream
        self.thinking_config = thinking_config
        self.generate_kwargs = generate_kwargs or {}
        self.client = genai.Client(api_key=api_key, **(client_kwargs or {}))

    async def __call__(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        tool_choice: Literal["auto", "none", "required"] | str | None = None,
        structured_model: Type[BaseModel] | None = None,
        **config_kwargs: Any,
    ) -> ChatResponse | AsyncGenerator[ChatResponse, None]:
        config: dict[str, Any] = {
            "thinking_config": self.thinking_config,
            **self.generate_kwargs,
            **config_kwargs,
        }
        if tools:
            config["tools"] = _format_gemini_tool_schemas(tools)
        if tool_choice:
            if tool_choice == "any":
                warnings.warn(
                    '"any" is deprecated; use "required" instead.',
                    DeprecationWarning,
                    stacklevel=2,
                )
                tool_choice = "required"
            _validate_tool_choice(tool_choice, tools)
            config["tool_config"] = _format_gemini_tool_choice(tool_choice)
        if structured_model:
            if tools or tool_choice:
                logger.warning(
                    "structured_model overrides tools and tool_choice.",
                )
            config.pop("tools", None)
            config.pop("tool_config", None)
            config["response_mime_type"] = "application/json"
            config["response_schema"] = structured_model

        kwargs: dict[str, Any] = {
            "model": self.model_name,
            "contents": messages,
            "config": {key: value for key, value in config.items() if value is not None},
        }
        started_at = time.monotonic()
        if self.stream:
            response = await self.client.aio.models.generate_content_stream(**kwargs)
            return self._parse_stream_response(
                started_at,
                response,
                structured_model,
            )
        response = await self.client.aio.models.generate_content(**kwargs)
        return self._parse_response(started_at, response, structured_model)

    async def _parse_stream_response(
        self,
        started_at: float,
        response: AsyncIterator[Any],
        structured_model: Type[BaseModel] | None = None,
    ) -> AsyncGenerator[ChatResponse, None]:
        text = ""
        thinking = ""
        tool_calls: list[dict[str, Any]] = []
        metadata = None
        response_id: str | None = None
        async for chunk in response:
            candidate = (
                chunk.candidates[0]
                if getattr(chunk, "candidates", None)
                else None
            )
            parts = (
                getattr(getattr(candidate, "content", None), "parts", None)
                if candidate
                else None
            )
            for part in parts or []:
                part_text = getattr(part, "text", None)
                if part_text:
                    if getattr(part, "thought", False):
                        thinking += part_text
                    else:
                        text += part_text
                function_call = getattr(part, "function_call", None)
                if function_call:
                    args = getattr(function_call, "args", None) or {}
                    signature = _gemini_thought_signature(part)
                    call_id = getattr(function_call, "id", None) or (
                        f"call_{len(tool_calls)}"
                    )
                    tool_calls.append(
                        {
                            "type": "tool_use",
                            "id": call_id,
                            "name": getattr(function_call, "name", None),
                            "input": dict(args),
                            "raw_input": json.dumps(args, ensure_ascii=False),
                            **(
                                {
                                    "extra_content": {
                                        "thought_signature": signature,
                                    },
                                }
                                if signature
                                else {}
                            ),
                        },
                    )
            if text and structured_model:
                metadata = _json_loads_with_repair(text)
            if response_id is None:
                response_id = getattr(chunk, "response_id", None)
            content = []
            if thinking:
                content.append({"type": "thinking", "thinking": thinking})
            if text:
                content.append({"type": "text", "text": text})
            yield ChatResponse(
                id=response_id or f"chat_{uuid4().hex}",
                content=[*content, *tool_calls],
                usage=_usage_from_gemini(
                    getattr(chunk, "usage_metadata", None),
                    started_at,
                ),
                metadata=metadata,
            )

    def _parse_response(
        self,
        started_at: float,
        response: Any,
        structured_model: Type[BaseModel] | None = None,
    ) -> ChatResponse:
        content: list[dict[str, Any]] = []
        tool_calls: list[dict[str, Any]] = []
        metadata = None
        candidate = (
            response.candidates[0]
            if getattr(response, "candidates", None)
            else None
        )
        parts = (
            getattr(getattr(candidate, "content", None), "parts", None)
            if candidate
            else None
        )
        for part in parts or []:
            part_text = getattr(part, "text", None)
            if part_text:
                if getattr(part, "thought", False):
                    content.append({"type": "thinking", "thinking": part_text})
                else:
                    content.append({"type": "text", "text": part_text})
            function_call = getattr(part, "function_call", None)
            if function_call:
                args = getattr(function_call, "args", None) or {}
                signature = _gemini_thought_signature(part)
                call_id = getattr(function_call, "id", None) or (
                    f"call_{len(tool_calls)}"
                )
                tool_calls.append(
                    {
                        "type": "tool_use",
                        "id": call_id,
                        "name": getattr(function_call, "name", None),
                        "input": dict(args),
                        "raw_input": json.dumps(args, ensure_ascii=False),
                        **(
                            {
                                "extra_content": {
                                    "thought_signature": signature,
                                },
                            }
                            if signature
                            else {}
                        ),
                    },
                )
        if getattr(response, "text", None) and structured_model:
            metadata = _json_loads_with_repair(response.text)
        return ChatResponse(
            id=getattr(response, "response_id", None) or f"chat_{uuid4().hex}",
            content=[*content, *tool_calls],
            usage=_usage_from_gemini(
                getattr(response, "usage_metadata", None),
                started_at,
            ),
            metadata=metadata,
        )


def _format_anthropic_image_block(image_block: dict[str, Any]) -> dict[str, Any]:
    source = image_block["source"]
    if source["type"] == "base64":
        return {**image_block}
    url = source["url"]
    parsed_url = urlparse(url)
    if parsed_url.scheme == "file":
        raw_url = unquote(parsed_url.path)
    elif parsed_url.scheme == "":
        raw_url = unquote(url)
    else:
        raw_url = url
    if os.path.exists(raw_url) and os.path.isfile(raw_url):
        support_image_extensions = {
            ".png": "image/png",
            ".jpg": "image/jpeg",
            ".jpeg": "image/jpeg",
            ".gif": "image/gif",
            ".webp": "image/webp",
        }
        media_type = support_image_extensions.get(os.path.splitext(raw_url)[1].lower())
        if media_type:
            with open(raw_url, "rb") as image_file:
                data = base64.b64encode(image_file.read()).decode("utf-8")
            return {
                "type": "image",
                "source": {
                    "type": "base64",
                    "media_type": media_type,
                    "data": data,
                },
            }
    if parsed_url.scheme not in ("", "file"):
        return {
            "type": "image",
            "source": {"type": "url", "url": url},
        }
    raise ValueError(
        f'Invalid image URL: "{url}". It should be a local file or a web URL.',
    )


class AnthropicChatFormatter:
    """Anthropic Messages API formatter for compatibility message objects."""

    support_tools_api = True
    support_multiagent = False
    support_vision = True
    supported_blocks: list[type] = []

    def __init__(
        self,
        token_counter: Any | None = None,
        max_tokens: int | None = None,
        **_kwargs: Any,
    ) -> None:
        self.token_counter = token_counter
        self.max_tokens = max_tokens

    async def format(self, msgs: list[Any], **_kwargs: Any) -> list[dict[str, Any]]:
        self.assert_list_of_msgs(msgs)
        return await self._format(copy.deepcopy(msgs))

    @staticmethod
    def assert_list_of_msgs(msgs: list[Any]) -> None:
        if not isinstance(msgs, list):
            raise TypeError("Input must be a list of message objects.")
        for msg in msgs:
            if not hasattr(msg, "get_content_blocks"):
                raise TypeError(
                    f"Expected message object, got {type(msg)} instead.",
                )

    @staticmethod
    def convert_tool_result_to_string(
        output: str | list[dict[str, Any]],
    ) -> tuple[str, Sequence[tuple[str, dict[str, Any]]]]:
        return LantaiOpenAIChatFormatter.convert_tool_result_to_string(output)

    async def _format(self, msgs: list[Any]) -> list[dict[str, Any]]:
        messages: list[dict[str, Any]] = []
        for index, msg in enumerate(msgs):
            content_blocks = []
            for block in msg.get_content_blocks():
                typ = block.get("type")
                if typ in ["thinking", "text"]:
                    content_blocks.append({**block})
                elif typ == "image":
                    content_blocks.append(_format_anthropic_image_block(block))
                elif typ == "file":
                    content_blocks.append(
                        {
                            "type": "text",
                            "text": attachment_context_text(block),
                        },
                    )
                elif typ == "tool_use":
                    content_blocks.append(
                        {
                            "id": block.get("id"),
                            "type": "tool_use",
                            "name": block.get("name"),
                            "input": block.get("input", {}),
                        },
                    )
                elif typ == "tool_result":
                    output = block.get("output")
                    if output is None:
                        content_value = [{"type": "text", "text": None}]
                    elif isinstance(output, list):
                        content_value = [
                            _format_anthropic_image_block(item)
                            if isinstance(item, dict) and item.get("type") == "image"
                            else item
                            for item in output
                        ]
                    else:
                        content_value = [{"type": "text", "text": str(output)}]
                    messages.append(
                        {
                            "role": "user",
                            "content": [
                                {
                                    "type": "tool_result",
                                    "tool_use_id": block.get("id"),
                                    "content": content_value,
                                },
                            ],
                        },
                    )
                else:
                    logger.warning(
                        "Unsupported block type %s in message, skipped.",
                        typ,
                    )
            role = "user" if msg.role == "system" and index != 0 else msg.role
            row = {"role": role, "content": content_blocks or None}
            if row["content"]:
                messages.append(row)
        return messages


def _get_bytes_from_web_url(url: str) -> str:
    response = requests.get(url, timeout=30)
    response.raise_for_status()
    return base64.b64encode(response.content).decode("utf-8")


def _to_gemini_inline_data(url: str) -> dict[str, str]:
    raw_url = url.removeprefix("file://")
    parsed_url = urlparse(raw_url)
    extension = raw_url.split(".")[-1].lower()

    media_type = None
    for typ, extensions in GeminiChatFormatter.supported_extensions.items():
        if extension in extensions:
            media_type = f"{typ}/{extension}"
            break
    if media_type is None:
        raise TypeError(
            f"Unsupported file extension: {extension}, expected "
            f"{GeminiChatFormatter.supported_extensions}",
        )
    if os.path.exists(raw_url):
        with open(raw_url, "rb") as media_file:
            data = base64.b64encode(media_file.read()).decode("utf-8")
        return {"data": data, "mime_type": media_type}
    if parsed_url.scheme not in ["", "file"]:
        return {"data": _get_bytes_from_web_url(url), "mime_type": media_type}
    raise ValueError(
        f"The URL `{url}` is not a valid media URL or local file.",
    )


def _gemini_thought_signature(part: Any) -> str:
    signature = getattr(part, "thought_signature", None)
    if isinstance(signature, str):
        return signature
    if isinstance(signature, (bytes, bytearray)):
        return base64.b64encode(bytes(signature)).decode("utf-8")
    return ""


def _format_gemini_media_block(media_block: dict[str, Any]) -> dict[str, Any]:
    source = media_block["source"]
    if source["type"] == "base64":
        return {
            "inline_data": {
                "data": source["data"],
                "mime_type": source["media_type"],
            },
        }
    if source["type"] == "url":
        return {"inline_data": _to_gemini_inline_data(source["url"])}
    raise ValueError(f"Unsupported source type: {source['type']}")


class GeminiChatFormatter:
    """Google Gemini formatter for compatibility message objects."""

    support_tools_api = True
    support_multiagent = False
    support_vision = True
    supported_blocks: list[type] = []
    supported_extensions = {
        "image": ["png", "jpeg", "jpg", "webp", "heic", "heif"],
        "video": [
            "mp4",
            "mpeg",
            "mov",
            "avi",
            "x-flv",
            "mpg",
            "webm",
            "wmv",
            "3gpp",
        ],
        "audio": ["mp3", "wav", "aiff", "aac", "ogg", "flac"],
    }

    def __init__(
        self,
        promote_tool_result_images: bool = False,
        token_counter: Any | None = None,
        max_tokens: int | None = None,
    ) -> None:
        self.promote_tool_result_images = promote_tool_result_images
        self.token_counter = token_counter
        self.max_tokens = max_tokens

    async def format(self, msgs: list[Any], **_kwargs: Any) -> list[dict[str, Any]]:
        self.assert_list_of_msgs(msgs)
        return await self._format(copy.deepcopy(msgs))

    @staticmethod
    def assert_list_of_msgs(msgs: list[Any]) -> None:
        if not isinstance(msgs, list):
            raise TypeError("Input must be a list of message objects.")
        for msg in msgs:
            if not hasattr(msg, "get_content_blocks"):
                raise TypeError(
                    f"Expected message object, got {type(msg)} instead.",
                )

    @staticmethod
    def convert_tool_result_to_string(
        output: str | list[dict[str, Any]],
    ) -> tuple[str, Sequence[tuple[str, dict[str, Any]]]]:
        return LantaiOpenAIChatFormatter.convert_tool_result_to_string(output)

    async def _format(self, msgs: list[Any]) -> list[dict[str, Any]]:
        messages = []
        index = 0
        while index < len(msgs):
            msg = msgs[index]
            parts = []
            for block in msg.get_content_blocks():
                typ = block.get("type")
                if typ == "text":
                    parts.append({"text": block.get("text")})
                elif typ == "file":
                    parts.append({"text": attachment_context_text(block)})
                elif typ == "tool_use":
                    extra_content = block.get("extra_content")
                    thought_signature = (
                        extra_content.get("thought_signature")
                        if isinstance(extra_content, dict)
                        else None
                    )
                    parts.append(
                        {
                            "function_call": {
                                "id": block.get("id"),
                                "name": block["name"],
                                "args": block["input"],
                            },
                            # Old histories stored the signature in ``id``.
                            # Keep them replayable while all new histories use
                            # the explicit opaque field above.
                            "thought_signature": (
                                thought_signature or block.get("id")
                            ),
                        },
                    )
                elif typ == "tool_result":
                    textual_output, multimodal_data = (
                        self.convert_tool_result_to_string(block["output"])
                    )
                    messages.append(
                        {
                            "role": "user",
                            "parts": [
                                {
                                    "function_response": {
                                        "id": block["id"],
                                        "name": block["name"],
                                        "response": {"output": textual_output},
                                    },
                                },
                            ],
                        },
                    )
                    promoted_blocks = []
                    for url, multimodal_block in multimodal_data:
                        if (
                            multimodal_block["type"] == "image"
                            and self.promote_tool_result_images
                        ):
                            promoted_blocks.extend(
                                [
                                    {
                                        "type": "text",
                                        "text": f"\n- The image from '{url}': ",
                                    },
                                    {
                                        "type": "image",
                                        "source": {"type": "url", "url": url},
                                    },
                                ],
                            )
                    if promoted_blocks:
                        msgs.insert(
                            index + 1,
                            _MessageShim(
                                name="user",
                                role="user",
                                content=[
                                    {
                                        "type": "text",
                                        "text": (
                                            "<system-info>The following are "
                                            "the image contents from the tool "
                                            f"result of '{block['name']}':"
                                        ),
                                    },
                                    *promoted_blocks,
                                    {"type": "text", "text": "</system-info>"},
                                ],
                            ),
                        )
                elif typ in ["image", "audio", "video"]:
                    parts.append(_format_gemini_media_block(block))
                else:
                    logger.warning(
                        "Unsupported block type %s in message, skipped.",
                        typ,
                    )
            role = "model" if msg.role == "assistant" else "user"
            if parts:
                messages.append({"role": role, "parts": parts})
            index += 1
        return messages


__all__ = [
    "AnthropicChatFormatter",
    "AnthropicChatModel",
    "ChatResponse",
    "ChatUsage",
    "GeminiChatFormatter",
    "GeminiChatModel",
]
