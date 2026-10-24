# -*- coding: utf-8 -*-
"""Wrappers for LantaiModelClient implementations."""

from __future__ import annotations

from lantai.utils.async_iterator import closing_async_iterator

import asyncio
import logging
from collections.abc import AsyncIterator
from types import SimpleNamespace
from typing import Any

from lantai.exceptions import RateLimitExceededException

from ..providers.rate_limiter import get_rate_limiter
from ..providers.tokendance_authorization import (
    TokenDanceAuthorization,
    assert_tokendance_authorization_current,
)
from ..providers.retry_chat_model import (
    RateLimitConfig,
    RetryConfig,
    _compute_backoff,
    _extract_retry_after,
    _is_rate_limit,
    _is_retryable,
    _normalize_rate_limit_config,
    _normalize_retry_config,
)
from ..token_usage.model_wrapper import TokenRecordingModelWrapper
from .cost_accounting import (
    CostEvidence,
    finalize_model_dispatch_budget,
    mark_model_dispatch_budget_ambiguous,
    record_model_usage_cost,
    reserve_model_dispatch_budget,
)
from .models import (
    LantaiModelClient,
    ModelRequest,
    ModelResponse,
    ModelStreamEvent,
    ModelUsage,
)

logger = logging.getLogger(__name__)

DEFAULT_MODEL_FAILOVER_FIRST_EVENT_TIMEOUT_SECONDS = 30.0


def _model_access_failure(exc: Exception) -> bool:
    """Return whether a model call failed before execution due to access.

    Providers do not use one exception hierarchy.  Preserve the original
    exception while checking the common HTTP attributes and the normalized
    Lantai error contract, including nested causes.
    """

    pending: list[BaseException] = [exc]
    seen: set[int] = set()
    messages: list[str] = []
    statuses: set[int] = set()
    codes: set[str] = set()
    while pending:
        current = pending.pop()
        if id(current) in seen:
            continue
        seen.add(id(current))
        messages.append(str(current).lower())
        for attr in ("status_code", "status"):
            value = getattr(current, attr, None)
            if isinstance(value, int):
                statuses.add(value)
        code = getattr(current, "code", None)
        if code:
            codes.add(str(code).lower())
        response = getattr(current, "response", None)
        response_status = getattr(response, "status_code", None)
        if isinstance(response_status, int):
            statuses.add(response_status)
        for nested in (
            getattr(current, "__cause__", None),
            getattr(current, "__context__", None),
        ):
            if isinstance(nested, BaseException):
                pending.append(nested)

    if statuses.intersection({401, 403, 404}):
        return True
    if codes.intersection(
        {
            "model_not_found",
            "model_unauthorized_access",
            "unauthorized",
            "forbidden",
        },
    ):
        return True
    text = " ".join(messages)
    return any(
        marker in text
        for marker in (
            "unauthorized access to model",
            "model not found",
            "no access to model",
            "access denied to model",
            "无权访问模型",
            "无访问模型权限",
            "模型不存在",
        )
    )


