# -*- coding: utf-8 -*-
"""Small compatibility helpers for provider stream deltas."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Literal


@dataclass
class NormalizedStreamChunk:
    """Provider-agnostic stream chunk shape used by compat wrappers."""

    text_delta: str = ""
    thinking_delta: str = ""
    tool_call_delta: list[Any] = field(default_factory=list)
    usage_delta: dict[str, Any] | None = None
    finish_reason: str | None = None
    error: str | None = None
    done: bool = False


StreamTextSemantics = Literal["auto", "delta", "snapshot"]


@dataclass(frozen=True)
class ProviderStreamProfile:
    """Provider-seam rules for turning raw chunks into one canonical stream.

    Provider quirks belong here rather than in Runtime events or renderers.
    ``deduplicate_snapshot_blocks`` is deliberately narrow: it only removes
    byte-identical blocks repeated *inside the same provider snapshot*.  It
    never treats identical chunks from an ordinary delta stream as duplicates.
    """

    provider_id: str = ""
    model_id: str = ""
    family: str = "openai"
    text_semantics: StreamTextSemantics = "auto"
    thinking_semantics: StreamTextSemantics = "auto"
    deduplicate_snapshot_blocks: bool = False


def provider_stream_profile(
    *,
    provider_id: str = "",
    model_id: str = "",
    family: str = "openai",
) -> ProviderStreamProfile:
    """Resolve a conservative stream profile at the provider boundary."""

    provider_key = str(provider_id or "").strip().casefold()
    model_key = str(model_id or "").strip().casefold()
    minimax_snapshot_blocks = (
        provider_key in {"minimax", "minimax-cn"}
        or "minimax" in model_key
    )
    return ProviderStreamProfile(
        provider_id=provider_key,
        model_id=model_key,
        family=str(family or "openai").strip().casefold(),
        deduplicate_snapshot_blocks=minimax_snapshot_blocks,
    )


@dataclass
class _CanonicalTextState:
    full_text: str = ""
    detected_semantics: StreamTextSemantics | None = None


class ProviderStreamNormalizer:
    """Stateful canonicalizer for one provider exchange.

    The state is scoped to a single model request.  Auto mode keeps the old
    cumulative-prefix compatibility while fixing its most dangerous ambiguity:
    two identical chunks are valid consecutive deltas until the provider has
    positively demonstrated snapshot semantics by extending a prior prefix.
    """

    def __init__(self, profile: ProviderStreamProfile | None = None) -> None:
        self.profile = profile or ProviderStreamProfile()
        self._text_states: dict[str, _CanonicalTextState] = {}

    def text_delta(
        self,
        current: str,
        *,
        channel: Literal["text", "thinking"] = "text",
        block_identity: str = "0",
        semantics: StreamTextSemantics | None = None,
    ) -> str:
        """Return the append-only delta for one logical provider block."""

        if not current:
            return ""
        configured = semantics or (
            self.profile.thinking_semantics
            if channel == "thinking"
            else self.profile.text_semantics
        )
        key = f"{channel}:{block_identity}"
        state = self._text_states.setdefault(key, _CanonicalTextState())

        if configured == "delta" or state.detected_semantics == "delta":
            state.detected_semantics = "delta"
            state.full_text += current
            return current

        if configured == "snapshot" or state.detected_semantics == "snapshot":
            state.detected_semantics = "snapshot"
            if current == state.full_text:
                return ""
            if current.startswith(state.full_text):
                delta = current[len(state.full_text) :]
                state.full_text = current
                return delta
            # A reset or rewritten snapshot is new provider output.  Preserve
            # it rather than silently deleting content on an unsafe guess.
            state.full_text = current
            return current

        if not state.full_text:
            state.full_text = current
            return current
        if current.startswith(state.full_text) and current != state.full_text:
            state.detected_semantics = "snapshot"
            delta = current[len(state.full_text) :]
            state.full_text = current
            return delta

        # Equality alone is not proof of a snapshot: token streams can
        # legitimately emit the same token twice ("哈" + "哈").
        state.detected_semantics = "delta"
        state.full_text += current
        return current

    def full_text(
        self,
        *,
        channel: Literal["text", "thinking"] = "text",
        block_identity: str = "0",
    ) -> str:
        """Return the canonical accumulated text for a logical block."""

        state = self._text_states.get(f"{channel}:{block_identity}")
        return state.full_text if state is not None else ""


def delta_from_possible_cumulative_text(
    current: str,
    *,
    previous: str,
) -> tuple[str, str]:
    """Return an append-only delta from either delta or cumulative chunks.

    Most OpenAI-compatible providers send token deltas, but some gateways send
    the entire content accumulated so far on every chunk. Keep the canonical
    stream append-only while preserving providers that already emit deltas.
    """

    if not current:
        return "", previous
    if previous and current == previous:
        return "", previous
    if current.startswith(previous):
        return current[len(previous) :], current
    return current, previous + current


def _lookup(source: Any, key: str) -> Any:
    if isinstance(source, dict):
        return source.get(key)
    return getattr(source, key, None)


def _mapping(source: Any) -> dict[str, Any]:
    if isinstance(source, dict):
        return source
    if hasattr(source, "model_dump"):
        try:
            dumped = source.model_dump(mode="json")
        except TypeError:
            dumped = source.model_dump()
        return dumped if isinstance(dumped, dict) else {}
    raw = getattr(source, "__dict__", None)
    return dict(raw) if isinstance(raw, dict) else {}


def _list(source: Any) -> list[Any]:
    if isinstance(source, list):
        return source
    if isinstance(source, tuple):
        return list(source)
    return []


def _json_args(value: Any) -> str:
    if isinstance(value, str):
        return value
    if value is None:
        return ""
    try:
        return json.dumps(value, ensure_ascii=False, separators=(",", ":"))
    except (TypeError, ValueError):
        return str(value)


def _tool_call_payload(
    *,
    index: int,
    name: str = "",
    arguments: Any = None,
    call_id: str | None = None,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "index": index,
        "function": {
            "name": name,
            "arguments": _json_args(arguments),
        },
    }
    if call_id:
        payload["id"] = call_id
    return payload


def _usage_number(source: Any, *keys: str) -> int:
    mapping = _mapping(source)
    for key in keys:
        raw = mapping.get(key)
        if raw is None:
            continue
        try:
            return int(raw)
        except (TypeError, ValueError):
            continue
    return 0


def _normalise_usage_payload(
    usage: Any,
    *,
    family: str,
) -> dict[str, Any] | None:
    mapping = _mapping(usage)
    if not mapping:
        return None

    family = family.lower()
    if family == "gemini":
        input_tokens = _usage_number(mapping, "promptTokenCount", "input_tokens")
        output_tokens = _usage_number(
            mapping,
            "candidatesTokenCount",
            "output_tokens",
        )
        total_tokens = _usage_number(mapping, "totalTokenCount", "total_tokens")
        cached_tokens = _usage_number(
            mapping,
            "cachedContentTokenCount",
            "cached_tokens",
        )
    else:
        input_tokens = _usage_number(mapping, "prompt_tokens", "input_tokens")
        output_tokens = _usage_number(
            mapping,
            "completion_tokens",
            "output_tokens",
        )
        total_tokens = _usage_number(mapping, "total_tokens")
        details = (
            _mapping(mapping.get("prompt_tokens_details"))
            or _mapping(mapping.get("input_tokens_details"))
        )
        cached_tokens = _usage_number(
            details,
            "cached_tokens",
            "cache_read_input_tokens",
        )
        cached_tokens += _usage_number(
            mapping,
            "cached_tokens",
            "cache_read_input_tokens",
            "cache_creation_input_tokens",
        )

    if not total_tokens:
        total_tokens = input_tokens + output_tokens
    return {
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "total_tokens": total_tokens,
        "cached_tokens": cached_tokens,
    }


def _error_text(source: Any) -> str:
    error = _lookup(source, "error")
    if isinstance(error, str):
        return error
    if error is None:
        return ""
    message = _lookup(error, "message")
    if isinstance(message, str):
        return message
    return str(error)


def _first_scalar_text(source: Any, *keys: str) -> str:
    for key in keys:
        value = _lookup(source, key)
        if isinstance(value, str) and value:
            return value
        if value is not None and not isinstance(value, (dict, list)):
            text = str(value)
            if text:
                return text
    return ""


def _content_blocks(source: Any) -> list[dict[str, Any]]:
    raw = _lookup(source, "content")
    if isinstance(raw, dict):
        return [raw]
    if not isinstance(raw, list):
        return []
    return [
        block
        for block in raw
        if isinstance(block, dict)
    ]


def _reasoning_detail_parts(source: Any) -> list[str]:
    if isinstance(source, str):
        return [source] if source else []
    if isinstance(source, (list, tuple)):
        parts: list[str] = []
        for item in source:
            parts.extend(_reasoning_detail_parts(item))
        return parts

    mapping = _mapping(source)
    if not mapping:
        return []

    parts: list[str] = []
    for key in (
        "text",
        "thinking",
        "reasoning",
        "reasoning_content",
        "reasoning_text",
        "summary",
        "content",
        "parts",
        "details",
    ):
        value = mapping.get(key)
        if isinstance(value, str):
            if value:
                parts.append(value)
        elif isinstance(value, (dict, list, tuple)):
            parts.extend(_reasoning_detail_parts(value))
    return parts


def _canonical_block_texts(
    source: Any,
    *,
    accepted_types: frozenset[str],
    deduplicate_identical_blocks: bool,
) -> list[str]:
    """Return ordered block text with optional same-snapshot deduplication."""

    parts: list[str] = []
    seen: set[tuple[str, str]] = set()
    for block in _content_blocks(source):
        block_type = str(block.get("type") or "").lower()
        if block_type not in accepted_types:
            continue
        text = block.get("thinking") or block.get("text") or ""
        normalized_parts = _reasoning_detail_parts(text)
        for part in normalized_parts:
            identity = (block_type, part)
            if deduplicate_identical_blocks and identity in seen:
                continue
            seen.add(identity)
            if part:
                parts.append(part)
    return parts


def extract_reasoning_delta(
    source: Any,
    *,
    deduplicate_identical_blocks: bool = False,
) -> str:
    """Return normalized thinking text from scalar or structured chunks."""

    direct = _first_scalar_text(
        source,
        "reasoning_content",
        "reasoningContent",
        "reasoning_text",
        "reasoning",
        "thinking",
    )
    if direct:
        return direct

    detail_parts = _reasoning_detail_parts(_lookup(source, "reasoning_details"))
    if detail_parts:
        return "\n".join(detail_parts)

    parts = _canonical_block_texts(
        source,
        accepted_types=frozenset({"thinking", "reasoning"}),
        deduplicate_identical_blocks=deduplicate_identical_blocks,
    )
    return "\n".join(part for part in parts if part)


def extract_text_delta(
    source: Any,
    *,
    deduplicate_identical_blocks: bool = False,
) -> str:
    """Return user-visible text delta from scalar or structured chunks."""

    direct = _first_scalar_text(source, "content", "text")
    if direct:
        return direct

    parts = _canonical_block_texts(
        source,
        accepted_types=frozenset({"text", ""}),
        deduplicate_identical_blocks=deduplicate_identical_blocks,
    )
    return "\n".join(part for part in parts if part)


def strip_reasoning_blocks(content: Any) -> Any:
    """Remove structured thinking blocks after promoting them elsewhere."""

    if not isinstance(content, list):
        return content
    filtered = [
        block
        for block in content
        if not (
            isinstance(block, dict)
            and str(block.get("type") or "").lower() in {"thinking", "reasoning"}
        )
    ]
    return filtered or None


def normalize_stream_chunk(
    delta: Any,
    *,
    usage: dict[str, Any] | None = None,
    finish_reason: str | None = None,
    error: str | None = None,
    profile: ProviderStreamProfile | None = None,
) -> NormalizedStreamChunk:
    """Build a small shared chunk contract from arbitrary provider deltas."""

    tool_calls = _lookup(delta, "tool_calls")
    deduplicate_identical_blocks = bool(
        profile and profile.deduplicate_snapshot_blocks
    )
    return NormalizedStreamChunk(
        text_delta=extract_text_delta(
            delta,
            deduplicate_identical_blocks=deduplicate_identical_blocks,
        ),
        thinking_delta=extract_reasoning_delta(
            delta,
            deduplicate_identical_blocks=deduplicate_identical_blocks,
        ),
        tool_call_delta=list(tool_calls) if isinstance(tool_calls, list) else [],
        usage_delta=usage if isinstance(usage, dict) else None,
        finish_reason=finish_reason,
        error=error,
        done=bool(finish_reason or error),
    )


def _combine_chunks(chunks: list[NormalizedStreamChunk]) -> NormalizedStreamChunk:
    text = "".join(chunk.text_delta for chunk in chunks)
    thinking = "".join(chunk.thinking_delta for chunk in chunks)
    tool_calls: list[Any] = []
    usage_delta: dict[str, Any] | None = None
    finish_reason: str | None = None
    error: str | None = None
    done = False

    for chunk in chunks:
        tool_calls.extend(chunk.tool_call_delta)
        if chunk.usage_delta is not None:
            usage_delta = chunk.usage_delta
        finish_reason = chunk.finish_reason or finish_reason
        error = chunk.error or error
        done = done or chunk.done

    return NormalizedStreamChunk(
        text_delta=text,
        thinking_delta=thinking,
        tool_call_delta=tool_calls,
        usage_delta=usage_delta,
        finish_reason=finish_reason,
        error=error,
        done=done,
    )


def _normalize_openai_payload(payload: Any) -> NormalizedStreamChunk:
    error = _error_text(payload)
    if error:
        return NormalizedStreamChunk(error=error, done=True)

    usage = _normalise_usage_payload(_lookup(payload, "usage"), family="openai")
    chunks: list[NormalizedStreamChunk] = []
    for choice in _list(_lookup(payload, "choices")):
        delta = _lookup(choice, "delta")
        finish_reason = _lookup(choice, "finish_reason")
        if delta is None and finish_reason is None:
            continue
        chunks.append(
            normalize_stream_chunk(
                delta or {},
                usage=None,
                finish_reason=str(finish_reason) if finish_reason else None,
            ),
        )

    if not chunks:
        return NormalizedStreamChunk(usage_delta=usage)
    combined = _combine_chunks(chunks)
    combined.usage_delta = usage
    if usage is not None and not combined.done:
        combined.done = False
    return combined


def _normalize_anthropic_payload(payload: Any) -> NormalizedStreamChunk:
    error = _error_text(payload)
    if error:
        return NormalizedStreamChunk(error=error, done=True)

    event_type = str(_lookup(payload, "type") or "")
    if event_type == "message_start":
        message = _mapping(_lookup(payload, "message"))
        usage = _normalise_usage_payload(message.get("usage"), family="anthropic")
        return NormalizedStreamChunk(usage_delta=usage)

    if event_type == "message_delta":
        delta = _mapping(_lookup(payload, "delta"))
        finish_reason = delta.get("stop_reason") or delta.get("finish_reason")
        return NormalizedStreamChunk(
            usage_delta=_normalise_usage_payload(
                _lookup(payload, "usage"),
                family="anthropic",
            ),
            finish_reason=str(finish_reason) if finish_reason else None,
            done=bool(finish_reason),
        )

    if event_type == "message_stop":
        return NormalizedStreamChunk(done=True)

    if event_type == "content_block_start":
        block = _mapping(_lookup(payload, "content_block"))
        block_type = str(block.get("type") or "")
        index = _usage_number(payload, "index")
        if block_type == "tool_use":
            return NormalizedStreamChunk(
                tool_call_delta=[
                    _tool_call_payload(
                        index=index,
                        call_id=str(block.get("id") or ""),
                        name=str(block.get("name") or ""),
                        arguments=block.get("input") or {},
                    ),
                ],
            )
        if block_type == "thinking":
            return NormalizedStreamChunk(
                thinking_delta=str(block.get("thinking") or block.get("text") or ""),
            )
        if block_type == "text":
            return NormalizedStreamChunk(text_delta=str(block.get("text") or ""))

    if event_type == "content_block_delta":
        delta = _mapping(_lookup(payload, "delta"))
        delta_type = str(delta.get("type") or "")
        if delta_type == "text_delta":
            return NormalizedStreamChunk(text_delta=str(delta.get("text") or ""))
        if delta_type == "thinking_delta":
            return NormalizedStreamChunk(
                thinking_delta=str(delta.get("thinking") or ""),
            )
        if delta_type == "input_json_delta":
            return NormalizedStreamChunk(
                tool_call_delta=[
                    _tool_call_payload(
                        index=_usage_number(payload, "index"),
                        arguments=str(delta.get("partial_json") or ""),
                    ),
                ],
            )

    return NormalizedStreamChunk()


def _gemini_parts(candidate: Any) -> list[Any]:
    content = _mapping(_lookup(candidate, "content"))
    return _list(content.get("parts") or _lookup(content, "parts"))


def _normalize_gemini_payload(payload: Any) -> NormalizedStreamChunk:
    error = _error_text(payload)
    if error:
        return NormalizedStreamChunk(error=error, done=True)

    usage = _normalise_usage_payload(
        _lookup(payload, "usageMetadata") or _lookup(payload, "usage_metadata"),
        family="gemini",
    )
    chunks: list[NormalizedStreamChunk] = []
    tool_index = 0

    for candidate in _list(_lookup(payload, "candidates")):
        candidate_map = _mapping(candidate)
        text_parts: list[str] = []
        thinking_parts: list[str] = []
        tool_calls: list[Any] = []
        for part in _gemini_parts(candidate):
            part_map = _mapping(part)
            function_call = (
                part_map.get("functionCall")
                or part_map.get("function_call")
                or _lookup(part, "function_call")
            )
            if function_call:
                function_map = _mapping(function_call)
                tool_calls.append(
                    _tool_call_payload(
                        index=tool_index,
                        name=str(function_map.get("name") or ""),
                        arguments=function_map.get("args")
                        or function_map.get("arguments")
                        or {},
                    ),
                )
                tool_index += 1
                continue

            text = str(part_map.get("text") or _lookup(part, "text") or "")
            if not text:
                continue
            is_thinking = bool(
                part_map.get("thought")
                or part_map.get("thoughtSignature")
                or part_map.get("thought_signature")
            )
            if is_thinking:
                thinking_parts.append(text)
            else:
                text_parts.append(text)

        finish_reason = candidate_map.get("finishReason") or candidate_map.get(
            "finish_reason",
        )
        chunks.append(
            NormalizedStreamChunk(
                text_delta="".join(text_parts),
                thinking_delta="".join(thinking_parts),
                tool_call_delta=tool_calls,
                finish_reason=str(finish_reason) if finish_reason else None,
                done=bool(finish_reason),
            ),
        )

    if not chunks:
        return NormalizedStreamChunk(usage_delta=usage)
    combined = _combine_chunks(chunks)
    combined.usage_delta = usage
    return combined


def normalize_provider_stream_payload(
    payload: Any,
    *,
    family: str,
) -> NormalizedStreamChunk:
    """Normalize a captured provider stream payload into fixture-contract shape."""

    provider_family = family.lower().replace("_compatible", "")
    if provider_family in {"openai", "azure", "openrouter"}:
        return _normalize_openai_payload(payload)
    if provider_family == "anthropic":
        return _normalize_anthropic_payload(payload)
    if provider_family == "gemini":
        return _normalize_gemini_payload(payload)
    raise ValueError(f"Unsupported provider stream family: {family}")
