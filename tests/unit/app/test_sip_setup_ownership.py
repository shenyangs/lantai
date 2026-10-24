import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from lantai.app.channels import sip


def configured_channel(monkeypatch, engine):
    channel = sip.SIPChannel(AsyncMock())
    channel._config = SimpleNamespace(
        max_concurrent_calls=0,
        stt_provider="test",
        language="zh",
        dashscope_api_key="",
        sip_mode="livekit",
        tts_provider="test",
        tts_voice="test",
        welcome_greeting="",
        call_timeout=0,
    )
    monkeypatch.setattr(sip, "create_stt_engine", lambda *args: engine)
    return channel


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", ["stop", "end", "cancel"])
async def test_shutdown_drains_incoming_setup_before_closing_stt(
    monkeypatch, operation
):
    entered, cleaning, release = (
        asyncio.Event(),
        asyncio.Event(),
        asyncio.Event(),
    )

    async def start():
        entered.set()
        try:
            await asyncio.Event().wait()
        finally:
            cleaning.set()
            await release.wait()

    engine = SimpleNamespace(start=start, stop=AsyncMock())
    channel = configured_channel(monkeypatch, engine)
    incoming = asyncio.create_task(
        channel._on_incoming_call(
            "call", "from", "to", asyncio.Queue(), AsyncMock()
        )
    )
    await entered.wait()
    if operation == "cancel":
        incoming.cancel()
        ending = incoming
    else:
        ending = asyncio.create_task(
            channel.stop()
            if operation == "stop"
            else channel._on_call_ended("call")
        )
    for _ in range(12):
        await asyncio.sleep(0)
    try:
        assert cleaning.is_set(), "shutdown left STT initialization running"
        assert not ending.done(), "shutdown returned before setup settled"
        engine.stop.assert_not_awaited()
    finally:
        release.set()
        if not incoming.done() and not cleaning.is_set():
            incoming.cancel()
        await asyncio.gather(incoming, ending, return_exceptions=True)
    engine.stop.assert_awaited_once()
    assert channel.session_mgr.get_session("call") is None
    assert not channel._call_tasks
    assert not channel._write_fns


@pytest.mark.asyncio
async def test_failed_stt_start_closes_partial_engine(monkeypatch):
    engine = SimpleNamespace(
        start=AsyncMock(
            side_effect=RuntimeError("synthetic initialization failure")
        ),
        stop=AsyncMock(),
    )
    channel = configured_channel(monkeypatch, engine)

    async def empty_speech(*args, **kwargs):
        if False:
            yield b""

    monkeypatch.setattr(sip, "synthesize_tts_stream", empty_speech)
    await channel._on_incoming_call(
        "call", "from", "to", asyncio.Queue(), AsyncMock()
    )
    engine.stop.assert_awaited_once()
    assert channel.session_mgr.get_session("call") is None


@pytest.mark.asyncio
async def test_duplicate_incoming_does_not_replace_initializing_call(
    monkeypatch,
):
    entered, release = asyncio.Event(), asyncio.Event()

    async def start():
        entered.set()
        await release.wait()

    engine = SimpleNamespace(
        start=AsyncMock(side_effect=start), stop=AsyncMock()
    )
    channel = configured_channel(monkeypatch, engine)
    first = asyncio.create_task(
        channel._on_incoming_call(
            "call", "from", "to", asyncio.Queue(), AsyncMock()
        )
    )
    await entered.wait()
    original = channel.session_mgr.get_session("call")
    second = asyncio.create_task(
        channel._on_incoming_call(
            "call", "other", "to", asyncio.Queue(), AsyncMock()
        )
    )
    await asyncio.sleep(0)
    try:
        assert second.done()
        assert channel.session_mgr.get_session("call") is original
        engine.start.assert_awaited_once()
    finally:
        release.set()
        await asyncio.gather(first, second, return_exceptions=True)
        await channel.stop()


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", ["stop", "end", "cancel"])
async def test_late_success_after_setup_cancellation_does_not_start_reader(
    monkeypatch, operation
):
    entered = asyncio.Event()

    async def start():
        entered.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            return

    engine = SimpleNamespace(start=start, stop=AsyncMock())
    channel = configured_channel(monkeypatch, engine)
    channel._audio_reader = AsyncMock()
    incoming = asyncio.create_task(
        channel._on_incoming_call(
            "call", "from", "to", asyncio.Queue(), AsyncMock()
        )
    )
    await entered.wait()
    if operation == "stop":
        await channel.stop()
    elif operation == "end":
        await channel._on_call_ended("call")
    else:
        incoming.cancel()
    await asyncio.gather(incoming, return_exceptions=True)
    channel._audio_reader.assert_not_called()
    assert not channel._call_tasks
    engine.stop.assert_awaited_once()


@pytest.mark.asyncio
async def test_cancel_then_stop_does_not_interrupt_setup_cleanup(monkeypatch):
    c = sip.SIPChannel(AsyncMock())
    c._config = SimpleNamespace(
        max_concurrent_calls=0,
        stt_provider="test",
        language="zh",
        dashscope_api_key="",
        sip_mode="livekit",
        tts_provider="test",
        tts_voice="test",
        welcome_greeting="",
        call_timeout=0,
    )
    entered, cleaning, release, cleaned = [asyncio.Event() for _ in range(4)]

    async def start():
        entered.set()
        try:
            await asyncio.Event().wait()
        finally:
            cleaning.set()
            await release.wait()
            cleaned.set()

    engine = SimpleNamespace(start=start, stop=AsyncMock())
    monkeypatch.setattr(sip, "create_stt_engine", lambda *args: engine)
    incoming = asyncio.create_task(
        c._on_incoming_call("call", "from", "to", asyncio.Queue(), AsyncMock())
    )
    await entered.wait()
    incoming.cancel()
    await cleaning.wait()
    stopping = asyncio.create_task(c.stop())
    for _ in range(20):
        await asyncio.sleep(0)
    stopped_early = engine.stop.await_count
    try:
        assert (
            stopped_early == 0
        ), "second cancellation interrupted setup cleanup and closed STT early"
    finally:
        release.set()
        await asyncio.gather(incoming, stopping, return_exceptions=True)
    assert cleaned.is_set()
