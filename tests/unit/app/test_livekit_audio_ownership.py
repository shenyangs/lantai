import asyncio
import sys
import types
import pytest
from lantai.app.channels.sip.livekit_backend import LiveKitBackend


class Stream:
    def __init__(self, release=None):
        self.release = release
        self.started = asyncio.Event()
        self.closed = False
        self.sent = False

    def __aiter__(self):
        return self

    async def __anext__(self):
        self.started.set()
        if self.sent:
            raise StopAsyncIteration
        self.sent = True
        if self.release:
            await self.release.wait()
        return types.SimpleNamespace(
            frame=types.SimpleNamespace(data=b"old-call")
        )

    async def aclose(self):
        self.closed = True


@pytest.fixture
def sdk(monkeypatch):
    monkeypatch.setitem(
        sys.modules,
        "livekit",
        types.SimpleNamespace(
            rtc=types.SimpleNamespace(AudioStream=lambda track: track)
        ),
    )


@pytest.mark.asyncio
async def test_old_reader_cannot_publish_into_next_call(sdk):
    b = LiveKitBackend()
    await b._notify_incoming("same-room", "A")
    release = asyncio.Event()
    stream = Stream(release)
    t = asyncio.create_task(b._read_audio_track("same-room", stream))
    await stream.started.wait()
    await b._notify_incoming("same-room", "B")
    release.set()
    await t
    assert (
        b._audio_queue.empty()
    ), "old audio and EOF were published into new call"


@pytest.mark.asyncio
async def test_stream_is_closed_on_normal_eof(sdk):
    b = LiveKitBackend()
    await b._notify_incoming("same-room", "A")
    stream = Stream()
    await b._read_audio_track("same-room", stream)
    assert stream.closed, "SDK AudioStream ownership not released"


@pytest.mark.asyncio
async def test_stop_drains_reader(sdk):
    b = LiveKitBackend()
    await b._notify_incoming("same-room", "A")
    stream = Stream(asyncio.Event())
    t = asyncio.create_task(b._read_audio_track("same-room", stream))
    await stream.started.wait()
    try:
        await b.stop()
        assert (
            t.done()
        ), "backend stop returned while reader still awaiting SDK"
    finally:
        t.cancel()
        await asyncio.gather(t, return_exceptions=True)


@pytest.mark.asyncio
async def test_one_track_eof_does_not_end_another_track(sdk):
    b = LiveKitBackend()
    await b._notify_incoming("same-room", "A")
    slow = Stream(asyncio.Event())
    t = asyncio.create_task(b._read_audio_track("same-room", slow))
    await slow.started.wait()
    try:
        await b._read_audio_track("same-room", Stream())
        items = []
        while not b._audio_queue.empty():
            items.append(b._audio_queue.get_nowait())
        assert (
            None not in items
        ), "one track terminates shared consumer while other reader active"
    finally:
        t.cancel()
        await asyncio.gather(t, return_exceptions=True)


@pytest.mark.asyncio
async def test_slow_consumer_has_bounded_frames_and_terminal(sdk):
    backend = LiveKitBackend()
    await backend._notify_incoming("room", "caller")
    queue = backend._audio_queue
    assert 0 < queue.maxsize <= 129
    for _ in range(queue.maxsize - 1):
        queue.put_nowait(b"audio")
    with pytest.raises(asyncio.QueueFull):
        queue.put_nowait(b"audio")
    queue.close()
    values = [queue.get_nowait() for _ in range(queue.qsize())]
    assert values[-1] is None
    assert values[:-1] == [b"audio"] * (queue.maxsize - 1)


@pytest.mark.asyncio
async def test_oversized_frame_is_rejected_before_retention(sdk):
    backend = LiveKitBackend()
    await backend._notify_incoming("room", "caller")
    queue = backend._audio_queue
    with pytest.raises(asyncio.QueueFull):
        queue.put_nowait(b"x" * (2 * 1024 * 1024 + 1))
    assert queue.empty()


