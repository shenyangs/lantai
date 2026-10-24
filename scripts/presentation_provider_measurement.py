# -*- coding: utf-8 -*-
"""Numeric-only model measurements for an isolated presentation smoke child.

Observe the existing wrapper boundary without issuing, retrying or draining a
model call. Invocation counts do not count HTTP requests hidden inside a model.
Missing usage stays unknown; sums and percentiles describe observed samples.
"""

from __future__ import annotations

import asyncio
import math
import threading
import time
from collections.abc import Mapping
from contextvars import ContextVar
from decimal import Decimal, InvalidOperation
from typing import Any


def _field(value: Any, name: str) -> Any:
    try:
        return (
            value.get(name)
            if isinstance(value, Mapping)
            else getattr(value, name, None)
        )
    except Exception:
        return None


def _integer(value: Any) -> int | None:
    return (
        value
        if isinstance(value, int)
        and not isinstance(value, bool)
        and value >= 0
        else None
    )


def _number(value: Any) -> float | None:
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        return None
    try:
        result = float(value)
    except (OverflowError, ValueError):
        return None
    return result if math.isfinite(result) and result >= 0 else None


def _reported(value: Any, *paths: tuple[str, ...]) -> Any:
    for source in (value, _field(value, "metadata")):
        for path in paths:
            raw = source
            for key in path:
                raw = _field(raw, key)
                if raw is None:
                    break
            if raw is not None:
                return raw
    return None


def read_reported_usage(value: Any) -> dict[str, Any]:
    """Read named usage fields without retaining prompts or object dumps."""
    input_tokens = _integer(
        _reported(
            value,
            *(
                (key,)
                for key in (
                    "input_tokens",
                    "prompt_tokens",
                    "inputTokens",
                    "promptTokens",
                )
            ),
        ),
    )
    output_tokens = _integer(
        _reported(
            value,
            *(
                (key,)
                for key in (
                    "output_tokens",
                    "completion_tokens",
                    "outputTokens",
                    "completionTokens",
                )
            ),
        ),
    )
    total_tokens = _integer(
        _reported(value, ("total_tokens",), ("totalTokens",)),
    )
    total_source = (
        "provider_reported" if total_tokens is not None else "unknown"
    )
    consistent = None
    if input_tokens is not None and output_tokens is not None:
        if total_tokens is None:
            total_tokens = input_tokens + output_tokens
            total_source = "sum_reported_fields"
        else:
            consistent = total_tokens == input_tokens + output_tokens
    cached_tokens = _integer(
        _reported(
            value,
            ("cached_tokens",),
            ("prompt_tokens_details", "cached_tokens"),
            ("input_tokens_details", "cached_tokens"),
            ("input_token_details", "cached_tokens"),
            ("input_token_details", "cache_read"),
            ("cache_read_input_tokens",),
            ("cached_input_tokens",),
        ),
    )
    cost = _reported(value, ("cost_cny",))
    micro_cny = None
    if isinstance(cost, (int, float, Decimal)) and not isinstance(cost, bool):
        try:
            decimal = Decimal(str(cost)) * 1_000_000
            if (
                decimal.is_finite()
                and decimal >= 0
                and decimal == decimal.to_integral_value()
            ):
                micro_cny = int(decimal)
        except (InvalidOperation, OverflowError, ValueError):
            pass
    return {
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "total_tokens": total_tokens,
        "total_tokens_source": total_source,
        "reported_total_consistent": consistent,
        "cached_tokens": cached_tokens,
        "provider_cost_status": "exact"
        if micro_cny is not None
        else "unknown",
        "provider_cost_micro_cny": micro_cny,
    }


def _safe_usage(value: Any) -> dict[str, Any]:
    result = read_reported_usage(None)
    for name in ("input_tokens", "output_tokens", "cached_tokens"):
        result[name] = _integer(_field(value, name))
    total = _integer(_field(value, "total_tokens"))
    source = _field(value, "total_tokens_source")
    inputs, outputs = result["input_tokens"], result["output_tokens"]
    if source == "provider_reported" and total is not None:
        result["total_tokens"] = total
        result["total_tokens_source"] = source
        if inputs is not None and outputs is not None:
            result["reported_total_consistent"] = total == inputs + outputs
    elif (
        source == "sum_reported_fields"
        and inputs is not None
        and outputs is not None
        and total == inputs + outputs
    ):
        result["total_tokens"] = total
        result["total_tokens_source"] = source
    cost = _integer(_field(value, "provider_cost_micro_cny"))
    if _field(value, "provider_cost_status") == "exact" and cost is not None:
        result["provider_cost_micro_cny"] = cost
        result["provider_cost_status"] = "exact"
    return result


