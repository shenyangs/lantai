# -*- coding: utf-8 -*-
"""Native adapter for OpenAI Responses-compatible endpoints."""

from __future__ import annotations

from lantai.utils.async_iterator import closing_async_iterator

from collections.abc import AsyncIterator
import json
from typing import Any
from uuid import uuid4

from openai import AsyncOpenAI

from ...providers.model_endpoint import ProviderModelEndpoint
from ...providers.openai_http import (
    OPENAI_SDK_MAX_RETRIES,
    create_openai_async_http_client,
    openai_endpoint_policy_receipt,
)
from ..instruction_trust import project_untrusted_tool_evidence
from ..model_normalizer import media_payload_url
from ..models import (
    ModelContentBlock,
    ModelMessage,
    ModelRequest,
    ModelResponse,
    ModelStreamEvent,
    ModelUsage,
)
from ..tools import ToolCall


def _responses_input(request: ModelRequest) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    for message in request.messages:
        text = message.text_content()
        multimodal_content: list[dict[str, Any]] = []
        if text:
            multimodal_content.append({"type": "input_text", "text": text})
        for block in message.content:
            if block.type == "image":
                image_url = media_payload_url(block.type, block.data)
                if image_url:
                    multimodal_content.append(
                        {"type": "input_image", "image_url": image_url}
                    )
        has_media = any(
            part.get("type") != "input_text" for part in multimodal_content
        )
        if has_media:
            items.append({"role": message.role, "content": multimodal_content})
        elif text:
            items.append({"role": message.role, "content": text})
        for block in message.content:
            if block.type == "thinking":
                raw_item = block.data.get("openai_response_item")
                if isinstance(raw_item, dict):
                    items.append(dict(raw_item))
            elif block.type == "tool_call":
                call_id = str(block.data.get("id") or message.id or "")
                items.append(
                    {
                        "type": "function_call",
                        "call_id": call_id,
                        "name": str(block.data.get("name") or message.name or ""),
                        "arguments": json.dumps(
                            block.data.get("arguments") or {},
                            ensure_ascii=False,
                            separators=(",", ":"),
                        ),
                    }
                )
            elif block.type == "tool_result":
                call_id = str(
                    block.data.get("id")
                    or message.tool_call_id
                    or message.id
                    or ""
                )
                name = str(block.data.get("name") or message.name or "")
                items.append(
                    {
                        "type": "function_call_output",
                        "call_id": call_id,
                        "output": project_untrusted_tool_evidence(
                            name,
                            _tool_output_text(block.data.get("output")),
                        ),
                    }
                )
    return items


def _tool_output_text(output: Any) -> str:
    if isinstance(output, str):
        return output
    if not isinstance(output, list):
        return json.dumps(output, ensure_ascii=False, default=str)
    parts: list[str] = []
    for item in output:
        if isinstance(item, dict) and str(item.get("type") or "") == "text":
            parts.append(str(item.get("text") or ""))
        else:
            parts.append(json.dumps(item, ensure_ascii=False, default=str))
    return "\n".join(part for part in parts if part)


def _responses_tools(request: ModelRequest) -> list[dict[str, Any]]:
    return [
        {
            "type": "function",
            "name": tool.name,
            "description": tool.description,
            "parameters": tool.input_schema
            or {"type": "object", "properties": {}},
        }
        for tool in request.tools
        if tool.enabled
    ]


