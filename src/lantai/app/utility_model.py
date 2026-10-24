# -*- coding: utf-8 -*-
"""Shared side channel for short, non-conversational model tasks.

Utility calls deliberately receive only their task prompt and input. They do
not inherit chat history, tools, runtime state, or the visible response stream.
"""
from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass
from typing import Any

from ..config import load_config
from ..config.config import (
    ModelSlotConfig,
    UtilityIntelligenceConfig,
)
from ..providers.provider_manager import ProviderManager
from ..providers.stream_normalizer import ProviderStreamNormalizer
from ..token_usage import TokenRecordingModelWrapper
from .agent_context import current_session_id_scope
from .background_semantics import run_primary_when_idle

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class ResolvedUtilityModel:
    model: Any
    provider_id: str
    model_id: str
    formatter: Any | None = None
    supports_image: bool | None = None

    @property
    def label(self) -> str:
        return f"{self.provider_id}/{self.model_id}"


@dataclass(frozen=True)
class UtilityTextResult:
    text: str
    model_label: str


class _UtilityMessage:
    """Small formatter-compatible message for isolated utility requests."""

    def __init__(self, *, role: str, content: Any) -> None:
        self.name = ""
        self.role = role
        self.content = content

    def get_content_blocks(self) -> list[dict[str, Any]]:
        if isinstance(self.content, str):
            return [{"type": "text", "text": self.content}]
        if isinstance(self.content, list):
            return [block for block in self.content if isinstance(block, dict)]
        return [{"type": "text", "text": str(self.content)}]

    def to_dict(self) -> dict[str, Any]:
        """Expose the message shape expected by OpenAI-compatible formatters."""

        return {
            "name": self.name,
            "role": self.role,
            "content": self.content,
        }


class UtilityTaskTimeoutError(TimeoutError):
    """Describe which bounded phase of an isolated model task timed out."""

    def __init__(self, *, phase: str, timeout_seconds: float) -> None:
        self.phase = phase
        self.timeout_seconds = float(timeout_seconds)
        labels = {
            "first_output": "first model output",
            "idle": "the next model output",
            "total": "task completion",
        }
        target = labels.get(phase, "model output")
        super().__init__(
            f"utility model timed out waiting for {target} after "
            f"{self.timeout_seconds:g}s",
        )


class UtilityModelPolicyError(RuntimeError):
    """Raised before a utility call violates an explicit provider policy."""


class UtilityTaskIncompleteError(RuntimeError):
    """Reject provider-terminated partial output without retaining its text."""

    def __init__(self, *, finish_reason: str) -> None:
        self.finish_reason = (
            finish_reason
            if finish_reason in {"length", "content_filter"} else "other"
        )
        super().__init__(f"utility model output incomplete: {self.finish_reason}")


def utility_capability_matrix(
    config: UtilityIntelligenceConfig,
    *,
    model_available: bool,
) -> dict[str, Any]:
    """Describe utility availability and honest degradation per consumer."""
    background_enabled = bool(
        getattr(config, "background_processing_enabled", True),
    )
    accelerator_available = config.mode == "dedicated" and bool(model_available)
    available = background_enabled
    reason = (
        "background_processing_disabled"
        if not background_enabled
        else (
            "dedicated_accelerator"
            if accelerator_available
            else "active_model_idle_fallback"
        )
    )
    return {
        "schema_version": "1",
        "mode": config.mode,
        "background_processing_enabled": background_enabled,
        "accelerator_available": accelerator_available,
        "available": available,
        "reason": reason,
        "turn_intelligence": {
            "required": False,
            "degradation": (
                "none" if accelerator_available else "primary_model_direct"
            ),
        },
        "memory_extraction": {
            "required": False,
            "degradation": (
                "none"
                if accelerator_available
                else (
                    "active_model_idle"
                    if background_enabled
                    else "l0_only"
                )
            ),
        },
        "title_generation": {
            "required": False,
            "degradation": (
                "none"
                if accelerator_available
                else (
                    "active_model_idle"
                    if background_enabled
                    else "local_semantic_title"
                )
            ),
        },
        "context_compaction": {
            "required": False,
            "degradation": "none" if available else "local_checkpoint",
        },
        "structured_tasks": {
            "required": True,
            "degradation": (
                "none"
                if accelerator_available
                else (
                    "active_model_idle"
                    if background_enabled
                    else "explicit_unavailable"
                )
            ),
        },
    }