def sanitize_report(value: Any) -> dict[str, Any]:
    """Recompute totals from safe calls; discard untrusted child totals."""
    raw_calls = _field(value, "calls")
    calls = []
    for index, call in enumerate(
        raw_calls if isinstance(raw_calls, list) else [],
    ):
        if not isinstance(call, Mapping):
            continue
        outcome = _field(call, "outcome")
        if outcome not in (
            "completed",
            "failed",
            "cancelled",
            "closed",
            "open",
        ):
            outcome = "open"
        calls.append(
            {
                "ordinal": _integer(_field(call, "ordinal")) or index + 1,
                "outcome": outcome,
                "duration_ms": _number(_field(call, "duration_ms"))
                if outcome != "open"
                else None,
                "usage_record_count": _integer(
                    _field(call, "usage_record_count"),
                )
                or 0,
                "measurement_error_count": _integer(
                    _field(call, "measurement_error_count"),
                )
                or 0,
                "stream_usage_snapshot_count": _integer(
                    _field(call, "stream_usage_snapshot_count"),
                )
                or 0,
                "usage_origin": _field(call, "usage_origin")
                if _field(call, "usage_origin")
                in ("wrapper_recorded", "stream_snapshot")
                else "unknown",
                "usage": _safe_usage(_field(call, "usage")),
            },
        )
    tokens = {}
    final_cost_coverage = bool(calls) and all(
        call["usage_origin"] == "wrapper_recorded"
        and call["outcome"] != "open"
        for call in calls
    )
    partial_count = sum(
        call["usage_origin"] == "stream_snapshot" for call in calls
    )
    for label in ("input", "output", "total", "cached"):
        known = [
            call["usage"][label + "_tokens"]
            for call in calls
            if call["usage"][label + "_tokens"] is not None
        ]
        tokens[label] = {
            "known_count": len(known),
            "unknown_count": len(calls) - len(known),
            "known_sum": sum(known),
            "total": sum(known)
            if calls
            and len(known) == len(calls)
            and not partial_count
            and all(call["outcome"] != "open" for call in calls)
            else None,
            "stream_snapshot_count": partial_count,
        }
    costs = [
        call["usage"]["provider_cost_micro_cny"]
        for call in calls
        if call["usage"]["provider_cost_status"] == "exact"
    ]
    durations = sorted(
        call["duration_ms"]
        for call in calls
        if call["duration_ms"] is not None
    )

    def percentile(fraction: float) -> float | None:
        return (
            durations[math.ceil(len(durations) * fraction) - 1]
            if durations
            else None
        )

    return {
        "schema_version": "1.0",
        "observed": _field(value, "observed") is True,
        "count_basis": "model_wrapper_invocations_http_retries_unobserved",
        "model_invocation_count": len(calls),
        "settled_invocation_count": sum(
            call["outcome"] != "open" for call in calls
        ),
        "calls": calls,
        "tokens": tokens,
        "billing": {
            "provider_cost_status": "not_applicable"
            if not calls
            else "exact"
            if len(costs) == len(calls) and final_cost_coverage
            else "unknown",
            "provider_cost_micro_cny": sum(costs)
            if len(costs) == len(calls) and final_cost_coverage
            else None,
            "known_subtotal_micro_cny": sum(costs),
            "known_count": len(costs),
            "unknown_count": len(calls) - len(costs),
            "stream_snapshot_count": partial_count,
        },
        "duration_ms": {
            "basis": (
                "invocation_start_through_terminal_or_explicit_close"
                "_all_outcomes"
            ),
            "percentile_method": "nearest_rank_observed_samples",
            "sample_count": len(durations),
            "p50": percentile(0.5),
            "p95": percentile(0.95),
            "min": durations[0] if durations else None,
            "max": durations[-1] if durations else None,
            "total": sum(durations) if durations else None,
        },
    }