@pytest.mark.asyncio
async def test_stale_playback_closure_cannot_play_into_next_call(sdk):
    backend = LiveKitBackend()
    writers = []

    async def incoming(*args):
        writers.append(args[-1])

    played = []

    async def play(*args):
        played.append(args)

    backend.on_incoming_call = incoming
    backend.play_audio = play
    await backend._notify_incoming("room", "first")
    await backend._notify_incoming("room", "second")
    await writers[0](b"old voice")
    await writers[1](b"new voice")
    assert played == [("room", b"new voice")]


@pytest.mark.asyncio
async def test_stop_waits_for_sdk_close_despite_repeated_cancellation(sdk):
    backend = LiveKitBackend()
    await backend._notify_incoming("room", "caller")
    stream = Stream(asyncio.Event())
    close_started, release_close = asyncio.Event(), asyncio.Event()

    async def close():
        close_started.set()
        await release_close.wait()
        stream.closed = True

    stream.aclose = close
    reader = asyncio.create_task(backend._read_audio_track("room", stream))
    await stream.started.wait()
    stopping = asyncio.create_task(backend.stop())
    await close_started.wait()
    stopping.cancel()
    await asyncio.sleep(0)
    stopping.cancel()
    await asyncio.sleep(0)
    assert not stopping.done()
    assert not reader.done()
    release_close.set()
    with pytest.raises(asyncio.CancelledError):
        await stopping
    assert reader.done() and stream.closed
    assert not backend._audio_readers


@pytest.mark.asyncio
async def test_overflow_ends_call_once_and_closes_stream(sdk, caplog):
    backend = LiveKitBackend()
    await backend._notify_incoming("room", "caller")
    queue = backend._audio_queue
    for _ in range(queue.maxsize - 1):
        queue.put_nowait(b"accepted")
    ended = []

    async def end(call_id):
        ended.append(call_id)

    backend.on_call_ended = end
    stream = Stream()
    await backend._read_audio_track("room", stream)
    pending = list(backend._ending_calls.values())
    await asyncio.gather(*pending)
    assert ended == ["room"]
    assert queue.failure == "audio_backlog_limit"
    assert stream.closed
    assert backend._audio_queue is None
    assert "Audio backlog limit reached" in caplog.text
    assert not backend._audio_readers


@pytest.mark.asyncio
async def test_same_track_is_subscribed_only_once(sdk):
    backend = LiveKitBackend()
    await backend._notify_incoming("room", "caller")
    stream = Stream(asyncio.Event())
    backend._start_audio_track("room", stream)
    backend._start_audio_track("room", stream)
    await stream.started.wait()
    assert len(backend._audio_readers) == 1
    await backend.stop()
    assert stream.closed
    assert not backend._audio_readers


@pytest.mark.asyncio
async def test_stop_before_reader_starts_clears_registration(sdk):
    backend = LiveKitBackend()
    await backend._notify_incoming("room", "caller")
    stream = Stream(asyncio.Event())
    backend._start_audio_track("room", stream)
    queue = backend._audio_queue
    await backend._close_audio_readers(queue)
    assert not backend._audio_readers
    assert not backend._audio_tracks
    assert queue.closed


@pytest.mark.asyncio
async def test_bytes_are_reclaimed_after_consumer_reads(sdk):
    backend = LiveKitBackend()
    await backend._notify_incoming("room", "caller")
    queue = backend._audio_queue
    frame = b"x" * (2 * 1024 * 1024)
    queue.put_nowait(frame)
    with pytest.raises(asyncio.QueueFull):
        queue.put_nowait(b"x")
    assert queue.get_nowait() is frame
    queue.put_nowait(frame)
    queue.close()
    queue.close()
    assert queue.qsize() == 2
    assert queue.get_nowait() is frame
    assert queue.get_nowait() is None