def safe_attr(obj: Any, name: str) -> Any:
    """Read an attribute without leaking legacy dict ``KeyError`` behavior."""
    if isinstance(obj, dict):
        return obj.get(name)
    try:
        return getattr(obj, name, None)
    except (AttributeError, KeyError, TypeError):
        return None


def _first_text_in_list(items: list) -> str:
    for item in items:
        if isinstance(item, dict) and isinstance(item.get("text"), str):
            return item["text"]
        inner = safe_attr(item, "text")
        if isinstance(inner, str):
            return inner
    return ""


def extract_text_from_response(response: Any) -> str:
    """Extract text from regular responses and provider stream chunks."""
    if response is None:
        return ""
    if isinstance(response, str):
        return response
    text = safe_attr(response, "text")
    if isinstance(text, str) and text:
        return text
    content = safe_attr(response, "content")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return _first_text_in_list(content)
    return ""


def _contains_json_value(text: str) -> bool:
    """Return whether model text contains at least one valid JSON value."""

    decoder = json.JSONDecoder()
    for index, char in enumerate(text):
        if char not in "[{":
            continue
        try:
            value, _end = decoder.raw_decode(text[index:])
        except json.JSONDecodeError:
            continue
        if isinstance(value, (dict, list)):
            return True
    return False


async def consume_model_response(
    model: Any,
    messages: list,
    *,
    first_output_timeout_seconds: float | None = None,
    idle_timeout_seconds: float | None = None,
    total_timeout_seconds: float | None = None,
    generation_kwargs: dict[str, Any] | None = None,
) -> str:
    """Consume a provider response without exposing its stream to chat."""
    loop = asyncio.get_running_loop()
    first_timeout = (
        max(0.001, float(first_output_timeout_seconds))
        if first_output_timeout_seconds is not None
        else None
    )
    idle_timeout = (
        max(0.001, float(idle_timeout_seconds))
        if idle_timeout_seconds is not None
        else None
    )
    total_timeout = (
        max(0.001, float(total_timeout_seconds))
        if total_timeout_seconds is not None
        else None
    )
    first_deadline = loop.time() + first_timeout if first_timeout else None
    total_deadline = loop.time() + total_timeout if total_timeout else None
    model_kwargs = dict(generation_kwargs or {})

    def next_timeout(
        *,
        phase: str,
        phase_timeout: float | None,
        phase_deadline: float | None = None,
    ) -> tuple[float, str, float] | None:
        """Choose the nearest phase or total deadline for one await point."""
        now = loop.time()
        candidates: list[tuple[float, str, float]] = []
        if phase_deadline is not None and phase_timeout is not None:
            candidates.append((phase_deadline - now, phase, phase_timeout))
        elif phase_timeout is not None:
            candidates.append((phase_timeout, phase, phase_timeout))
        if total_deadline is not None and total_timeout is not None:
            candidates.append((total_deadline - now, "total", total_timeout))
        if not candidates:
            return None
        remaining, selected_phase, reported_timeout = min(
            candidates,
            key=lambda candidate: candidate[0],
        )
        if remaining <= 0:
            raise UtilityTaskTimeoutError(
                phase=selected_phase,
                timeout_seconds=reported_timeout,
            )
        return remaining, selected_phase, reported_timeout

    initial_timeout = next_timeout(
        phase="first_output",
        phase_timeout=first_timeout,
        phase_deadline=first_deadline,
    )
    if initial_timeout is not None:
        timeout, timeout_phase, reported_timeout = initial_timeout
        try:
            response = await asyncio.wait_for(
                model(messages, **model_kwargs),
                timeout=timeout,
            )
        except asyncio.TimeoutError as exc:
            raise UtilityTaskTimeoutError(
                phase=timeout_phase,
                timeout_seconds=reported_timeout,
            ) from exc
    else:
        response = await model(messages, **model_kwargs)
    if hasattr(response, "__aiter__"):
        accumulated = ""
        stream_normalizer = ProviderStreamNormalizer()
        finish_reason = None
        iterator = response.__aiter__()
        first_chunk = True
        while True:
            phase = "first_output" if first_chunk else "idle"
            phase_timeout = first_timeout if first_chunk else idle_timeout
            phase_deadline = first_deadline if first_chunk else None
            selected_timeout = None
            try:
                selected_timeout = next_timeout(
                    phase=phase,
                    phase_timeout=phase_timeout,
                    phase_deadline=phase_deadline,
                )
                next_chunk = iterator.__anext__()
                if selected_timeout is None:
                    chunk = await next_chunk
                else:
                    timeout, timeout_phase, reported_timeout = selected_timeout
                    chunk = await asyncio.wait_for(next_chunk, timeout=timeout)
            except StopAsyncIteration:
                if finish_reason in {"length", "content_filter"}:
                    raise UtilityTaskIncompleteError(finish_reason=finish_reason)
                return accumulated
            except Exception as exc:
                # Drain terminal usage when possible, but a known incomplete
                # finish is irreversible. Tail deadlines or transport errors
                # must not reclassify it as a blindly retryable failure.
                # CancelledError remains outside Exception and propagates.
                if finish_reason in {"length", "content_filter"}:
                    raise UtilityTaskIncompleteError(
                        finish_reason=finish_reason,
                    ) from None
                if isinstance(exc, asyncio.TimeoutError) and selected_timeout:
                    _, timeout_phase, reported_timeout = selected_timeout
                    raise UtilityTaskTimeoutError(
                        phase=timeout_phase,
                        timeout_seconds=reported_timeout,
                    ) from exc
                raise
            first_chunk = False
            reason = safe_attr(chunk, "finish_reason")
            # Anthropic's wire enum is equivalent to OpenAI's length finish;
            # do not infer truncation from usage or from the output's shape.
            if reason == "max_tokens":
                reason = "length"
            if reason is not None and finish_reason not in {"length", "content_filter"}:
                finish_reason = reason
            text = extract_text_from_response(chunk)
            if text:
                semantics = safe_attr(chunk, "text_semantics")
                accumulated += stream_normalizer.text_delta(
                    text,
                    semantics=(
                        semantics
                        if isinstance(semantics, str) and semantics in {"delta", "snapshot"}
                        else None
                    ),
                )
    reason = safe_attr(response, "finish_reason")
    if reason == "max_tokens":
        reason = "length"
    if reason in {"length", "content_filter"}:
        raise UtilityTaskIncompleteError(finish_reason=reason)
    return extract_text_from_response(response)