class ModelAccessFailoverClient:
    """Use one configured fallback when the primary model cannot start.

    Streaming failover is allowed only before the primary emits an event.  A
    provider cannot therefore replay visible text or a tool call after work
    has already started.
    """

    def __init__(
        self,
        primary: LantaiModelClient,
        fallback: LantaiModelClient,
        *,
        first_event_timeout_seconds: float | None = (
            DEFAULT_MODEL_FAILOVER_FIRST_EVENT_TIMEOUT_SECONDS
        ),
    ) -> None:
        self._primary = primary
        self._fallback = fallback
        self._first_event_timeout_seconds = (
            max(0.0, float(first_event_timeout_seconds))
            if first_event_timeout_seconds is not None
            else None
        )
        self._primary_failure_reason: str | None = None
        self.provider_id = primary.provider_id
        self.model_name = primary.model_name
        self.routing_snapshot = dict(
            getattr(primary, "routing_snapshot", {}) or {}
        )

    def _log_failover(self, exc: Exception, *, reason: str) -> None:
        logger.warning(
            "Primary model %s/%s could not start (%s); falling back once to "
            "%s/%s: %s",
            self._primary.provider_id,
            self._primary.model_name,
            reason,
            self._fallback.provider_id,
            self._fallback.model_name,
            exc,
        )

    @staticmethod
    async def _close_stream(stream: Any) -> None:
        close = getattr(stream, "aclose", None)
        if not callable(close):
            return
        try:
            await close()
        except Exception:  # pragma: no cover - best-effort provider cleanup
            pass

    def _remember_primary_failure(self, exc: Exception, *, reason: str) -> None:
        self._primary_failure_reason = reason
        self._log_failover(exc, reason=reason)

    async def _stream_fallback(
        self,
        request: ModelRequest,
        *,
        reason: str,
    ) -> AsyncIterator[ModelStreamEvent]:
        first_fallback_event = True
        async with closing_async_iterator(
            self._fallback.stream(request)
        ) as owned_stream_5:
            async for event in owned_stream_5:
                if first_fallback_event:
                    event.metadata.setdefault(
                        "model_failover",
                        {
                            "from_provider": self._primary.provider_id,
                            "from_model": self._primary.model_name,
                            "to_provider": self._fallback.provider_id,
                            "to_model": self._fallback.model_name,
                            "reason": reason,
                        },
                    )
                    first_fallback_event = False
                yield event

    async def complete(self, request: ModelRequest) -> ModelResponse:
        if self._primary_failure_reason is not None:
            return await self._fallback.complete(request)
        try:
            return await self._primary.complete(request)
        except Exception as exc:
            if not _model_access_failure(exc):
                raise
            self._remember_primary_failure(
                exc,
                reason="primary_model_inaccessible",
            )
            return await self._fallback.complete(request)

    async def stream(
        self,
        request: ModelRequest,
    ) -> AsyncIterator[ModelStreamEvent]:
        if self._primary_failure_reason is not None:
            async with closing_async_iterator(
                self._stream_fallback(
                    request,
                    reason=self._primary_failure_reason,
                )
            ) as owned_stream_4:
                async for event in owned_stream_4:
                    yield event
            return

        stream = self._primary.stream(request)
        try:
            iterator = stream.__aiter__()
            try:
                first_event_awaitable = iterator.__anext__()
                if self._first_event_timeout_seconds is None:
                    first_event = await first_event_awaitable
                else:
                    first_event = await asyncio.wait_for(
                        first_event_awaitable,
                        timeout=self._first_event_timeout_seconds,
                    )
            except StopAsyncIteration:
                exc = RuntimeError(
                    "Primary model stream ended before its first event"
                )
                reason = "primary_model_empty_stream"
                self._remember_primary_failure(exc, reason=reason)
            except asyncio.TimeoutError as exc:
                reason = "primary_model_first_event_timeout"
                self._remember_primary_failure(exc, reason=reason)
            except Exception as exc:
                if not _model_access_failure(exc):
                    raise
                reason = "primary_model_inaccessible"
                self._remember_primary_failure(exc, reason=reason)
            else:
                yield first_event
                async for event in iterator:
                    yield event
                return

        finally:
            await self._close_stream(stream)

        async with closing_async_iterator(
            self._stream_fallback(request, reason=reason)
        ) as owned_stream_3:
            async for event in owned_stream_3:
                yield event