@pytest.mark.asyncio
async def test_stop_owns_pending_connection_task(sdk):
    backend = LiveKitBackend()
    started, finished = asyncio.Event(), asyncio.Event()

    async def connecting():
        started.set()
        try:
            await asyncio.Event().wait()
        finally:
            finished.set()

    task = backend._spawn(connecting())
    await started.wait()
    await backend.stop()
    assert task.done() and finished.is_set()
    assert not backend._background_tasks


@pytest.mark.asyncio
async def test_call_end_cancels_setup_before_notifying_channel(sdk):
    backend = LiveKitBackend()
    started, cleaned = asyncio.Event(), asyncio.Event()

    async def incoming(*args):
        started.set()
        try:
            await asyncio.Event().wait()
        finally:
            cleaned.set()

    observed = []

    async def ended(call_id):
        observed.append(cleaned.is_set())

    backend.on_incoming_call = incoming
    backend.on_call_ended = ended
    task = backend._spawn(backend._notify_incoming("room", "caller"))
    await started.wait()
    await backend._handle_call_ended("room")
    assert task.done()
    assert observed == [True]


@pytest.mark.asyncio
async def test_stale_call_end_does_not_touch_new_call(sdk):
    backend = LiveKitBackend()
    await backend._notify_incoming("old", "caller")
    old = backend._audio_queue
    await backend._notify_incoming("new", "caller")
    current = backend._audio_queue
    await backend._handle_call_ended("old", old)
    assert backend._audio_queue is current
    assert not current.closed


@pytest.mark.asyncio
async def test_stop_closes_admission_before_background_cleanup():
    backend = LiveKitBackend()
    started, cleanup, release, late_started = [
        asyncio.Event() for _ in range(4)
    ]

    async def old():
        started.set()
        try:
            await asyncio.Event().wait()
        finally:
            cleanup.set()
            await release.wait()

    async def late():
        late_started.set()
        await asyncio.Event().wait()

    backend._spawn(old())
    await started.wait()
    stopping = asyncio.create_task(backend.stop())
    await cleanup.wait()
    backend._spawn(late())
    await asyncio.sleep(0)
    release.set()
    await stopping
    assert not late_started.is_set()
    assert not backend._background_tasks


@pytest.mark.asyncio
@pytest.mark.parametrize("during_stop", [False, True])
async def test_sdk_close_failure_is_observable(sdk, caplog, during_stop):
    backend = LiveKitBackend()
    await backend._notify_incoming("room", "caller")
    queue = backend._audio_queue
    stream = Stream(asyncio.Event() if during_stop else None)

    async def close():
        raise OSError("synthetic close failure")

    stream.aclose = close
    backend._start_audio_track("room", stream)
    await stream.started.wait()
    if during_stop:
        await backend.stop()
    else:
        # Let owned close and the resulting call cleanup settle.
        readers = list(backend._audio_readers)
        await asyncio.gather(*readers)
        await asyncio.gather(*list(backend._ending_calls.values()))
    assert queue.failure == "audio_stream_close_failed"
    assert "Audio stream cleanup failed" in caplog.text
    assert not backend._audio_readers