def _slot_image_capability(slot: ModelSlotConfig) -> bool | None:
    """Use exact model metadata, keeping unknown custom-provider compatibility."""
    provider = ProviderManager.get_instance().get_provider(slot.provider_id)
    if provider is None:
        return None
    info = provider.get_model_info(slot.model)
    if info is None:
        return None
    image = getattr(info, "supports_image", None)
    if isinstance(image, bool):
        return image
    legacy = getattr(info, "supports_multimodal", None)
    return legacy if isinstance(legacy, bool) else None


def _dedicated_model(slot: ModelSlotConfig) -> ResolvedUtilityModel | None:
    manager = ProviderManager.get_instance()
    if not manager.is_model_slot_available(slot):
        return None
    provider = manager.get_provider(slot.provider_id)
    if provider is None:
        return None
    raw_model = provider.get_chat_model_instance(slot.model)
    from ..agents.model_factory import create_formatter_for_chat_model

    supports_image = _slot_image_capability(slot)
    return ResolvedUtilityModel(
        model=TokenRecordingModelWrapper(slot.provider_id, raw_model),
        provider_id=slot.provider_id,
        model_id=slot.model,
        formatter=create_formatter_for_chat_model(
            raw_model,
            # Unknown custom models retain the existing permissive policy;
            # known text-only slots are rejected before a visual request.
            supports_multimodal=supports_image is not False,
        ),
        supports_image=supports_image,
    )


