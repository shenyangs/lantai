# -*- coding: utf-8 -*-
"""LiveKit backend -- production-grade SIP/RTP (event-driven).

Architecture::

    SIP Phone -> LiveKit SIP Server (Go)
              -> LiveKit Room (WebRTC)
              -> This Backend (STT/TTS bridge)

The backend connects to a pre-configured room on startup and waits
for a SIP participant to join via ``participant_connected`` events.
When the call ends (``participant_disconnected``), the room is
explicitly deleted and the backend reconnects to wait for the next
call.

.. note::

    TODO: 当前采用单房间事件监听模式，底层架构已具备并发能力，
    后续可配合 FastAPI Webhook 实现动态房间的并发路由。

Required config::

    sip_mode: "livekit"
    livekit_url: "ws://your-livekit:7880"
    livekit_api_key: "devkey"
    livekit_api_secret: "secret"
    livekit_room_name: "sip-inbound"

Required packages::

    pip install livekit livekit-api
"""
from __future__ import annotations

import asyncio
import logging
import uuid
import weakref
from typing import Any, Optional

from lantai.utils.async_settlement import await_settled

from .audio_inbox import AudioInbox
from .backend import CallEndedCallback, IncomingCallCallback

logger = logging.getLogger(__name__)


class LiveKitBackend:
    """SipBackend backed by LiveKit SIP Server (event-driven).

    Instead of polling ``list_rooms``, the backend connects to a
    fixed room on startup and reacts to LiveKit room events:

    * ``participant_connected``  → start agent session
    * ``track_subscribed``       → bridge audio to STT
    * ``participant_disconnected`` → cleanup + delete room + reconnect

    TODO: 当前采用单房间事件监听模式，底层架构已具备并发能力，
    后续可配合 FastAPI Webhook 实现动态房间的并发路由。
    """

    def __init__(
        self,
        livekit_url: str = "",
        livekit_api_key: str = "",
        livekit_api_secret: str = "",
        sip_trunk_id: str = "",
        room_name: str = "sip-inbound",
        output_sample_rate: int = 24000,
    ) -> None:
        self._url = livekit_url
        self._api_key = livekit_api_key
        self._api_secret = livekit_api_secret
        self._trunk_id = sip_trunk_id
        self._room_name = room_name
        self._output_sample_rate = output_sample_rate

        self.on_incoming_call: Optional[IncomingCallCallback] = None
        self.on_call_ended: Optional[CallEndedCallback] = None

        self._lk_api: Any = None
        self._room: Any = None
        self._audio_source: Any = None
        self._audio_queue: Optional[AudioInbox] = None
        self._audio_readers: dict[asyncio.Task, AudioInbox] = {}
        self._audio_tracks: dict[tuple[int, int], asyncio.Task] = {}
        self._call_id: Optional[str] = None
        self._participant: Any = None
        self._ending_calls: dict[AudioInbox, asyncio.Task] = {}
        self._disconnect_task: Optional[asyncio.Task] = None
        self._background_tasks: set[asyncio.Task] = set()
        self._stop_task: Optional[asyncio.Task] = None
        self._incoming_task: Optional[asyncio.Task] = None
        self._cancelled_tasks: weakref.WeakSet = weakref.WeakSet()
        self._running = False
        self._stopping = False
        self._busy = False

    # ----------------------------------------------------------
    # Lifecycle
    # ----------------------------------------------------------

    async def start(self) -> None:
        if not self._url:
            raise ValueError("livekit_url is required")
        if not self._api_key or not self._api_secret:
            raise ValueError(
                "livekit_api_key and livekit_api_secret are required",
            )

        from livekit import api as lk_api

        http_url = self._url.replace(
            "ws://",
            "http://",
        ).replace("wss://", "https://")
        self._lk_api = lk_api.LiveKitAPI(
            url=http_url,
            api_key=self._api_key,
            api_secret=self._api_secret,
        )
        self._running = True
        self._stopping = False
        # Non-blocking: retry connection in background so start() doesn't block
        self._spawn(self._connect_with_retry())
        logger.info(
            "LiveKit backend started (event-driven): %s room=%s",
            self._url,
            self._room_name,
        )

    def _spawn(self, operation) -> Optional[asyncio.Task]:
        if self._stopping:
            operation.close()
            return None
        task = asyncio.create_task(operation)
        self._background_tasks.add(task)

        def settled(done: asyncio.Task) -> None:
            self._background_tasks.discard(done)
            if not done.cancelled() and done.exception() is not None:
                logger.error(
                    "SIP background task failed", exc_info=done.exception()
                )

        task.add_done_callback(settled)
        return task

    async def stop(self) -> None:
        self._stopping = True
        self._running = False
        if self._stop_task is None or self._stop_task.done():
            self._stop_task = asyncio.create_task(self._stop_owned())
        await await_settled(self._stop_task)

    def _cancel_once(self, task: asyncio.Task) -> None:
        # Multiple cleanup owners may join the same task. Repeated cancel()
        # would interrupt the callback's asynchronous finally block.
        if task not in self._cancelled_tasks and not task.done():
            self._cancelled_tasks.add(task)
            task.cancel()

    async def _stop_owned(self) -> None:
        tasks = list(self._background_tasks)
        for task in tasks:
            self._cancel_once(task)
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        await self._disconnect_room()
        ending = list(self._ending_calls.values())
        if ending:
            await asyncio.gather(*ending, return_exceptions=True)
        if self._lk_api:
            await self._lk_api.aclose()
            self._lk_api = None
        self._busy = False
        logger.info("LiveKit backend stopped")

    # ----------------------------------------------------------
    # Audio playback
    # ----------------------------------------------------------

    async def play_audio(
        self,
        call_id: str,
        audio: bytes,
    ) -> None:
        if self._audio_source is None:
            if not getattr(self, "_no_source_warned", False):
                logger.warning(
                    "play_audio: no source for %s",
                    call_id,
                )
                self._no_source_warned = True
            return
        from livekit import rtc

        frame = rtc.AudioFrame(
            data=audio,
            sample_rate=self._output_sample_rate,
            num_channels=1,
            samples_per_channel=len(audio) // 2,
        )
        await self._audio_source.capture_frame(frame)

    # ----------------------------------------------------------
    # Room connection (event-driven)
    # ----------------------------------------------------------

    async def _connect_with_retry(self) -> None:
        """Retry _connect_and_wait with exponential backoff."""
        delay = 2
        while self._running:
            try:
                await self._connect_and_wait()
                return  # connected successfully
            except Exception:
                logger.warning(
                    "Connect failed, retrying in %ds...",
                    delay,
                )
                await asyncio.sleep(delay)
                delay = min(delay * 2, 30)

    async def _connect_and_wait(self) -> None:
        """Connect to the pre-configured room and bind events."""
        from livekit import api as lk_api, rtc

        token = (
            lk_api.AccessToken(
                api_key=self._api_key,
                api_secret=self._api_secret,
            )
            .with_identity("lantai-sip-agent")
            .with_grants(
                lk_api.VideoGrants(
                    room_join=True,
                    room=self._room_name,
                ),
            )
            .to_jwt()
        )

        room = rtc.Room()
        self._room = room
        self._busy = False

        audio_source = rtc.AudioSource(
            sample_rate=self._output_sample_rate,
            num_channels=1,
        )
        track = rtc.LocalAudioTrack.create_audio_track(
            "lantai-tts",
            audio_source,
        )
        self._audio_source = audio_source
        self._no_source_warned = False

        # Room names are reused; channel session IDs must not be. Late STT
        # callbacks from a prior call must never resolve the next session.
        call_id = f"{self._room_name}:{uuid.uuid4().hex}"

        # -- Event: SIP participant joins --
        @room.on("participant_connected")
        def on_participant_connected(
            participant: rtc.RemoteParticipant,
        ):
            if self._stopping or self._room is not room:
                return
            self._spawn(
                self._handle_sip_participant(
                    call_id,
                    participant,
                    expected_room=room,
                ),
            )

        # -- Event: audio track ready --
        @room.on("track_subscribed")
        def on_track_subscribed(
            remote_track: rtc.Track,
            _publication: rtc.RemoteTrackPublication,
            _participant: rtc.RemoteParticipant,
        ):
            if (
                not self._stopping
                and self._room is room
                and _participant is self._participant
                and remote_track.kind == rtc.TrackKind.KIND_AUDIO
            ):
                self._start_audio_track(call_id, remote_track)

        # -- Event: SIP participant leaves --
        @room.on("participant_disconnected")
        def on_participant_disconnected(
            participant: rtc.RemoteParticipant,
        ):
            if (
                not self._stopping
                and self._room is room
                and participant is self._participant
            ):
                self._schedule_call_end(call_id, self._audio_queue)

        try:
            await room.connect(self._url, token)
            await room.local_participant.publish_track(track)
            logger.info(
                "Connected to room: %s, waiting for SIP participant...",
                self._room_name,
            )
            # Check participants already in the room
            for p in room.remote_participants.values():
                self._spawn(
                    self._handle_sip_participant(
                        call_id,
                        p,
                        expected_room=room,
                    )
                )
        except BaseException:
            if self._room is room:
                await self._disconnect_room()
            raise

    async def _close_audio_readers(self, queue: AudioInbox) -> None:
        tasks = [
            task
            for task, inbox in self._audio_readers.items()
            if inbox is queue and task is not asyncio.current_task()
        ]
        for task in tasks:
            self._cancel_once(task)
        try:
            if tasks:
                await asyncio.gather(*tasks, return_exceptions=True)
        finally:
            for task in tasks:
                self._audio_readers.pop(task, None)
            for key, task in list(self._audio_tracks.items()):
                if task in tasks:
                    self._audio_tracks.pop(key, None)
            queue.close()

    async def _disconnect_room(self) -> None:
        """Fence ingress first, then settle every owned reader and room."""
        if self._disconnect_task is None or self._disconnect_task.done():
            queue, room = self._audio_queue, self._room
            incoming = self._incoming_task
            self._incoming_task = None
            caller = asyncio.current_task()
            self._audio_queue = None
            self._audio_source = None
            self._room = None
            self._participant = None
            self._call_id = None

            async def disconnect() -> None:
                try:
                    if incoming is not None and incoming is not caller:
                        self._cancel_once(incoming)
                        await asyncio.gather(incoming, return_exceptions=True)
                    if queue is not None:
                        await self._close_audio_readers(queue)
                finally:
                    if room:
                        try:
                            await room.disconnect()
                        except Exception:
                            logger.warning(
                                "Room disconnect failed", exc_info=True
                            )

            self._disconnect_task = asyncio.create_task(disconnect())
        await await_settled(self._disconnect_task)

    async def _delete_and_reconnect(self) -> None:
        """Delete the room and reconnect to wait for next call."""
        await self._disconnect_room()

        # Explicitly delete the room to prevent stale rooms
        if self._lk_api:
            try:
                from livekit import api as lk_api

                await self._lk_api.room.delete_room(
                    lk_api.DeleteRoomRequest(
                        room=self._room_name,
                    ),
                )
                logger.info(
                    "Deleted room: %s",
                    self._room_name,
                )
            except Exception:
                logger.debug(
                    "Failed to delete room %s",
                    self._room_name,
                    exc_info=True,
                )

        if self._running:
            # Small delay before reconnecting
            await asyncio.sleep(1)
            if self._running:
                self._spawn(self._connect_with_retry())

    # ----------------------------------------------------------
    # Participant handling
    # ----------------------------------------------------------

    async def _handle_sip_participant(
        self,
        call_id: str,
        participant: Any,
        *,
        expected_room: Any = None,
    ) -> None:
        """Handle a SIP or WebRTC participant joining the room."""
        from livekit import rtc

        if self._stopping or (
            expected_room is not None and self._room is not expected_room
        ):
            return
        if self._busy:
            logger.warning(
                "Agent busy, ignoring participant: %s",
                participant.identity,
            )
            return
        if participant.kind not in (
            rtc.ParticipantKind.PARTICIPANT_KIND_SIP,
            rtc.ParticipantKind.PARTICIPANT_KIND_STANDARD,
        ):
            return

        self._busy = True
        self._participant = participant
        is_sip = participant.kind == rtc.ParticipantKind.PARTICIPANT_KIND_SIP
        from_uri = (
            participant.attributes.get(
                "sip.phoneNumber",
                participant.identity,
            )
            if is_sip
            else participant.identity
        )
        logger.info(
            "%s participant in %s: %s",
            "SIP" if is_sip else "WebRTC",
            self._room_name,
            from_uri,
        )
        await self._notify_incoming(
            call_id,
            from_uri,
        )

        # Subscribe to existing audio tracks
        for pub in participant.track_publications.values():
            trk = pub.track
            if trk and trk.kind == rtc.TrackKind.KIND_AUDIO:
                self._start_audio_track(call_id, trk)

    def _schedule_call_end(
        self,
        call_id: str,
        queue: Optional[AudioInbox],
    ) -> None:
        if queue is None or queue is not self._audio_queue:
            return
        if queue in self._ending_calls:
            return
        task = asyncio.create_task(self._handle_call_ended(call_id, queue))
        self._ending_calls[queue] = task

        def settled(done: asyncio.Task) -> None:
            self._ending_calls.pop(queue, None)
            if not done.cancelled() and done.exception() is not None:
                logger.error(
                    "SIP call cleanup failed", exc_info=done.exception()
                )

        task.add_done_callback(settled)

    async def _handle_call_ended(
        self,
        call_id: str,
        queue: Optional[AudioInbox] = None,
    ) -> None:
        """A stale participant or overflow must not end a newer call."""
        queue = queue if queue is not None else self._audio_queue
        if queue is None or queue is not self._audio_queue:
            return
        await self._disconnect_room()
        try:
            if self.on_call_ended:
                await self.on_call_ended(call_id)
        finally:
            self._busy = False
            logger.info("SIP call ended in room %s", self._room_name)
            await self._delete_and_reconnect()

    # ----------------------------------------------------------
    # Audio bridge
    # ----------------------------------------------------------

    def _start_audio_track(self, call_id: str, track: Any) -> None:
        queue = self._audio_queue
        if (
            self._stopping
            or queue is None
            or queue.closed
            or call_id != self._call_id
        ):
            return
        key = (id(queue), id(track))
        if key in self._audio_tracks:
            return
        task = asyncio.create_task(
            self._read_audio_track(call_id, track, queue),
        )
        # Register before scheduling: another track can reach EOF first.
        self._audio_tracks[key] = task
        self._audio_readers[task] = queue

    async def _read_audio_track(
        self,
        call_id: str,
        track: Any,
        queue: Optional[AudioInbox] = None,
    ) -> None:
        """Own the SDK stream; never move a slow consumer's backlog upstream."""
        from livekit import rtc

        queue = queue if queue is not None else self._audio_queue
        task = asyncio.current_task()
        key = (id(queue), id(track))
        if (
            queue is None
            or queue is not self._audio_queue
            or queue.closed
            or self._audio_tracks.get(key, task) is not task
        ):
            self._audio_readers.pop(task, None)
            if self._audio_tracks.get(key) is task:
                self._audio_tracks.pop(key, None)
            return
        self._audio_tracks[key] = task
        self._audio_readers[task] = queue
        stream = None
        count = 0
        try:
            stream = rtc.AudioStream(track)
            async for event in stream:
                if queue is not self._audio_queue or queue.closed:
                    break
                frame = event.frame
                count += 1
                if count == 1:
                    logger.info(
                        "LiveKit frame #1 %s: rate=%s ch=%s len=%d",
                        call_id,
                        getattr(frame, "sample_rate", "?"),
                        getattr(frame, "num_channels", "?"),
                        len(frame.data),
                    )
                queue.put_nowait(bytes(frame.data))
        except asyncio.QueueFull:
            queue.close("audio_backlog_limit")
            logger.error(
                "Audio backlog limit reached; ending call %s", call_id
            )
        except asyncio.CancelledError:
            pass
        except Exception:
            queue.close("audio_stream_failed")
            logger.warning("Audio stream failed: %s", call_id, exc_info=True)
        finally:
            try:
                if stream is not None:
                    await await_settled(asyncio.create_task(stream.aclose()))
            except Exception:
                queue.close("audio_stream_close_failed")
                logger.warning(
                    "Audio stream cleanup failed: %s",
                    call_id,
                    exc_info=True,
                )
            finally:
                self._audio_readers.pop(task, None)
                self._audio_tracks.pop(key, None)
                if not any(q is queue for q in self._audio_readers.values()):
                    queue.close()
                if queue.failure:
                    self._schedule_call_end(call_id, queue)

    async def _notify_incoming(
        self,
        call_id: str,
        from_uri: str,
    ) -> None:
        """Notify SIPChannel of a new incoming call."""
        if self._stopping:
            return
        if self._audio_queue is not None:
            old_queue = self._audio_queue
            self._audio_queue = None
            await await_settled(
                asyncio.create_task(
                    self._close_audio_readers(old_queue),
                )
            )
        audio_queue = AudioInbox()
        self._audio_queue = audio_queue
        self._call_id = call_id

        async def write_audio(data: bytes) -> None:
            if self._audio_queue is audio_queue and not audio_queue.closed:
                await self.play_audio(call_id, data)

        self._incoming_task = asyncio.current_task()
        try:
            if self.on_incoming_call:
                await self.on_incoming_call(
                    call_id,
                    from_uri,
                    self._room_name,
                    audio_queue,
                    write_audio,
                )
        except Exception:
            audio_queue.close("call_setup_failed")
            logger.warning("SIP call setup failed: %s", call_id, exc_info=True)
            self._schedule_call_end(call_id, audio_queue)
        finally:
            if self._incoming_task is asyncio.current_task():
                self._incoming_task = None
