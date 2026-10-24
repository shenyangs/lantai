# -*- coding: utf-8 -*-
"""Legacy model adapter behind LantaiModelClient."""

from __future__ import annotations

from lantai.utils.async_iterator import closing_async_iterator

from collections.abc import AsyncGenerator, AsyncIterator
from typing import Any
from uuid import uuid4

from ...providers.model_endpoint import ProviderModelEndpoint
from ...providers.reasoning import (
    apply_current_reasoning_options,
    get_reasoning_capability,
)
from ...providers.reasoning_output import (
    ReasoningContentParser,
    ReasoningSegment,
    resolve_reasoning_output_profile,
)
from ...providers.stream_normalizer import (
    ProviderStreamNormalizer,
    provider_stream_profile,
)
from ..context_guard import prepare_model_request_for_context
from .legacy_model_messages import legacy_msgs_from_model_messages
from ..models import (
    ModelContentBlock,
    ModelMessage,
    ModelRequest,
    ModelResponse,
    ModelStreamEvent,
)
from ..tools import ToolCall
from .openai_model_client import _provider_metadata, normalize_model_usage


def _chat_response_content(response: Any) -> list[dict[str, Any]]:
    content = getattr(response, "content", None)
    if isinstance(content, list):
        return [block for block in content if isinstance(block, dict)]
    if isinstance(content, str):
        return [{"type": "text", "text": content}]
    return []


def _tool_call_from_legacy_block(block: dict[str, Any]) -> ToolCall | None:
    if str(block.get("type") or "") != "tool_use":
        return None
    name = str(block.get("name") or "")
    if not name:
        return None
    raw_arguments = block.get("input", {})
    arguments = (
        dict(raw_arguments)
        if isinstance(raw_arguments, dict)
        else {"_raw": raw_arguments}
    )
    return ToolCall(
        id=str(block.get("id") or f"legacy_tool_{uuid4().hex}"),
        name=name,
        arguments=arguments,
        metadata={"source": "legacy_model_stream"},
    )


def _model_block_from_legacy_block(block: dict[str, Any]) -> ModelContentBlock:
    block_type = str(block.get("type") or "text")
    if block_type == "text":
        return ModelContentBlock(type="text", text=str(block.get("text") or ""))
    if block_type == "thinking":
        return ModelContentBlock(
            type="thinking",
            text=str(block.get("thinking") or block.get("text") or ""),
            data={key: value for key, value in block.items() if key != "type"},
        )
    if block_type == "tool_use":
        return ModelContentBlock(
            type="tool_call",
            data={
                "id": block.get("id"),
                "name": block.get("name"),
                "arguments": block.get("input", {}),
            },
        )
    return ModelContentBlock(
        type="text",
        text=str(block.get("text") or block),
        metadata={"legacy_type": block_type},
    )


def _legacy_text_from_stream_block(block: dict[str, Any]) -> str:
    block_type = str(block.get("type") or "")
    if block_type == "thinking":
        return str(block.get("thinking") or block.get("text") or "")
    if block_type == "text":
        return str(block.get("text") or "")
    return ""


def _stream_text_block_key(
    block_type: str,
    occurrence_by_type: dict[str, int],
) -> str:
    index = occurrence_by_type.get(block_type, 0)
    occurrence_by_type[block_type] = index + 1
    return f"{block_type}:{index}"


def _final_text_block(
    *,
    block_type: str,
    text: str,
    source: dict[str, Any],
) -> ModelContentBlock:
    if block_type == "thinking":
        data = {key: value for key, value in source.items() if key != "type"}
        data["thinking"] = text
        return ModelContentBlock(type="thinking", text=text, data=data)
    return ModelContentBlock(type="text", text=text)


def _tool_block_key(block: dict[str, Any], *, fallback_index: int) -> str:
    tool_id = block.get("id")
    if tool_id:
        return f"tool:{tool_id}"
    return f"tool:{block.get('name') or ''}:{fallback_index}"