def resolve_active_model(agent_id: str) -> ResolvedUtilityModel | None:
    """Resolve the agent's primary chat model for an isolated side-channel call."""

    # Resolve these lazily so importing the side-channel never initializes an
    # Agent model and tests can replace the active-model source cleanly.
    from ..agents.model_factory import create_model_and_formatter
    from ..config.config import load_agent_config

    model, formatter = create_model_and_formatter(agent_id=agent_id)
    try:
        slot = load_agent_config(agent_id).active_model
    except Exception:
        slot = None
    # Match create_model_and_formatter: incomplete agent slots use global.
    if slot is None or not slot.provider_id or not slot.model:
        slot = ProviderManager.get_instance().get_active_model()
    if slot is None:
        return None
    supports_image = _slot_image_capability(slot)
    if formatter is not None:
        # This formatter belongs to the isolated call, whose agent slot may
        # differ from the ambient foreground chat used by default formatting.
        formatter._lantai_supports_multimodal = supports_image is not False
    return ResolvedUtilityModel(
        model=model,
        provider_id=slot.provider_id,
        model_id=slot.model,
        formatter=formatter,
        supports_image=supports_image,
    )


def resolve_utility_model(
    config: UtilityIntelligenceConfig,
    agent_id: str,
) -> ResolvedUtilityModel | None:
    """Resolve the configured utility model without touching chat state."""
    if config.mode == "dedicated":
        return _dedicated_model(config.model)
    return resolve_active_model(agent_id)


async def run_utility_text_task(
    *,
    agent_id: str,
    system_prompt: str,
    user_prompt: str,
    purpose: str,
    timeout_seconds: float | None = None,
    first_output_timeout_seconds: float | None = None,
    idle_timeout_seconds: float | None = None,
    allow_follow_active: bool = True,
    allow_model_fallback: bool = True,
    expected_provider_id: str | None = None,
    require_no_api_cost: bool = False,
    max_output_tokens: int | None = None,
    usage_session_id: str | None = None,
    structured_output_schema: dict[str, Any] | None = None,
) -> UtilityTextResult | None:
    """Run one isolated text task through the configured utility model."""
    config = load_config().utility_intelligence
    if (
        allow_follow_active
        and not bool(getattr(config, "background_processing_enabled", True))
    ):
        return None
    if not allow_follow_active and config.mode != "dedicated":
        # Foreground maintenance (for example automatic context compaction)
        # must not consume the same provider/model slot that owes the user a
        # reply.  Without a dedicated helper, the caller degrades locally.
        return None

    try:
        resolved = resolve_utility_model(config, agent_id)
    except Exception:
        logger.warning("Utility accelerator unavailable", exc_info=True)
        resolved = None
    uses_primary = config.mode == "follow_active"
    if resolved is None and allow_follow_active and allow_model_fallback:
        try:
            resolved = resolve_active_model(agent_id)
        except Exception:
            logger.warning("Active utility fallback unavailable", exc_info=True)
            resolved = None
        uses_primary = resolved is not None
    if resolved is None:
        return None
    if (
        expected_provider_id is not None
        and resolved.provider_id != expected_provider_id
    ):
        raise UtilityModelPolicyError(
            "resolved utility model does not match the authorized provider "
            f"{expected_provider_id!r}",
        )
    if require_no_api_cost:
        provider = ProviderManager.get_instance().get_provider(
            resolved.provider_id,
        )
        model_info = (
            provider.get_model_info(resolved.model_id)
            if provider is not None
            else None
        )
        if not (
            provider is not None
            and (
                bool(getattr(provider, "is_local", False))
                or bool(getattr(model_info, "is_free", False))
            )
        ):
            raise UtilityModelPolicyError(
                "utility model must be local or explicitly free before "
                "this task can run",
            )

    timeout = (
        float(timeout_seconds)
        if isinstance(timeout_seconds, (int, float))
        else float(config.timeout_seconds)
    )
    logger.debug("Running utility task %s via %s", purpose, resolved.label)
    consume_kwargs: dict[str, Any] = {"total_timeout_seconds": timeout}
    if first_output_timeout_seconds is not None:
        consume_kwargs["first_output_timeout_seconds"] = float(
            first_output_timeout_seconds,
        )
    if idle_timeout_seconds is not None:
        consume_kwargs["idle_timeout_seconds"] = float(idle_timeout_seconds)
    generation_kwargs: dict[str, Any] = {}
    if max_output_tokens is not None:
        if int(max_output_tokens) <= 0:
            raise UtilityModelPolicyError(
                "utility model output token ceiling must be positive",
            )
        generation_kwargs["max_tokens"] = int(max_output_tokens)
    if structured_output_schema is not None and resolved.provider_id == "lmstudio":
        provider = ProviderManager.get_instance().get_provider(resolved.provider_id)
        if provider is not None and bool(getattr(provider, "is_local", False)):
            # LM Studio supports schema-constrained output for its local engines.
            # Other providers retain the existing prompt and validation contract.
            generation_kwargs["response_format"] = {
                "type": "json_schema",
                "json_schema": {
                    "name": "utility_result",
                    "strict": True,
                    "schema": structured_output_schema,
                },
            }
    if generation_kwargs:
        consume_kwargs["generation_kwargs"] = generation_kwargs
    try:
        async def consume() -> str:
            async def consume_response() -> str:
                # All deadlines belong to the consumer: an outer wait_for
                # would cancel it and discard an already-known partial finish.
                return await consume_model_response(
                    resolved.model,
                    [
                        {"role": "system", "content": system_prompt},
                        {"role": "user", "content": user_prompt},
                    ],
                    **consume_kwargs,
                )

            if usage_session_id is not None:
                with current_session_id_scope(usage_session_id):
                    return await consume_response()
            return await consume_response()

        text = (
            await run_primary_when_idle(agent_id, consume)
            if uses_primary
            else await consume()
        )
    except UtilityTaskTimeoutError:
        raise
    except asyncio.TimeoutError as exc:
        raise UtilityTaskTimeoutError(
            phase="total",
            timeout_seconds=timeout,
        ) from exc
    return UtilityTextResult(text=text, model_label=resolved.label)


