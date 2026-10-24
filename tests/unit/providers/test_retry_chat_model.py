# -*- coding: utf-8 -*-

from __future__ import annotations

import pytest

from lantai.providers.retry_chat_model import (
    RateLimitConfig,
    RetryChatModel,
    RetryConfig,
)


class _RetryableError(Exception):
    status_code = 500


class _FakeLimiter:
    def __init__(self) -> None:
        self.acquired = 0
        self.released = 0

    async def acquire(self) -> None:
        self.acquired += 1

    def release(self) -> None:
        self.released += 1

    async def report_rate_limit(self, _retry_after=None) -> None:
        return None


@pytest.mark.asyncio
async def test_legacy_retry_model_does_not_replay_after_first_stream_output(
    monkeypatch,
) -> None:
    limiter = _FakeLimiter()

    async def fake_get_rate_limiter(**_kwargs):
        return limiter

    monkeypatch.setattr(
        "lantai.providers.retry_chat_model.get_rate_limiter",
        fake_get_rate_limiter,
    )

    class Inner:
        model_name = "test-model"
        stream = True
        _provider_id = "test-provider"

        def __init__(self) -> None:
            self.calls = 0

        async def __call__(self, *_args, **_kwargs):
            self.calls += 1

            async def generate():
                yield "partial"
                raise _RetryableError("stream failed after visible output")

            return generate()

    inner = Inner()
    model = RetryChatModel(
        inner,
        retry_config=RetryConfig(
            enabled=True,
            max_retries=2,
            backoff_base=0.1,
            backoff_cap=0.5,
        ),
        rate_limit_config=RateLimitConfig(
            max_concurrent=1,
            max_qpm=0,
            acquire_timeout=10,
        ),
    )

    stream = await model(messages=[])
    output = []
    with pytest.raises(_RetryableError, match="visible output"):
        async for chunk in stream:
            output.append(chunk)

    assert output == ["partial"]
    assert inner.calls == 1


@pytest.mark.asyncio
async def test_legacy_retry_model_retries_before_first_stream_output(
    monkeypatch,
) -> None:
    limiter = _FakeLimiter()

    async def fake_get_rate_limiter(**_kwargs):
        return limiter

    monkeypatch.setattr(
        "lantai.providers.retry_chat_model.get_rate_limiter",
        fake_get_rate_limiter,
    )

    class Inner:
        model_name = "test-model"
        stream = True
        _provider_id = "test-provider"

        def __init__(self) -> None:
            self.calls = 0

        async def __call__(self, *_args, **_kwargs):
            self.calls += 1
            attempt = self.calls

            async def generate():
                if attempt == 1:
                    raise _RetryableError("stream failed before output")
                yield "ok"

            return generate()

    inner = Inner()
    model = RetryChatModel(
        inner,
        retry_config=RetryConfig(
            enabled=True,
            max_retries=1,
            backoff_base=0.1,
            backoff_cap=0.5,
        ),
        rate_limit_config=RateLimitConfig(
            max_concurrent=1,
            max_qpm=0,
            acquire_timeout=10,
        ),
    )

    stream = await model(messages=[])
    output = [chunk async for chunk in stream]

    assert output == ["ok"]
    assert inner.calls == 2