@pytest.mark.asyncio
async def test_room_callbacks_fence_participant_and_old_room(monkeypatch):
    rooms = []

    class Room:
        def __init__(self):
            self.handlers = {}
            self.remote_participants = {}
            self.local_participant = types.SimpleNamespace(
                publish_track=self.publish
            )
            self.disconnected = False
            rooms.append(self)

        def on(self, name):
            def bind(callback):
                self.handlers[name] = callback
                return callback

            return bind

        async def publish(self, track):
            pass

        async def connect(self, *args):
            pass

        async def disconnect(self):
            self.disconnected = True

    class Token:
        def __init__(self, **kwargs):
            pass

        def with_identity(self, *args):
            return self

        def with_grants(self, *args):
            return self

        def to_jwt(self):
            return "synthetic-token"

    rtc = types.SimpleNamespace(
        Room=Room,
        AudioSource=lambda **kwargs: object(),
        LocalAudioTrack=types.SimpleNamespace(
            create_audio_track=lambda *args: object()
        ),
        ParticipantKind=types.SimpleNamespace(
            PARTICIPANT_KIND_SIP=1,
            PARTICIPANT_KIND_STANDARD=2,
        ),
        TrackKind=types.SimpleNamespace(KIND_AUDIO=1),
        AudioStream=lambda track: track,
    )
    monkeypatch.setitem(
        sys.modules,
        "livekit",
        types.SimpleNamespace(
            rtc=rtc,
            api=types.SimpleNamespace(
                AccessToken=Token, VideoGrants=lambda **kwargs: kwargs
            ),
        ),
    )
    backend = LiveKitBackend()
    calls = []

    async def incoming(call_id, *args):
        calls.append(call_id)

    backend.on_incoming_call = incoming
    await backend._connect_and_wait()
    first_room = rooms[-1]
    participant = types.SimpleNamespace(
        kind=1, identity="caller", attributes={}, track_publications={}
    )
    other = types.SimpleNamespace(
        kind=1, identity="other", attributes={}, track_publications={}
    )
    first_room.handlers["participant_connected"](participant)
    await asyncio.gather(*list(backend._background_tasks))
    first_queue = backend._audio_queue
    first_room.handlers["participant_disconnected"](other)
    await asyncio.sleep(0)
    assert backend._audio_queue is first_queue
    stream = Stream(asyncio.Event())
    stream.kind = 1
    first_room.handlers["track_subscribed"](stream, None, participant)
    first_room.handlers["track_subscribed"](stream, None, participant)
    await stream.started.wait()
    assert len(backend._audio_readers) == 1
    first_room.handlers["participant_disconnected"](participant)
    await asyncio.gather(*list(backend._ending_calls.values()))
    assert stream.closed and first_room.disconnected
    await backend._connect_and_wait()
    second_room = rooms[-1]
    second_room.handlers["participant_connected"](participant)
    await asyncio.gather(*list(backend._background_tasks))
    current = backend._audio_queue
    assert len(calls) == 2 and calls[0] != calls[1]
    first_room.handlers["participant_connected"](other)
    first_room.handlers["participant_disconnected"](participant)
    first_room.handlers["track_subscribed"](Stream(), None, participant)
    await asyncio.sleep(0)
    assert backend._audio_queue is current and not current.closed
    await backend.stop()


@pytest.mark.asyncio
async def test_multitrack_overflow_reclaims_early_rejected_track(monkeypatch):
    class Stream:
        def __aiter__(self):
            return self

        async def __anext__(self):
            return types.SimpleNamespace(
                frame=types.SimpleNamespace(data=b"x")
            )

        async def aclose(self):
            pass

    monkeypatch.setitem(
        sys.modules,
        "livekit",
        types.SimpleNamespace(
            rtc=types.SimpleNamespace(AudioStream=lambda track: track)
        ),
    )
    b = LiveKitBackend()
    await b._notify_incoming("call", "from")
    b._start_audio_track("call", Stream())
    b._start_audio_track("call", Stream())
    await asyncio.sleep(0)
    await b.stop()
    assert (
        not b._audio_tracks
    ), "early rejected track remains retained after stop"


@pytest.mark.asyncio
async def test_disconnect_then_stop_does_not_recancel_setup_cleanup():
    b = LiveKitBackend()
    entered, cleaning, release, finished = [asyncio.Event() for _ in range(4)]

    async def incoming(*args):
        entered.set()
        try:
            await asyncio.Event().wait()
        finally:
            cleaning.set()
            await release.wait()
            finished.set()

    b.on_incoming_call = incoming
    t = b._spawn(b._notify_incoming("call", "from"))
    await entered.wait()
    d = asyncio.create_task(b._disconnect_room())
    await cleaning.wait()
    s = asyncio.create_task(b.stop())
    for _ in range(8):
        await asyncio.sleep(0)
    try:
        assert not t.done(), "second cancel interrupted owned incoming cleanup"
        assert not s.done()
    finally:
        release.set()
        await asyncio.gather(d, s, return_exceptions=True)
    assert finished.is_set()