async def run_utility_content_task(
    *,
    agent_id: str,
    system_prompt: str,
    user_content: str | list[dict[str, Any]],
    purpose: str,
    timeout_seconds: float | None = None,
    allow_follow_active: bool = True,
    usage_session_id: str | None = None,
    wait_for_idle: bool = True,
) -> UtilityTextResult | None:
    """Run an isolated text-or-multimodal task through utility intelligence.

    The caller supplies only provider-neutral Lantai content blocks.  This
    keeps rendered-slide review away from chat history, tools and mutable
    runtime state while allowing the normal provider formatter to carry the
    exact image evidence. Images never go to a known text-only slot. A
    foreground tool child may explicitly opt out of the background idle lane:
    its parent turn cannot become idle until this requested review completes.
    """

    config = load_config().utility_intelligence
    if (
        allow_follow_active
        and not bool(getattr(config, "background_processing_enabled", True))
    ):
        return None
    if not allow_follow_active and config.mode != "dedicated":
        return None

    try:
        resolved = resolve_utility_model(config, agent_id)
    except Exception:
        logger.warning("Utility accelerator unavailable", exc_info=True)
        resolved = None
    uses_primary = config.mode == "follow_active"
    needs_images = isinstance(user_content, list) and any(
        isinstance(block, dict) and block.get("type") in {"image", "image_url"}
        for block in user_content
    )
    if needs_images and resolved is not None and resolved.supports_image is False:
        resolved = None
    if resolved is None and allow_follow_active:
        try:
            resolved = resolve_active_model(agent_id)
        except Exception:
            logger.warning("Active utility fallback unavailable", exc_info=True)
            resolved = None
        uses_primary = resolved is not None
    if resolved is None or (needs_images and resolved.supports_image is False):
        return None

    timeout = (
        max(float(timeout_seconds), 0.01)
        if isinstance(timeout_seconds, (int, float))
        else max(float(config.timeout_seconds), 0.01)
    )
    if resolved.formatter is None:
        messages = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_content},
        ]
    else:
        messages = await resolved.formatter.format(
            [
                _UtilityMessage(role="system", content=system_prompt),
                _UtilityMessage(role="user", content=user_content),
            ],
        )

    async def consume() -> str:
        async def consume_response() -> str:
            return await consume_model_response(
                resolved.model, messages, total_timeout_seconds=timeout,
            )

        if usage_session_id is not None:
            with current_session_id_scope(usage_session_id):
                return await consume_response()
        return await consume_response()

    logger.debug("Running utility content task %s via %s", purpose, resolved.label)
    try:
        text = (
            await run_primary_when_idle(agent_id, consume)
            if uses_primary and wait_for_idle
            else await consume()
        )
    except UtilityTaskTimeoutError:
        raise
    except asyncio.TimeoutError as exc:
        raise UtilityTaskTimeoutError(
            phase="total",
            timeout_seconds=timeout,
        ) from exc
    return UtilityTextResult(text=text, model_label=resolved.label)


