import asyncio
import os
import subprocess
import sys
from types import SimpleNamespace
from unittest.mock import AsyncMock
import pytest
from lantai.app.channels.sip import SIPChannel


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", ["stop", "call_end"])
async def test_cleanup_waits_for_cancelled_reader(operation):
    channel = SIPChannel(AsyncMock())
    session = channel.session_mgr.create_session("call", "from", "to")
    session.status = "active"
    session.stt_engine = type("STT", (), {"stop": AsyncMock()})()
    entered, cleanup, release = (
        asyncio.Event(),
        asyncio.Event(),
        asyncio.Event(),
    )

    async def reader():
        entered.set()
        try:
            await asyncio.Event().wait()
        finally:
            cleanup.set()
            await release.wait()

    task = asyncio.create_task(reader())
    channel._call_tasks["call"] = task
    await entered.wait()
    owner = asyncio.create_task(
        channel.stop()
        if operation == "stop"
        else channel._on_call_ended("call")
    )
    await cleanup.wait()
    for _ in range(5):
        await asyncio.sleep(0)
    try:
        assert (
            not owner.done()
        ), "cleanup returned while audio reader still owns work"
        session.stt_engine.stop.assert_not_awaited()
    finally:
        release.set()
        await asyncio.gather(owner, task, return_exceptions=True)


@pytest.mark.asyncio
async def test_idle_timeout_does_not_cancel_its_own_cleanup():
    channel = SIPChannel(AsyncMock())
    session = channel.session_mgr.create_session("call", "from", "to")
    session.status = "active"
    entered, release = asyncio.Event(), asyncio.Event()

    async def close_stt():
        entered.set()
        await release.wait()

    session.stt_engine = type("STT", (), {"stop": staticmethod(close_stt)})()
    timeout = asyncio.create_task(channel._call_timeout("call", 0))
    channel._timeout_tasks["call"] = timeout
    await entered.wait()
    for _ in range(5):
        await asyncio.sleep(0)
    try:
        assert not timeout.done(), "timeout cancelled its own STT shutdown"
    finally:
        release.set()
        await asyncio.gather(timeout, return_exceptions=True)
    assert channel.session_mgr.get_session("call") is None


@pytest.mark.asyncio
async def test_concurrent_stop_and_end_share_shutdown_despite_caller_cancellation():
    channel = SIPChannel(AsyncMock())
    channel.backend = type("Backend", (), {"stop": AsyncMock()})()
    session = channel.session_mgr.create_session("call", "from", "to")
    session.status = "active"
    entered, release = asyncio.Event(), asyncio.Event()

    async def close_stt():
        entered.set()
        await release.wait()

    session.stt_engine = type(
        "STT", (), {"stop": AsyncMock(side_effect=close_stt)}
    )()
    ending = asyncio.create_task(channel._on_call_ended("call"))
    await entered.wait()
    stopping = asyncio.create_task(channel.stop())
    await asyncio.sleep(0)
    for _ in range(3):
        stopping.cancel()
        ending.cancel()
        await asyncio.sleep(0)
    try:
        assert not ending.done()
        assert not stopping.done()
        channel.backend.stop.assert_not_awaited()
        assert session.tts_abort.is_set()
    finally:
        release.set()
        await asyncio.gather(ending, stopping, return_exceptions=True)
    session.stt_engine.stop.assert_awaited_once()
    channel.backend.stop.assert_awaited_once()
    assert not channel._call_tasks
    assert not channel._timeout_tasks
    assert channel.session_mgr.get_session("call") is None


@pytest.mark.asyncio
async def test_stop_rejects_new_call_before_provider_setup():
    channel = SIPChannel(AsyncMock())
    await channel.stop()
    # No config/provider exists: any attempted setup would fail here.
    await channel._on_incoming_call(
        "late", "from", "to", asyncio.Queue(), AsyncMock()
    )
    assert channel.session_mgr.get_session("late") is None


@pytest.mark.asyncio
@pytest.mark.parametrize("stop_again", [False, True])
async def test_restart_cannot_overtake_previous_shutdown(stop_again):
    c = SIPChannel(AsyncMock())
    c._config = SimpleNamespace(enabled=True, sip_mode="livekit")

    class Backend:
        running = True

        async def start(self):
            self.running = True

        async def stop(self):
            self.running = False

    c.backend = Backend()
    s = c.session_mgr.create_session("call", "from", "to")
    s.status = "active"
    entered, release = asyncio.Event(), asyncio.Event()

    async def stop_stt():
        entered.set()
        await release.wait()

    s.stt_engine = SimpleNamespace(stop=stop_stt)
    stopping = asyncio.create_task(c.stop())
    await entered.wait()
    restarting = asyncio.create_task(c.start())
    await asyncio.sleep(0)
    early = restarting.done()
    if stop_again:
        latest_stop = asyncio.create_task(c.stop())
        await asyncio.sleep(0)
    release.set()
    await asyncio.gather(stopping, restarting)
    if stop_again:
        await latest_stop
    assert (
        not early
    ), "restart reopened admission while previous stop still owned STT cleanup"
    assert c.backend.running is (not stop_again)
    assert c._stopping is stop_again


def test_existing_cleanup_and_timeout_do_not_wait_on_each_other():
    code = """
import asyncio, os
from unittest.mock import AsyncMock
from lantai.app.channels.sip import SIPChannel
async def main():
    c=SIPChannel(AsyncMock())
    s=c.session_mgr.create_session('call','from','to'); s.status='active'
    s.stt_engine=type('STT',(),{'stop':AsyncMock()})()
    entered,release=asyncio.Event(),asyncio.Event()
    original=asyncio.sleep
    async def sleep(delay):
        if delay==9876:
            entered.set(); await release.wait()
        else: await original(delay)
    asyncio.sleep=sleep
    t=asyncio.create_task(c._call_timeout('call',9876)); c._timeout_tasks['call']=t
    await entered.wait()
    e=asyncio.create_task(c._on_call_ended('call'))
    release.set()
    done,pending=await asyncio.wait([e,t],timeout=.2)
    print('pending=',len(pending),'stt_stops=',s.stt_engine.stop.await_count,flush=True)
    os._exit(1 if pending else 0)
asyncio.run(main())
"""
    r = subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True,
        text=True,
        timeout=5,
        env=os.environ.copy(),
    )
    assert r.returncode == 0, r.stdout + r.stderr
