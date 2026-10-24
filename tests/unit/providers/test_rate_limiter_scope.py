# -*- coding: utf-8 -*-
"""Behavior locks for provider-scoped LLM throttling."""

from __future__ import annotations

import pytest

from lantai.providers.rate_limiter import get_rate_limiter, reset_rate_limiter


@pytest.mark.asyncio
async def test_rate_limiters_are_shared_per_provider_and_isolated_across_providers() -> None:
    reset_rate_limiter()

    first = await get_rate_limiter(scope_key="provider-a", max_concurrent=1)
    same = await get_rate_limiter(scope_key="provider-a", max_concurrent=9)
    other = await get_rate_limiter(scope_key="provider-b", max_concurrent=1)

    assert first is same
    assert other is not first


@pytest.mark.asyncio
async def test_reset_rate_limiter_can_clear_only_one_provider() -> None:
    reset_rate_limiter()
    first = await get_rate_limiter(scope_key="provider-a")
    other = await get_rate_limiter(scope_key="provider-b")

    reset_rate_limiter("provider-a")

    assert await get_rate_limiter(scope_key="provider-a") is not first
    assert await get_rate_limiter(scope_key="provider-b") is other
