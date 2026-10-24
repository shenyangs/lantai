# -*- coding: utf-8 -*-
"""Bounded negotiation of forced tool choice at the provider boundary.

The caller keeps its required-tool contract. Only an explicit parameter
rejection before model output permits replay with the provider's default
choice. Successful observations live in this client's bounded memory cache;
they never modify provider configuration or contain prompts or responses.
"""

from __future__ import annotations

import inspect
import json
import re
from collections import OrderedDict
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import aclosing
from copy import deepcopy
from hashlib import sha256
from typing import Any

_ERROR_LIMIT = 8192
_PARAMETER_ERRORS = {
    "invalid_parameter_error",
    "invalid_request_error",
    "unsupported_parameter",
    "unsupported_value",
}
_UNSUPPORTED_CHOICE = re.compile(
    r"(?:unsupported|unrecognized|unknown|not supported)\s+"
    r"(?:parameter\s*:?\s*)?[\"']?tool_choice\b"
    r"|\btool_choice\b[\"']?\s*(?:parameter\s*)?"
    r"(?:(?:=|:)?\s*[\"']?required[\"']?\s*)?"
    r"(?:does not support|is not supported|not supported|is unsupported|"
    r"is not allowed|cannot be set to [\"']?required\b|"
    r"only supports? [\"']?auto\b|must be(?: set to)? [\"']?auto\b)"
    r"|[\"']?required[\"']?\s+(?:is not supported|is unsupported|"
    r"is not allowed)\s+(?:for|with|as)\s+[\"']?tool_choice\b",
    re.IGNORECASE,
)


def _value(obj: Any, field: str, default: Any = None) -> Any:
    return (
        obj.get(field, default)
        if isinstance(obj, dict)
        else getattr(obj, field, default)
    )


def _parameter_rejection(exc: Exception) -> bool:
    """Accept a provider HTTP/SSE error, never an arbitrary exception sentence."""
    if isinstance(exc, (TimeoutError, ConnectionError)):
        return False
    status = getattr(exc, "status_code", None)
    if status is not None and status not in (400, 422):
        return False
    body = getattr(exc, "body", None)
    if not isinstance(body, dict):
        raw = str(exc)[:_ERROR_LIMIT].strip()
        if raw.startswith("data:"):
            raw = raw[5:].strip()
        try:
            body = json.loads(raw)
        except (ValueError, TypeError):
            return False
        # Plain JSON in a local exception must still be a provider error frame.
        if not isinstance(body, dict) or not isinstance(body.get("error"), dict):
            return False
    error = body.get("error", body)
    if not isinstance(error, dict):
        return False
    if status is None and not any(
        error.get(key) in _PARAMETER_ERRORS for key in ("code", "type")
    ):
        return False
    message = str(error.get("message") or "")[:_ERROR_LIMIT]
    if error.get("param") == "tool_choice":
        if any(
            error.get(key) in {"unsupported_parameter", "unsupported_value"}
            for key in ("code", "type")
        ):
            return True
        return bool(
            re.search(
                r"unsupported|not support|not allowed|only supports? [\"']?auto\b|"
                r"only [\"']?auto[\"']? (?:is )?supported",
                message,
                re.IGNORECASE,
            )
        )
    return bool(_UNSUPPORTED_CHOICE.search(message))


def _output_started(raw: Any) -> bool:
    chunk = _value(raw, "chunk", raw)
    if _value(chunk, "usage") is not None:
        return True
    extra = _value(chunk, "model_extra") or {}
    if _value(extra, "x_lantai_transport_activity") == "sse_done":
        return True
    for choice in _value(chunk, "choices", []) or []:
        if _value(choice, "finish_reason"):
            return True
        delta = _value(choice, "delta") or _value(choice, "message")
        if any(
            _value(delta, field)
            for field in (
                "content",
                "reasoning_content",
                "reasoning",
                "thinking",
                "reasoning_details",
                "analysis",
                "tool_calls",
                "function_call",
                "refusal",
                "audio",
            )
        ):
            return True
    return False


def _terminal(raw: Any) -> bool:
    chunk = _value(raw, "chunk", raw)
    extra = _value(chunk, "model_extra") or {}
    return _value(extra, "x_lantai_transport_activity") == "sse_done" or any(
        _value(choice, "finish_reason") for choice in _value(chunk, "choices", []) or []
    )