class TokenRecordingModelClient:
    """Record token usage emitted by a LantaiModelClient."""

    def __init__(self, inner: LantaiModelClient) -> None:
        self._inner = inner
        self.provider_id = inner.provider_id
        self.model_name = inner.model_name
        self.routing_snapshot = dict(
            getattr(inner, "routing_snapshot", {}) or {}
        )
        self.model_transport_policy_receipt = dict(
            getattr(inner, "model_transport_policy_receipt", {}) or {}
        )
        self._recorder = TokenRecordingModelWrapper(
            self.provider_id,
            SimpleNamespace(model_name=self.model_name, stream=True),
        )

    def _record_usage(
        self,
        request: ModelRequest,
        usage: ModelUsage | dict[str, Any] | None,
    ) -> CostEvidence | None:
        if usage is not None:
            payload = usage.model_dump() if isinstance(usage, ModelUsage) else usage
            try:
                self._recorder._record_usage(  # pylint: disable=protected-access
                    payload,
                )
            except Exception:  # pragma: no cover - telemetry must never fail a call
                logger.exception("Failed to record aggregate model token usage")
        try:
            return record_model_usage_cost(
                provider_id=self.provider_id,
                request=request,
                usage=usage,
            )
        except Exception:  # pragma: no cover - evidence failure is observable in logs
            logger.exception("Failed to record model cost evidence")
            return None

    async def stream(
        self,
        request: ModelRequest,
    ) -> AsyncIterator[ModelStreamEvent]:
        reservation = await reserve_model_dispatch_budget(
            request=request,
            provider_id=self.provider_id,
            model_id=self.model_name,
        )
        from ..research.context import native_dispatch

        last_usage: ModelUsage | None = None
        completed = False
        try:
            async with closing_async_iterator(
                native_dispatch(self._inner).stream(request)
            ) as owned_stream_2:
                async for event in owned_stream_2:
                    if event.usage is not None:
                        last_usage = event.usage
                    yield event
            completed = True
        finally:
            if not completed:
                # Closing a locally retired stream skips the normal epilogue.
                # Persist any trustworthy billing record already observed,
                # then retain an UNKNOWN row for the unconfirmed remote tail:
                # local iterator closure is not proof that provider billing
                # stopped at the same instant.
                if last_usage is not None:
                    self._record_usage(request, last_usage)
                self._record_usage(request, None)
                mark_model_dispatch_budget_ambiguous(reservation)
        evidence = self._record_usage(request, last_usage)
        finalize_model_dispatch_budget(reservation, evidence=evidence)

    async def complete(self, request: ModelRequest) -> ModelResponse:
        reservation = await reserve_model_dispatch_budget(
            request=request,
            provider_id=self.provider_id,
            model_id=self.model_name,
        )
        try:
            from ..research.context import native_dispatch

            response = await native_dispatch(self._inner).complete(request)
        except BaseException:
            mark_model_dispatch_budget_ambiguous(reservation)
            raise
        evidence = self._record_usage(request, response.usage)
        finalize_model_dispatch_budget(reservation, evidence=evidence)
        return response


