import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from lantai.app.channels.sip import SIPChannel


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", ["stop", "end"])
async def test_reset_timeout_keeps_cleanup_owned_until_call_ends(operation):
    c = SIPChannel(AsyncMock())
    c._config = SimpleNamespace(call_timeout=0)
    session = c.session_mgr.create_session("call", "from", "to")
    session.status = "active"
    session.stt_engine = SimpleNamespace(stop=AsyncMock())
    entered, cleaning, release, cleaned = [asyncio.Event() for _ in range(4)]

    async def timeout():
        entered.set()
        try:
            await asyncio.Event().wait()
        finally:
            cleaning.set()
            await release.wait()
            cleaned.set()

    retired = asyncio.create_task(timeout())
    c._timeout_tasks["call"] = retired
    await entered.wait()
    c._reset_timeout("call")
    await cleaning.wait()
    ending = asyncio.create_task(
        c.stop() if operation == "stop" else c._on_call_ended("call")
    )
    for _ in range(15):
        await asyncio.sleep(0)
    try:
        assert not ending.done(), "hangup detached retired timeout cleanup"
        session.stt_engine.stop.assert_not_awaited()
    finally:
        release.set()
        await asyncio.gather(ending, retired, return_exceptions=True)
    assert (
        cleaned.is_set()
    ), "hangup interrupted cleanup with a second cancellation"
    session.stt_engine.stop.assert_awaited_once()
