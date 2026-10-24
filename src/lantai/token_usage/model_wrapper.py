# -*- coding: utf-8 -*-
"""Model wrapper that records token usage from LLM responses."""

from datetime import date, datetime, timezone
from collections.abc import AsyncGenerator, Mapping
from typing import Any, Literal, Type

from pydantic import BaseModel

from .buffer import _UsageEvent
from .manager import get_token_usage_manager
from ..providers.reasoning import apply_current_reasoning_options
from ..providers.request_profiles import is_kimi_coding_base_url


class TokenRecordingModelWrapper:
    """Wrap a chat model-like object to record token usage on each call."""

    _usage_by_session: dict[str, list[dict[str, Any]]] = {}

    def __init__(self, provider_id: str, model: Any) -> None:
        self.model_name = getattr(model, "model_name", "unknown")
        self.stream = getattr(model, "stream", True)
        self._model = model
        self._provider_id = provider_id

    def __getattr__(self, name: str) -> Any:
        return getattr(self._model, name)

    @staticmethod
    def _as_mapping(value: Any) -> Mapping[str, Any] | None:
        """Return a read-only mapping view for dict-like usage payloads."""
        if isinstance(value, Mapping):
            return value
        if isinstance(value, BaseModel):
            return value.model_dump()
        model_dump = getattr(value, "model_dump", None)
        if callable(model_dump):
            dumped = model_dump()
            if isinstance(dumped, Mapping):
                return dumped
        return None

    @classmethod
    def _usage_number(cls, source: Any, *keys: str) -> int:
        """Read a numeric usage field from dicts or provider objects."""
        mapping = cls._as_mapping(source)
        for key in keys:
            if mapping is not None:
                raw = mapping.get(key)
            else:
                raw = getattr(source, key, None)
            if raw is None:
                continue
            try:
                return int(raw)
            except (TypeError, ValueError):
                continue
        return 0

    @classmethod
    def _nested_usage_number_or_none(
        cls,
        source: Any,
        *paths: tuple[str, ...],
    ) -> int | None:
        """Read a numeric field while preserving missing versus explicit zero."""
        for path in paths:
            current = source
            for key in path:
                mapping = cls._as_mapping(current)
                if mapping is not None:
                    current = mapping.get(key)
                else:
                    current = getattr(current, key, None)
                if current is None:
                    break
            else:
                try:
                    return int(current)
                except (TypeError, ValueError):
                    continue
        return None

    def _record_usage(self, usage: Any | dict[str, Any] | None) -> None:
        """Enqueue a usage event synchronously — never blocks the caller."""
        if usage is None:
            return

        usage_meta = (
            usage.get("metadata")
            if isinstance(usage, Mapping)
            else getattr(usage, "metadata", None)
        )
        pt = self._usage_number(
            usage,
            "input_tokens",
            "prompt_tokens",
            "inputTokens",
            "promptTokens",
        )
        ct = self._usage_number(
            usage,
            "output_tokens",
            "completion_tokens",
            "outputTokens",
            "completionTokens",
        )
        # Some legacy providers store the raw OpenAI CompletionUsage object in
        # metadata. Fall back to it so provider-specific objects never break
        # chat.
        if pt <= 0:
            pt = self._usage_number(
                usage_meta,
                "prompt_tokens",
                "input_tokens",
                "inputTokens",
                "promptTokens",
            )
        if ct <= 0:
            ct = self._usage_number(
                usage_meta,
                "completion_tokens",
                "output_tokens",
                "outputTokens",
                "completionTokens",
            )

        cached_tokens = self._nested_usage_number_or_none(
            usage,
            ("cached_tokens",),
            ("prompt_tokens_details", "cached_tokens"),
            ("input_tokens_details", "cached_tokens"),
            ("input_token_details", "cache_read"),
            ("input_token_details", "cached_tokens"),
            ("cache_read_input_tokens",),
            ("cached_input_tokens",),
        )
        if cached_tokens is None:
            cached_tokens = self._nested_usage_number_or_none(
                usage_meta,
                ("cached_tokens",),
                ("prompt_tokens_details", "cached_tokens"),
                ("input_tokens_details", "cached_tokens"),
                ("input_token_details", "cache_read"),
                ("input_token_details", "cached_tokens"),
                ("cache_read_input_tokens",),
                ("cached_input_tokens",),
            )

        meta_mapping = self._as_mapping(usage_meta)
        cost_cny = 0
        if meta_mapping is not None:
            try:
                cost_cny = float(meta_mapping.get("cost_cny", 0) or 0)
            except (TypeError, ValueError):
                cost_cny = 0
        if pt <= 0 and ct <= 0 and not cost_cny:
            return

        event = _UsageEvent(
            provider_id=self._provider_id,
            model_name=self.model_name,
            prompt_tokens=pt,
            completion_tokens=ct,
            date_str=date.today().isoformat(),
            now_iso=datetime.now(tz=timezone.utc).isoformat(
                timespec="seconds",
            ),
        )
        # Fire-and-forget: synchronous put_nowait, ~100 ns, no await needed.
        get_token_usage_manager().enqueue(event)

        usage_data = {
            "provider_id": self._provider_id,
            "model_name": self.model_name,
            "prompt_tokens": pt,
            "completion_tokens": ct,
            "input_tokens": pt,
            "output_tokens": ct,
            "total_tokens": pt + ct,
        }
        if cached_tokens is not None:
            usage_data["cached_tokens"] = cached_tokens
            usage_data["cache_hit_rate"] = cached_tokens / pt if pt > 0 else 0
        # Capture cost from gateway metadata if present
        if cost_cny:
            usage_data["cost_cny"] = cost_cny
        self._store_usage(usage_data)

    @classmethod
    def pop_usage_for_session(cls, session_id: str) -> dict[str, Any] | None:
        calls = cls._usage_by_session.pop(session_id, None)
        if not calls:
            return None
        if isinstance(calls, Mapping):
            calls = [dict(calls)]

        prompt_tokens = sum(int(call.get("prompt_tokens", 0) or 0) for call in calls)
        completion_tokens = sum(
            int(call.get("completion_tokens", 0) or 0) for call in calls
        )
        cached_known = any("cached_tokens" in call for call in calls)
        cached_tokens = (
            sum(int(call.get("cached_tokens", 0) or 0) for call in calls)
            if cached_known
            else None
        )
        cost_cny = sum(float(call.get("cost_cny", 0) or 0) for call in calls)
        last_call = calls[-1]

        usage: dict[str, Any] = {
            "provider_id": last_call.get("provider_id"),
            "model_name": last_call.get("model_name"),
            "prompt_tokens": prompt_tokens,
            "completion_tokens": completion_tokens,
            "input_tokens": prompt_tokens,
            "output_tokens": completion_tokens,
            "total_tokens": prompt_tokens + completion_tokens,
            "context_tokens": int(last_call.get("input_tokens", 0) or 0),
            "calls": calls,
        }
        if cached_tokens is not None:
            usage["cached_tokens"] = cached_tokens
            usage["cache_hit_rate"] = (
                cached_tokens / prompt_tokens if prompt_tokens > 0 else 0
            )
        if cost_cny:
            usage["cost_cny"] = cost_cny
        return usage

    def _store_usage(self, usage: dict[str, Any] | None) -> None:
        from ..app.agent_context import get_current_session_id

        session_id = get_current_session_id()
        if session_id and usage:
            TokenRecordingModelWrapper._usage_by_session.setdefault(
                session_id,
                [],
            ).append(usage)

    async def __call__(
        self,
        messages: list[dict],
        tools: list[dict] | None = None,
        tool_choice: Literal["auto", "none", "required"] | str | None = None,
        structured_model: Type[BaseModel] | None = None,
        **kwargs: Any,
    ) -> Any | AsyncGenerator[Any, None]:
        # Fix: Omit tool_choice="auto" for vLLM compatibility
        # vLLM without --enable-auto-tool-choice will reject requests when
        # tool_choice="auto" is present, even if tools are provided.
        # By omitting tool_choice when it's "auto", we bypass the check
        # while keeping tools available for correct tool calling behavior.
        if tool_choice == "auto":
            tool_choice = None

        base_url = str(
            getattr(getattr(self._model, "client", None), "base_url", "")
            or "",
        )
        kwargs = apply_current_reasoning_options(
            kwargs,
            provider_id=self._provider_id,
            model_id=self.model_name,
            base_url=base_url,
            chat_model=self._model.__class__.__name__,
        )
        if is_kimi_coding_base_url(base_url) and "prompt_cache_key" not in kwargs:
            from ..app.agent_context import get_current_session_id

            session_id = get_current_session_id()
            if session_id:
                kwargs["prompt_cache_key"] = session_id

        from ..research.context import legacy_dispatch

        result = await legacy_dispatch(self._model, self._provider_id)(
            messages=messages,
            tools=tools,
            tool_choice=tool_choice,
            structured_model=structured_model,
            **kwargs,
        )

        if isinstance(result, AsyncGenerator):
            return self._wrap_stream(result)
        self._record_usage(getattr(result, "usage", None))
        return result

    async def _wrap_stream(
        self,
        stream: AsyncGenerator[Any, None],
    ) -> AsyncGenerator[Any, None]:
        last_usage: Any | None = None
        try:
            async for chunk in stream:
                if getattr(chunk, "usage", None) is not None:
                    last_usage = chunk.usage
                yield chunk
        finally:
            await stream.aclose()
        self._record_usage(last_usage)