async def _stream_items(response: Any) -> AsyncIterator[Any]:
    if hasattr(response, "__aenter__"):
        async with response as stream:
            async for chunk in stream:
                yield chunk
    else:
        try:
            async for chunk in response:
                yield chunk
        finally:
            close = getattr(response, "aclose", None) or getattr(
                response, "close", None
            )
            if close is not None:
                result = close()
                if inspect.isawaitable(result):
                    await result


class ToolChoiceCompatibility:
    """Client-local observations scoped to endpoint, model and wire options."""

    def __init__(self) -> None:
        self._observations: OrderedDict[str, None] = OrderedDict()

    def _remember(self, key: str) -> None:
        self._observations[key] = None
        self._observations.move_to_end(key)
        while len(self._observations) > 128:
            self._observations.popitem(last=False)

    async def create(
        self,
        create: Callable[..., Awaitable[Any]],
        payload: dict[str, Any],
        *,
        provider_id: str,
        base_url: str,
        allow_recovery: bool = True,
    ) -> ToolChoiceCall:
        # All non-content options participate: new reasoning switches cannot
        # accidentally reuse an observation from a different thinking mode.
        scope = {
            key: value
            for key, value in payload.items()
            if key not in {"messages", "tools", "tool_choice"}
        }
        key = sha256(
            json.dumps(
                [provider_id, base_url, scope],
                sort_keys=True,
                default=str,
            ).encode()
        ).hexdigest()
        eligible = payload.get("tool_choice") == "required" and bool(
            payload.get("tools")
        )
        cached = allow_recovery and eligible and key in self._observations
        if cached:
            self._observations.move_to_end(key)
        call = ToolChoiceCall(
            owner=self,
            key=key,
            create=create,
            payload=deepcopy(payload),
            eligible=eligible,
            cached=cached,
            allow_recovery=allow_recovery,
        )
        if cached:
            call.payload.pop("tool_choice", None)
        await call.start()
        return call


class ToolChoiceCall:
    def __init__(
        self,
        *,
        owner: ToolChoiceCompatibility,
        key: str,
        create: Callable[..., Awaitable[Any]],
        payload: dict[str, Any],
        eligible: bool,
        cached: bool,
        allow_recovery: bool,
    ) -> None:
        self._owner, self._key, self._create = owner, key, create
        self.payload = payload
        self._eligible, self._cached = eligible, cached
        self._allow_recovery = allow_recovery
        self._attempts = 0
        self.response: Any = None

    def _recover(self, exc: Exception) -> bool:
        if (
            not self._allow_recovery
            or not self._eligible
            or self._cached
            or self._attempts
            or not _parameter_rejection(exc)
        ):
            return False
        self._attempts = 1
        self.payload.pop("tool_choice", None)
        return True

    async def start(self) -> None:
        try:
            self.response = await self._create(**self.payload)
        except Exception as exc:
            if not self._recover(exc):
                raise
            self.response = await self._create(**self.payload)
        if not self.payload.get("stream") and any(
            _value(choice, "message") is not None
            for choice in _value(self.response, "choices", []) or []
        ):
            self._succeeded()

    def _succeeded(self) -> None:
        if self._attempts:
            self._owner._remember(self._key)

    def metadata(self) -> dict[str, Any]:
        if not self._cached and not self._attempts:
            return {}
        return {
            "tool_choice_compatibility": {
                "requested_tool_choice": "required",
                "effective_tool_choice": "auto",
                "reason": "provider_rejected_forced_tool_choice",
                "source": "observed" if self._attempts else "client_cache",
                "recovery_attempts": self._attempts,
            }
        }

    async def iter_stream(self) -> AsyncIterator[Any]:
        output_started = False
        terminal = False
        while True:
            try:
                async with aclosing(_stream_items(self.response)) as stream:
                    async for chunk in stream:
                        output_started = output_started or _output_started(chunk)
                        terminal = terminal or _terminal(chunk)
                        yield chunk
            except Exception as exc:
                if output_started or not self._recover(exc):
                    raise
                self.response = await self._create(**self.payload)
                continue
            if terminal:
                self._succeeded()
            return