# The stream adapter and patched closures share the observer's private state.
# pylint: disable=protected-access
class _ObservedStream:
    def __init__(
        self,
        observer: ProviderMeasurement,
        upstream: Any,
        entry: dict[str, Any],
    ):
        self.observer, self.upstream, self.entry = observer, upstream, entry

    def __aiter__(self):
        return self

    async def __anext__(self):
        token = self.observer._current.set(self.entry)
        try:
            chunk = await self.upstream.__anext__()
            self.observer._observe(
                self.entry,
                _field(chunk, "usage"),
                "stream_snapshot",
            )
            return chunk
        except StopAsyncIteration:
            self.observer._finish(self.entry, "completed")
            raise
        except BaseException as exc:
            self.observer._finish(
                self.entry,
                "cancelled"
                if isinstance(exc, asyncio.CancelledError)
                else "failed",
            )
            raise
        finally:
            self.observer._current.reset(token)

    async def aclose(self):
        token = self.observer._current.set(self.entry)
        try:
            close = getattr(self.upstream, "aclose", None)
            if close is not None:
                await close()
            self.observer._finish(self.entry, "closed")
        except BaseException as exc:
            self.observer._finish(
                self.entry,
                "cancelled"
                if isinstance(exc, asyncio.CancelledError)
                else "failed",
            )
            raise
        finally:
            self.observer._current.reset(token)


class ProviderMeasurement:
    """Observe one wrapper class temporarily in an isolated CLI child."""

    def __init__(self, wrapper_class: type):
        self.wrapper_class = wrapper_class
        self._current: ContextVar[Any] = ContextVar(
            "presentation_measurement",
            default=None,
        )
        self._lock = threading.RLock()
        self._entries: list[dict[str, Any]] = []
        self._entered = False

    def _finish(self, entry: dict[str, Any], outcome: str) -> None:
        with self._lock:
            if entry["outcome"] == "open":
                entry["outcome"] = outcome
                entry["duration_ms"] = max(
                    0.0,
                    (time.monotonic() - entry["_start"]) * 1000,
                )

    def _observe(self, entry: dict[str, Any], usage: Any, origin: str) -> None:
        with self._lock:
            if origin == "wrapper_recorded":
                entry["usage_record_count"] += 1
            if usage is None:
                return
            try:
                entry["usage"] = read_reported_usage(usage)
                entry["usage_origin"] = origin
                if origin == "stream_snapshot":
                    entry["stream_usage_snapshot_count"] += 1
            except Exception:
                entry["measurement_error_count"] += 1

    def __enter__(self):
        if self._entered or getattr(
            self.wrapper_class.__call__,
            "_presentation_observed",
            False,
        ):
            raise RuntimeError("wrapper is already observed")
        self._original_call = self.wrapper_class.__call__
        self._original_record = self.wrapper_class._record_usage
        observer = self

        async def observed_call(wrapper, *args, **kwargs):
            with observer._lock:
                entry = {
                    "ordinal": len(observer._entries) + 1,
                    "outcome": "open",
                    "_start": time.monotonic(),
                    "duration_ms": None,
                    "usage": read_reported_usage(None),
                    "usage_record_count": 0,
                    "measurement_error_count": 0,
                    "stream_usage_snapshot_count": 0,
                    "usage_origin": "unknown",
                }
                observer._entries.append(entry)
            token = observer._current.set(entry)
            try:
                result = await observer._original_call(
                    wrapper,
                    *args,
                    **kwargs,
                )
                if hasattr(result, "__aiter__"):
                    return _ObservedStream(observer, result, entry)
                observer._finish(entry, "completed")
                return result
            except BaseException as exc:
                observer._finish(
                    entry,
                    "cancelled"
                    if isinstance(exc, asyncio.CancelledError)
                    else "failed",
                )
                raise
            finally:
                observer._current.reset(token)

        def observed_record(wrapper, usage):
            entry = observer._current.get()
            if entry is not None:
                observer._observe(entry, usage, "wrapper_recorded")
            return observer._original_record(wrapper, usage)

        observed_call._presentation_observed = True
        self.wrapper_class.__call__ = observed_call
        self.wrapper_class._record_usage = observed_record
        self._entered = True
        return self

    def __exit__(self, *exc):
        self.wrapper_class.__call__ = self._original_call
        self.wrapper_class._record_usage = self._original_record

    def summary(self) -> dict[str, Any]:
        with self._lock:
            return sanitize_report(
                {"observed": self._entered, "calls": self._entries},
            )


# pylint: enable=protected-access