async def run_active_model_text_task(
    *,
    agent_id: str,
    system_prompt: str,
    user_prompt: str,
    purpose: str,
    timeout_seconds: float | None = None,
    wait_for_idle: bool = False,
) -> UtilityTextResult | None:
    """Run one isolated fallback task through the active chat model.

    This is intentionally separate from ``run_utility_text_task``: callers may
    use it only as an explicit recovery path after configured dedicated
    intelligence failed.  The call receives no chat tools or mutable runtime
    state and remains bounded by the utility timeout.
    """

    config = load_config().utility_intelligence
    timeout = (
        max(float(timeout_seconds), 0.01)
        if isinstance(timeout_seconds, (int, float))
        else max(float(config.timeout_seconds), 0.01)
    )
    resolved = resolve_active_model(agent_id)
    if resolved is None:
        return None
    logger.info("Running active-model fallback task %s via %s", purpose, resolved.label)
    try:
        async def consume() -> str:
            return await consume_model_response(
                resolved.model,
                [
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_prompt},
                ],
                total_timeout_seconds=timeout,
            )

        text = (
            await run_primary_when_idle(agent_id, consume)
            if wait_for_idle
            else await consume()
        )
    except UtilityTaskTimeoutError:
        raise
    except asyncio.TimeoutError as exc:
        raise UtilityTaskTimeoutError(
            phase="total",
            timeout_seconds=timeout,
        ) from exc
    return UtilityTextResult(text=text, model_label=resolved.label)


async def run_memory_text_task(
    *,
    agent_id: str,
    system_prompt: str,
    user_prompt: str,
    purpose: str,
    timeout_seconds: float | None = None,
    require_json: bool = False,
) -> UtilityTextResult | None:
    """Run isolated post-reply memory analysis on configured intelligence.

    The call happens only after the visible reply and receives no conversation
    tools or mutable runtime state. A dedicated model is preferred when the
    user configured one; otherwise ``follow_active`` remains a real semantic
    memory lane instead of silently reducing every conversation to raw L0.
    A dedicated accelerator is preferred. If it is absent, times out, or
    returns unusable output, the active model performs the same isolated task
    later through the foreground-first idle lane.
    """

    config = load_config().utility_intelligence
    if not bool(getattr(config, "background_processing_enabled", True)):
        return None
    timeout = (
        max(float(timeout_seconds), 0.01)
        if isinstance(timeout_seconds, (int, float))
        else max(float(config.timeout_seconds), 0.01)
    )
    messages = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": user_prompt},
    ]

    async def consume(resolved: ResolvedUtilityModel) -> str:
        return await consume_model_response(
            resolved.model, messages, total_timeout_seconds=timeout,
        )

    if config.mode == "dedicated":
        try:
            utility = resolve_utility_model(config, agent_id)
        except Exception:
            logger.warning("Memory accelerator unavailable", exc_info=True)
            utility = None
        if utility is not None:
            logger.debug(
                "Running background memory task %s via accelerator %s",
                purpose,
                utility.label,
            )
            try:
                text = await consume(utility)
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.warning(
                    "Memory accelerator failed; deferring to active model",
                    exc_info=True,
                )
            else:
                if text.strip() and (
                    not require_json or _contains_json_value(text)
                ):
                    return UtilityTextResult(text=text, model_label=utility.label)

    try:
        active = (
            resolve_utility_model(config, agent_id)
            if config.mode == "follow_active"
            else resolve_active_model(agent_id)
        )
    except Exception:
        logger.warning("Active memory fallback unavailable", exc_info=True)
        return None
    if active is None:
        return None
    logger.debug(
        "Deferring background memory task %s to idle primary model %s",
        purpose,
        active.label,
    )
    try:
        text = await run_primary_when_idle(
            agent_id,
            lambda: consume(active),
        )
    except asyncio.CancelledError:
        raise
    except Exception:
        # Primary preemption is intentionally visible to the durable worker,
        # which requeues the job without consuming a retry attempt.
        raise
    if not text.strip() or (require_json and not _contains_json_value(text)):
        return None
    return UtilityTextResult(text=text, model_label=active.label)