class RetryingModelClient:
    """Retry and rate-limit wrapper for LantaiModelClient."""

    def __init__(
        self,
        inner: LantaiModelClient,
        *,
        retry_config: RetryConfig | None = None,
        rate_limit_config: RateLimitConfig | None = None,
    ) -> None:
        self._inner = inner
        self.provider_id = inner.provider_id
        self.model_name = inner.model_name
        self.routing_snapshot = dict(
            getattr(inner, "routing_snapshot", {}) or {}
        )
        self.model_transport_policy_receipt = dict(
            getattr(inner, "model_transport_policy_receipt", {}) or {}
        )
        self._retry_config = _normalize_retry_config(retry_config)
        self._rate_limit_config = _normalize_rate_limit_config(
            rate_limit_config,
        )
        configured_retries = (
            self._retry_config.max_retries
            if self._retry_config.enabled
            else 0
        )
        self.model_transport_policy_receipt["runtime_retry"] = {
            "owner": "lantai.runtime.model_wrappers.RetryingModelClient",
            "max_retries_before_first_event": configured_retries,
            "max_dispatches_before_first_event": configured_retries + 1,
            # A Native safe-current-round recovery is already a new HTTP
            # dispatch.  It must not silently acquire another full retry
            # allowance and multiply the logical generation's budget.
            "recovery_dispatch_max_retries": 0,
        }

    async def _limiter(self):
        return await get_rate_limiter(
            max_concurrent=self._rate_limit_config.max_concurrent,
            max_qpm=self._rate_limit_config.max_qpm,
            default_pause_seconds=self._rate_limit_config.pause_seconds,
            jitter_range=self._rate_limit_config.jitter_range,
            scope_key=self.provider_id,
        )

    async def _acquire(self, limiter: Any, operation: str) -> None:
        try:
            await asyncio.wait_for(
                limiter.acquire(),
                timeout=self._rate_limit_config.acquire_timeout,
            )
        except asyncio.TimeoutError as exc:
            raise RateLimitExceededException(
                operation=operation,
                retry_after=int(self._rate_limit_config.acquire_timeout),
                details={"reason": "Timed out waiting for execution slot"},
            ) from exc

    def _dispatch_request(
        self,
        request: ModelRequest,
        *,
        attempt: int,
        max_attempts: int,
    ) -> ModelRequest:
        metadata = dict(request.metadata)
        metadata["runtime_model_dispatch_attempt"] = attempt
        metadata["runtime_model_dispatch_max_attempts"] = max_attempts
        base_id = str(metadata.get("model_request_id") or "").strip()
        if base_id:
            metadata["runtime_model_dispatch_id"] = (
                f"{base_id}:provider:{self.provider_id}:model:{self.model_name}:"
                f"dispatch:{attempt}"
            )
        return request.model_copy(
            update={
                "provider_id": self.provider_id,
                "model": self.model_name,
                "metadata": metadata,
            }
        )

    def _attempts_for_request(self, request: ModelRequest) -> int:
        recovery_attempt = request.metadata.get(
            "runtime_model_stream_recovery_attempt"
        )
        try:
            is_native_recovery = int(recovery_attempt or 0) > 0
        except (TypeError, ValueError):
            is_native_recovery = False
        if is_native_recovery:
            return 1
        return (
            self._retry_config.max_retries + 1
            if self._retry_config.enabled
            else 1
        )

    async def complete(self, request: ModelRequest) -> ModelResponse:
        limiter = await self._limiter()
        attempts = self._attempts_for_request(request)
        for attempt in range(1, attempts + 1):
            acquired = False
            try:
                await self._acquire(limiter, "LLM execution")
                acquired = True
                return await self._inner.complete(
                    self._dispatch_request(
                        request,
                        attempt=attempt,
                        max_attempts=attempts,
                    )
                )
            except Exception as exc:
                if _is_retryable(exc) and _is_rate_limit(exc):
                    await limiter.report_rate_limit(_extract_retry_after(exc))
                if not _is_retryable(exc) or attempt >= attempts:
                    raise
                delay = _compute_backoff(attempt, self._retry_config)
                logger.warning(
                    "Native LLM call failed (attempt %d/%d): %s. "
                    "Retrying in %.1fs ...",
                    attempt,
                    attempts,
                    exc,
                    delay,
                )
                await asyncio.sleep(delay)
            finally:
                if acquired:
                    limiter.release()
        raise RuntimeError("unreachable native LLM retry state")

    async def stream(
        self,
        request: ModelRequest,
    ) -> AsyncIterator[ModelStreamEvent]:
        limiter = await self._limiter()
        attempts = self._attempts_for_request(request)
        for attempt in range(1, attempts + 1):
            acquired = False
            emitted_event = False
            try:
                await self._acquire(limiter, "LLM execution")
                acquired = True
                async with closing_async_iterator(
                    self._inner.stream(
                        self._dispatch_request(
                            request,
                            attempt=attempt,
                            max_attempts=attempts,
                        )
                    )
                ) as owned_stream_1:
                    async for event in owned_stream_1:
                        emitted_event = True
                        yield event
                return
            except Exception as exc:
                if acquired:
                    acquired = False
                    limiter.release()
                if emitted_event:
                    raise
                if _is_retryable(exc) and _is_rate_limit(exc):
                    await limiter.report_rate_limit(_extract_retry_after(exc))
                if not _is_retryable(exc) or attempt >= attempts:
                    raise
                delay = _compute_backoff(attempt, self._retry_config)
                logger.warning(
                    "Native LLM stream failed (attempt %d/%d): %s. "
                    "Retrying in %.1fs ...",
                    attempt,
                    attempts,
                    exc,
                    delay,
                )
                await asyncio.sleep(delay)
            finally:
                if acquired:
                    limiter.release()


class TokenDanceBoundModelClient:
    """Discard late TokenDance results after identity or key rebinding.

    The authorization is captured before the request.  Every awaited response
    boundary is checked against current trusted Watcha and provider state
    before Runtime can observe or persist the result.
    """

    def __init__(
        self,
        inner: LantaiModelClient,
        *,
        manager: Any,
        authorization: TokenDanceAuthorization,
        routing_snapshot: dict[str, Any],
    ) -> None:
        self._inner = inner
        self._manager = manager
        self._authorization = authorization
        self.provider_id = inner.provider_id
        self.model_name = inner.model_name
        self.routing_snapshot = dict(routing_snapshot)

    async def complete(self, request: ModelRequest) -> ModelResponse:
        response = await self._inner.complete(request)
        assert_tokendance_authorization_current(
            self._manager,
            self._authorization,
        )
        return response

    async def stream(
        self,
        request: ModelRequest,
    ) -> AsyncIterator[ModelStreamEvent]:
        async with closing_async_iterator(
            self._inner.stream(request)
        ) as owned_stream_0:
            async for event in owned_stream_0:
                assert_tokendance_authorization_current(
                    self._manager,
                    self._authorization,
                )
                yield event