def _tool_arguments(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return dict(value)
    if not value:
        return {}
    try:
        parsed = json.loads(str(value))
    except (TypeError, ValueError):
        return {"__raw_arguments__": str(value)}
    return parsed if isinstance(parsed, dict) else {"value": parsed}


def _response_item_dict(item: Any) -> dict[str, Any]:
    if item is None:
        return {}
    model_dump = getattr(item, "model_dump", None)
    if callable(model_dump):
        try:
            dumped = model_dump(mode="json", exclude_none=True)
        except TypeError:
            dumped = model_dump()
        return dict(dumped) if isinstance(dumped, dict) else {}
    if isinstance(item, dict):
        return dict(item)
    return {
        key: value
        for key, value in vars(item).items()
        if not key.startswith("_") and value is not None
    }


def _reasoning_summary(item: dict[str, Any]) -> str:
    summary = item.get("summary")
    if not isinstance(summary, list):
        return ""
    return "\n".join(
        str(part.get("text") or "")
        for part in summary
        if isinstance(part, dict) and part.get("text")
    )


def _usage(value: Any) -> ModelUsage | None:
    if value is None:
        return None
    input_tokens = int(getattr(value, "input_tokens", 0) or 0)
    output_tokens = int(getattr(value, "output_tokens", 0) or 0)
    total_tokens = int(
        getattr(value, "total_tokens", 0) or input_tokens + output_tokens,
    )
    return ModelUsage(
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        total_tokens=total_tokens,
    )


class OpenAIResponsesModelClient:
    """Lantai model client for the OpenAI Responses wire protocol."""

    def __init__(self, endpoint: ProviderModelEndpoint) -> None:
        self.endpoint = endpoint
        self.provider_id = endpoint.provider_id
        self.model_name = endpoint.model_id
        self.model_transport_policy_receipt = openai_endpoint_policy_receipt(
            provider_id=endpoint.provider_id,
            model_id=endpoint.model_id,
            base_url=endpoint.base_url,
            protocol="openai_responses",
        )
        kwargs: dict[str, Any] = {
            "base_url": endpoint.base_url,
            "api_key": endpoint.api_key,
            "http_client": create_openai_async_http_client(),
            "max_retries": OPENAI_SDK_MAX_RETRIES,
        }
        if endpoint.default_headers:
            kwargs["default_headers"] = endpoint.default_headers
        self._client = AsyncOpenAI(**kwargs)

    async def stream(
        self,
        request: ModelRequest,
    ) -> AsyncIterator[ModelStreamEvent]:
        message_id = request.metadata.get("message_id") or f"msg_{uuid4().hex}"
        payload: dict[str, Any] = {
            "model": request.model or self.endpoint.model_id,
            "input": _responses_input(request),
            "stream": True,
        }
        tools = _responses_tools(request)
        if tools:
            payload["tools"] = tools
            if request.tool_choice is not None:
                payload["tool_choice"] = request.tool_choice
        payload.update(self.endpoint.generate_kwargs)
        payload.update(request.generation)
        response_stream = await self._client.responses.create(**payload)
        async with closing_async_iterator(response_stream):
            yield ModelStreamEvent(kind="message_start", message_id=message_id)
            text_parts: list[str] = []
            usage: ModelUsage | None = None
            finish_reason: str | None = None
            protocol_completed = False
            tool_calls: dict[str, dict[str, str]] = {}
            content_blocks: list[ModelContentBlock] = []
            async for event in response_stream:
                event_type = str(getattr(event, "type", "") or "")
                emitted_semantic_event = False
                if event_type == "response.output_text.delta":
                    delta = str(getattr(event, "delta", "") or "")
                    if delta:
                        text_parts.append(delta)
                        emitted_semantic_event = True
                        yield ModelStreamEvent(
                            kind="text_delta",
                            message_id=message_id,
                            delta=delta,
                            content_block=ModelContentBlock(
                                type="text", text=delta
                            ),
                        )
                elif event_type == "response.output_item.added":
                    item = getattr(event, "item", None)
                    if str(getattr(item, "type", "") or "") == "function_call":
                        item_id = str(getattr(item, "id", "") or "")
                        tool_calls[item_id] = {
                            "id": str(
                                getattr(item, "call_id", "")
                                or item_id
                                or f"call_{uuid4().hex}"
                            ),
                            "name": str(getattr(item, "name", "") or ""),
                            "arguments": str(
                                getattr(item, "arguments", "") or ""
                            ),
                        }
                elif event_type == "response.function_call_arguments.delta":
                    item_id = str(getattr(event, "item_id", "") or "")
                    delta = str(getattr(event, "delta", "") or "")
                    state = tool_calls.setdefault(
                        item_id,
                        {
                            "id": item_id or f"call_{uuid4().hex}",
                            "name": "",
                            "arguments": "",
                        },
                    )
                    state["arguments"] += delta
                    if delta:
                        emitted_semantic_event = True
                        yield ModelStreamEvent(
                            kind="tool_call_delta",
                            message_id=message_id,
                            delta=delta,
                            raw={"item_id": item_id},
                        )
                elif event_type == "response.output_item.done":
                    item = getattr(event, "item", None)
                    item_type = str(getattr(item, "type", "") or "")
                    if item_type == "reasoning":
                        raw_item = _response_item_dict(item)
                        summary = _reasoning_summary(raw_item)
                        block = ModelContentBlock(
                            type="thinking",
                            text=summary or None,
                            data={"openai_response_item": raw_item},
                        )
                        content_blocks.append(block)
                        if summary:
                            emitted_semantic_event = True
                            yield ModelStreamEvent(
                                kind="thinking_delta",
                                message_id=message_id,
                                delta=summary,
                                content_block=block,
                            )
                    elif item_type == "function_call":
                        item_id = str(getattr(item, "id", "") or "")
                        state = tool_calls.pop(item_id, {})
                        call_id = str(
                            getattr(item, "call_id", "")
                            or state.get("id")
                            or item_id
                            or f"call_{uuid4().hex}"
                        )
                        name = str(
                            getattr(item, "name", "")
                            or state.get("name")
                            or ""
                        )
                        raw_arguments = str(
                            getattr(item, "arguments", "")
                            or state.get("arguments")
                            or ""
                        )
                        arguments = _tool_arguments(raw_arguments)
                        tool_call = ToolCall(
                            id=call_id,
                            name=name,
                            arguments=arguments,
                        )
                        content_blocks.append(
                            ModelContentBlock(
                                type="tool_call",
                                data={
                                    "id": call_id,
                                    "name": name,
                                    "arguments": arguments,
                                },
                            )
                        )
                        emitted_semantic_event = True
                        yield ModelStreamEvent(
                            kind="tool_call_completed",
                            message_id=message_id,
                            tool_call=tool_call,
                        )
                elif event_type == "response.completed":
                    protocol_completed = True
                    response = getattr(event, "response", None)
                    usage = _usage(getattr(response, "usage", None))
                    finish_reason = str(
                        getattr(response, "status", "completed")
                        or "completed",
                    )
                    if usage is not None:
                        emitted_semantic_event = True
                        yield ModelStreamEvent(
                            kind="usage",
                            message_id=message_id,
                            usage=usage,
                        )
                elif event_type in {"response.failed", "response.incomplete"}:
                    response = getattr(event, "response", None)
                    status = str(getattr(response, "status", "") or event_type)
                    raise RuntimeError(
                        "Provider Responses stream reached a non-success terminal "
                        f"state ({status}); partial text and tool arguments were "
                        "not committed as a completed model message."
                    )
                elif event_type == "error":
                    code = str(getattr(event, "code", "") or "provider_error")
                    message = str(getattr(event, "message", "") or "")
                    raise RuntimeError(
                        "Provider Responses stream reported an error "
                        f"({code}): {message}"
                    )
                if not emitted_semantic_event:
                    # The SDK decoded a real provider event, but it carried no
                    # user-visible semantic delta.  Surface that distinction so
                    # Native idle detection does not mistake upstream activity
                    # for a dead connection.  Raw SSE comments/pings swallowed by
                    # the SDK never reach this layer and are intentionally not
                    # fabricated here.
                    yield ModelStreamEvent(
                        kind="provider_activity",
                        message_id=message_id,
                        metadata={
                            "provider_event": True,
                            "provider_event_type": event_type or "unknown",
                            "semantic_output": False,
                        },
                    )

            if not protocol_completed:
                raise RuntimeError(
                    "Provider Responses stream ended before a protocol terminal "
                    "response.completed event; partial text and tool arguments "
                    "were not committed as a completed model message."
                )

            if text_parts:
                content_blocks.insert(
                    0,
                    ModelContentBlock(type="text", text="".join(text_parts)),
                )
            message = ModelMessage(
                id=message_id,
                role="assistant",
                content=content_blocks,
            )
            response = ModelResponse(
                message=message,
                usage=usage,
                finish_reason=finish_reason,
            )
            yield ModelStreamEvent(
                kind="message_completed",
                message_id=message_id,
                response=response,
                usage=usage,
                finish_reason=finish_reason,
            )

    async def complete(self, request: ModelRequest) -> ModelResponse:
        completed: ModelResponse | None = None
        async for event in self.stream(request):
            if event.response is not None:
                completed = event.response
        if completed is None:
            return ModelResponse(
                message=ModelMessage(
                    id=f"msg_{uuid4().hex}",
                    role="assistant",
                ),
            )
        return completed