def _append_inline_segment(
    segment: ReasoningSegment,
    *,
    final_blocks: dict[str, ModelContentBlock],
    final_block_order: list[str],
) -> None:
    key = f"inline:{segment.kind}"
    previous = final_blocks.get(key)
    if key not in final_blocks:
        final_block_order.append(key)
    final_blocks[key] = ModelContentBlock(
        type=segment.kind,
        text=(previous.text if previous is not None else "") + segment.text,
    )


def _message_from_chat_response(
    response: Any,
    *,
    message_id: str,
) -> ModelMessage:
    blocks = [
        _model_block_from_legacy_block(block)
        for block in _chat_response_content(response)
    ]
    return ModelMessage(id=message_id, role="assistant", content=blocks)


def _is_async_stream(value: Any) -> bool:
    return isinstance(value, AsyncGenerator) or hasattr(value, "__aiter__")


class LegacyModelClient:
    """Adapter for Anthropic/Gemini and unsupported providers during Phase 4."""

    def __init__(
        self,
        *,
        endpoint: ProviderModelEndpoint,
        model: Any,
        formatter: Any,
    ) -> None:
        self.endpoint = endpoint
        self.provider_id = endpoint.provider_id
        self.model_name = endpoint.model_id
        self._model = model
        self._formatter = formatter

    async def _call_model(
        self,
        request: ModelRequest,
    ) -> Any | AsyncGenerator[Any, None]:
        request = prepare_model_request_for_context(request, self.endpoint)
        msgs = legacy_msgs_from_model_messages(request.messages)
        formatted = await self._formatter.format(msgs)
        tools = None
        if request.tools:
            tools = [
                {
                    "type": "function",
                    "function": {
                        "name": tool.name,
                        "description": tool.description,
                        "parameters": tool.input_schema
                        or {"type": "object", "properties": {}},
                    },
                }
                for tool in request.tools
                if tool.enabled
            ]
        kwargs = dict(request.generation)
        kwargs = apply_current_reasoning_options(
            kwargs,
            provider_id=self.endpoint.provider_id,
            model_id=request.model or self.endpoint.model_id,
            base_url=self.endpoint.base_url,
            chat_model=self.endpoint.chat_model,
        )
        reasoning_capability = get_reasoning_capability(
            self.endpoint.provider_id,
            request.model or self.endpoint.model_id,
            base_url=self.endpoint.base_url,
            chat_model=self.endpoint.chat_model,
        )
        if (
            request.tool_choice == "required"
            and reasoning_capability.mode == "minimax_adaptive_thinking"
            and not reasoning_capability.fixed
        ):
            # MiniMax M3 may answer from its adaptive-thinking path instead of
            # honoring Anthropic's forced tool choice.  A required tool round
            # is deterministic orchestration, so disable thinking for this
            # call only; the later synthesis round restores the user setting.
            kwargs["thinking"] = {"type": "disabled"}
        tool_choice = request.tool_choice
        if tool_choice == "auto":
            tool_choice = None
        return await self._model(
            messages=formatted,
            tools=tools,
            tool_choice=tool_choice,
            **kwargs,
        )

    async def stream(
        self,
        request: ModelRequest,
    ) -> AsyncIterator[ModelStreamEvent]:
        message_id = request.metadata.get("message_id") or f"msg_{uuid4().hex}"
        result = await self._call_model(request)
        async with closing_async_iterator(result):
            yield ModelStreamEvent(
                kind="message_start",
                message_id=message_id,
                metadata={
                    "provider": _provider_metadata(
                        self.endpoint,
                        client_class=self.__class__.__name__,
                        model_class=self._model.__class__.__name__,
                    ),
                },
            )

            last_response: Any | None = None
            last_usage = None
            final_blocks: dict[str, ModelContentBlock] = {}
            final_block_order: list[str] = []
            final_tool_blocks: dict[str, dict[str, Any]] = {}
            stream_profile = provider_stream_profile(
                provider_id=self.endpoint.provider_id,
                model_id=request.model or self.endpoint.model_id,
                family=self.endpoint.family,
            )
            provider_normalizer = ProviderStreamNormalizer(stream_profile)
            reasoning_output_profile = resolve_reasoning_output_profile(
                self.endpoint.provider_id,
                request.model or self.endpoint.model_id,
                base_url=self.endpoint.base_url,
                chat_model=self.endpoint.chat_model,
            )
            reasoning_content_parser = ReasoningContentParser(
                reasoning_output_profile,
            )
            if _is_async_stream(result):
                async for chunk in result:
                    chunk_emitted_event = False
                    last_response = chunk
                    usage = normalize_model_usage(
                        getattr(chunk, "usage", None)
                    )
                    if usage is not None:
                        last_usage = usage
                    content_blocks = _chat_response_content(chunk)
                    deduplicate_pre_tool_snapshot = (
                        stream_profile.deduplicate_snapshot_blocks
                        and any(
                            str(block.get("type") or "") == "tool_use"
                            for block in content_blocks
                        )
                    )
                    occurrence_by_type: dict[str, int] = {}
                    seen_text_blocks: set[tuple[str, str]] = set()
                    for block_index, block in enumerate(content_blocks):
                        block_type = str(block.get("type") or "text")
                        if block_type in {"thinking", "text"}:
                            current = _legacy_text_from_stream_block(block)
                            snapshot_identity = (block_type, current)
                            if (
                                deduplicate_pre_tool_snapshot
                                and snapshot_identity in seen_text_blocks
                            ):
                                # MiniMax-compatible streams can repeat the exact
                                # same cumulative text block twice in one chunk
                                # immediately before a tool call.  It is one
                                # snapshot, not two assistant messages.
                                continue
                            if deduplicate_pre_tool_snapshot:
                                seen_text_blocks.add(snapshot_identity)
                            key = _stream_text_block_key(
                                block_type, occurrence_by_type
                            )
                            channel = (
                                "thinking"
                                if block_type == "thinking"
                                else "text"
                            )
                            delta = provider_normalizer.text_delta(
                                current,
                                channel=channel,
                                block_identity=key,
                            )
                            next_full = provider_normalizer.full_text(
                                channel=channel,
                                block_identity=key,
                            )
                            if (
                                block_type == "text"
                                and reasoning_output_profile.inline_tag_pairs
                            ):
                                for segment in reasoning_content_parser.feed(
                                    delta
                                ):
                                    _append_inline_segment(
                                        segment,
                                        final_blocks=final_blocks,
                                        final_block_order=final_block_order,
                                    )
                                    chunk_emitted_event = True
                                    yield ModelStreamEvent(
                                        kind=(
                                            "thinking_delta"
                                            if segment.kind == "thinking"
                                            else "text_delta"
                                        ),
                                        message_id=message_id,
                                        delta=segment.text,
                                        content_block=ModelContentBlock(
                                            type=segment.kind,
                                            text=segment.text,
                                        ),
                                    )
                                continue
                            if key not in final_blocks:
                                final_block_order.append(key)
                            final_blocks[key] = _final_text_block(
                                block_type=block_type,
                                text=next_full,
                                source=block,
                            )
                        elif block_type == "tool_use":
                            key = _tool_block_key(
                                block, fallback_index=block_index
                            )
                            if key not in final_blocks:
                                final_block_order.append(key)
                            final_blocks[key] = _model_block_from_legacy_block(
                                block
                            )
                            final_tool_blocks[key] = block
                            continue
                        else:
                            key = f"{block_type}:{block_index}"
                            if key not in final_blocks:
                                final_block_order.append(key)
                            final_blocks[key] = _model_block_from_legacy_block(
                                block
                            )
                            continue

                        if block_type == "thinking":
                            if delta:
                                chunk_emitted_event = True
                                yield ModelStreamEvent(
                                    kind="thinking_delta",
                                    message_id=message_id,
                                    delta=delta,
                                    content_block=ModelContentBlock(
                                        type="thinking",
                                        text=delta,
                                    ),
                                )
                        elif block_type == "text":
                            if delta:
                                chunk_emitted_event = True
                                yield ModelStreamEvent(
                                    kind="text_delta",
                                    message_id=message_id,
                                    delta=delta,
                                    content_block=ModelContentBlock(
                                        type="text",
                                        text=delta,
                                    ),
                                )
                    if not chunk_emitted_event:
                        yield ModelStreamEvent(
                            kind="provider_activity",
                            message_id=message_id,
                            metadata={"provider_chunk": True},
                        )
                for segment in reasoning_content_parser.flush():
                    _append_inline_segment(
                        segment,
                        final_blocks=final_blocks,
                        final_block_order=final_block_order,
                    )
                    yield ModelStreamEvent(
                        kind=(
                            "thinking_delta"
                            if segment.kind == "thinking"
                            else "text_delta"
                        ),
                        message_id=message_id,
                        delta=segment.text,
                        content_block=ModelContentBlock(
                            type=segment.kind,
                            text=segment.text,
                        ),
                    )
                response = (
                    ModelResponse(
                        message=ModelMessage(
                            id=message_id,
                            role="assistant",
                            content=[
                                final_blocks[key]
                                for key in final_block_order
                                if key in final_blocks
                            ],
                        ),
                        usage=last_usage,
                    )
                    if last_response is not None
                    else ModelResponse(
                        message=ModelMessage(id=message_id, role="assistant"),
                    )
                )
                for key in final_block_order:
                    tool_call = _tool_call_from_legacy_block(
                        final_tool_blocks.get(key, {}),
                    )
                    if tool_call is not None:
                        yield ModelStreamEvent(
                            kind="tool_call_completed",
                            message_id=message_id,
                            tool_call=tool_call,
                        )
            else:
                if reasoning_output_profile.inline_tag_pairs:
                    for block_index, block in enumerate(
                        _chat_response_content(result),
                    ):
                        block_type = str(block.get("type") or "text")
                        if block_type == "text":
                            for segment in reasoning_content_parser.feed(
                                _legacy_text_from_stream_block(block),
                            ):
                                _append_inline_segment(
                                    segment,
                                    final_blocks=final_blocks,
                                    final_block_order=final_block_order,
                                )
                                yield ModelStreamEvent(
                                    kind=(
                                        "thinking_delta"
                                        if segment.kind == "thinking"
                                        else "text_delta"
                                    ),
                                    message_id=message_id,
                                    delta=segment.text,
                                    content_block=ModelContentBlock(
                                        type=segment.kind,
                                        text=segment.text,
                                    ),
                                )
                            continue
                        key = f"{block_type}:{block_index}"
                        final_block_order.append(key)
                        final_blocks[key] = _model_block_from_legacy_block(
                            block
                        )
                    for segment in reasoning_content_parser.flush():
                        _append_inline_segment(
                            segment,
                            final_blocks=final_blocks,
                            final_block_order=final_block_order,
                        )
                        yield ModelStreamEvent(
                            kind=(
                                "thinking_delta"
                                if segment.kind == "thinking"
                                else "text_delta"
                            ),
                            message_id=message_id,
                            delta=segment.text,
                            content_block=ModelContentBlock(
                                type=segment.kind,
                                text=segment.text,
                            ),
                        )
                    response = ModelResponse(
                        message=ModelMessage(
                            id=message_id,
                            role="assistant",
                            content=[
                                final_blocks[key]
                                for key in final_block_order
                                if key in final_blocks
                            ],
                        ),
                        usage=normalize_model_usage(
                            getattr(result, "usage", None)
                        ),
                    )
                else:
                    response = ModelResponse(
                        message=_message_from_chat_response(
                            result,
                            message_id=message_id,
                        ),
                        usage=normalize_model_usage(
                            getattr(result, "usage", None)
                        ),
                    )

            source_metadata = getattr(
                last_response or result, "metadata", None
            )
            compatibility = (
                source_metadata.get("tool_choice_compatibility")
                if isinstance(source_metadata, dict)
                else None
            )
            if isinstance(compatibility, dict):
                response.message.metadata["tool_choice_compatibility"] = dict(
                    compatibility
                )

            yield ModelStreamEvent(
                kind="message_completed",
                message_id=message_id,
                response=response,
                usage=response.usage,
            )

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
