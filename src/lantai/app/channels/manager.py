# -*- coding: utf-8 -*-
# pylint: disable=protected-access
# ChannelManager is the framework owner of BaseChannel and must call
# _is_native_payload and _consume_one_request as part of the contract.

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from pathlib import Path

from typing import (
    Any,
    Callable,
    Dict,
    List,
    Optional,
    TYPE_CHECKING,
)

from .base import BaseChannel, ContentType, ProcessHandler, TextContent
from .command_registry import CommandRegistry
from .delivery import ChannelDeliveryLedger, DeliveryRecord, DeliveryRegistration
from .registry import get_channel_registry
from .renderer import ChannelDisplayConfig
from .unified_queue_manager import UnifiedQueueManager
from ...config import get_available_channels
from ...utils.async_settlement import await_settled

if TYPE_CHECKING:
    from ...config.config import Config

logger = logging.getLogger(__name__)

# Callback when user reply was sent: (channel, user_id, session_id)
OnLastDispatch = Optional[Callable[[str, str, str], None]]

# Default max size per channel queue
_CHANNEL_QUEUE_MAXSIZE = 1000
_CHANNEL_MAX_QUEUES = 256
_CHANNEL_MAX_PENDING_ENQUEUE_TASKS = 128
_CHANNEL_START_TIMEOUT_SECONDS = 3.0
_CHANNEL_REPLACE_TIMEOUT_SECONDS = 30.0
_CHANNEL_STOP_TIMEOUT_SECONDS = 5.0
_CHANNEL_FAILURE_NOTICE_TIMEOUT_SECONDS = 5.0


@dataclass(frozen=True)
class _QueuedDelivery:
    payload: Any
    delivery_id: str


async def _process_batch(ch: BaseChannel, batch: List[Any]) -> None:
    """Merge if needed and process one payload (native or request)."""
    if ch.channel == "dingtalk" and batch and ch._is_native_payload(batch[0]):
        first = batch[0] if isinstance(batch[0], dict) else {}
        logger.info(
            "manager _process_batch dingtalk: batch_len=%s first_has_sw=%s",
            len(batch),
            bool(first.get("session_webhook")),
        )
    if len(batch) > 1 and ch._is_native_payload(batch[0]):
        merged = ch.merge_native_items(batch)
        if ch.channel == "dingtalk" and isinstance(merged, dict):
            logger.info(
                "manager _process_batch dingtalk merged: has_sw=%s",
                bool(merged.get("session_webhook")),
            )
        await ch._consume_one_request(merged)
    elif len(batch) > 1:
        merged = ch.merge_requests(batch)
        if merged is not None:
            await ch._consume_one_request(merged)
        else:
            await ch.consume_one(batch[0])
    elif ch._is_native_payload(batch[0]):
        await ch._consume_one_request(batch[0])
    else:
        await ch.consume_one(batch[0])


class ChannelManager:
    """Owns queues and consumer loops; channels define how to consume via
    consume_one(). Enqueue via enqueue(channel_id, payload) (thread-safe).
    """

    def __init__(
        self,
        channels: List[BaseChannel],
        *,
        delivery_ledger_path: Path | None = None,
    ):
        self.channels = channels
        self._lock = asyncio.Lock()
        self._loop: Optional[asyncio.AbstractEventLoop] = None

        # New unified queue system
        self._command_registry = CommandRegistry()
        self._queue_manager: UnifiedQueueManager | None = None
        self._workspace = None

        # Per-channel locks to prevent concurrent restarts
        self._restart_locks: dict[str, asyncio.Lock] = {}

        # Track enqueue tasks for graceful shutdown
        self._enqueue_tasks: set[asyncio.Task] = set()
        self._enqueue_cancellation_requested: set[asyncio.Task] = set()
        self._rejected_enqueue_count = 0

        # Track slow channel start tasks so optional external channels cannot
        # block console chat startup indefinitely.
        self._startup_tasks: set[asyncio.Task] = set()
        self._startup_cancellation_requested: set[asyncio.Task] = set()
        self._channel_start_tasks: dict[int, asyncio.Task] = {}
        self._channel_stop_tasks: dict[int, asyncio.Task] = {}
        self._delivery_ledger = ChannelDeliveryLedger(delivery_ledger_path)

    @classmethod
    def from_env(
        cls,
        process: ProcessHandler,
        on_last_dispatch: OnLastDispatch = None,
    ) -> "ChannelManager":
        """
        Create channels from env and inject unified process
        (AgentRequest -> Event stream).
        process is typically runner.stream_query, handled by the Lantai
        process endpoint.
        on_last_dispatch: called when a user send+reply was sent.
        """
        available = get_available_channels()
        registry = get_channel_registry()
        channels: list[BaseChannel] = [
            ch_cls.from_env(process, on_reply_sent=on_last_dispatch)
            for key, ch_cls in registry.items()
            if key in available
        ]
        return cls(channels)

    @classmethod
    # pylint: disable=too-many-branches
    def from_config(
        cls,
        process: ProcessHandler,
        config: "Config",
        on_last_dispatch: OnLastDispatch = None,
        workspace_dir: Path | None = None,
    ) -> "ChannelManager":
        """Create channels from config (config.json or agent.json).

        Args:
            process: Process handler for agent communication
            config: Configuration object with channels
            on_last_dispatch: Callback for dispatch events
            workspace_dir: Agent workspace directory for channel state files
        """
        available = get_available_channels()
        ch = config.channels
        show_tool_details = getattr(config, "show_tool_details", True)
        extra = getattr(ch, "__pydantic_extra__", None) or {}

        channels: list[BaseChannel] = []
        for key, ch_cls in get_channel_registry().items():
            if key not in available:
                continue
            ch_cfg = getattr(ch, key, None)
            if ch_cfg is None and key in extra:
                ch_cfg = extra[key]
            if ch_cfg is None:
                continue
            if isinstance(ch_cfg, dict):
                from types import SimpleNamespace
                from ...config.config import BaseChannelConfig

                defaults = BaseChannelConfig().model_dump()
                defaults.update(ch_cfg)
                ch_cfg = SimpleNamespace(**defaults)

            # Check if channel is enabled
            # Handle both Pydantic objects (built-in)
            # and dicts (customchannels)
            if isinstance(ch_cfg, dict):
                enabled = ch_cfg.get("enabled", False)
            else:
                enabled = getattr(ch_cfg, "enabled", False)
            if not enabled:
                continue

            # Handle both Pydantic objects (built-in)
            # and dicts (custom channels)
            if isinstance(ch_cfg, dict):
                filter_tool_messages = ch_cfg.get(
                    "filter_tool_messages",
                    False,
                )
                filter_thinking = ch_cfg.get("filter_thinking", False)
            else:
                filter_tool_messages = getattr(
                    ch_cfg,
                    "filter_tool_messages",
                    False,
                )
                filter_thinking = getattr(
                    ch_cfg,
                    "filter_thinking",
                    False,
                )

            from_config_kwargs = {
                "process": process,
                "config": ch_cfg,
                "on_reply_sent": on_last_dispatch,
                "show_tool_details": show_tool_details,
                "filter_tool_messages": filter_tool_messages,
                "filter_thinking": filter_thinking,
                "display_config": ChannelDisplayConfig.from_config(
                    ch_cfg,
                    show_tool_details=show_tool_details,
                ),
                "no_text_debounce": getattr(
                    ch_cfg,
                    "no_text_debounce",
                    True,
                ),
                "workspace_dir": workspace_dir,
            }

            # Only pass kwargs that the channel's from_config accepts
            import inspect

            sig = inspect.signature(ch_cls.from_config)
            if any(
                p.kind == inspect.Parameter.VAR_KEYWORD
                for p in sig.parameters.values()
            ):
                filtered_kwargs = from_config_kwargs
            else:
                filtered_kwargs = {
                    k: v
                    for k, v in from_config_kwargs.items()
                    if k in sig.parameters
                }

            try:
                channel_instance = ch_cls.from_config(**filtered_kwargs)
                channel_instance.configure_runtime(
                    display_config=from_config_kwargs["display_config"],
                    no_text_debounce=from_config_kwargs["no_text_debounce"],
                    streaming_enabled=(
                        bool(getattr(ch_cfg, "streaming_enabled", False))
                        or bool(channel_instance.streaming_enabled)
                    ),
                )
                channels.append(channel_instance)
            except Exception as e:
                logger.warning(
                    "Failed to initialize channel '%s', skipping: %s",
                    key,
                    e,
                )
                continue

        ledger_path = None
        if workspace_dir is not None:
            ledger_path = (
                Path(workspace_dir)
                / ".runtime"
                / "channel-deliveries.sqlite3"
            )
        return cls(channels, delivery_ledger_path=ledger_path)

    def _make_enqueue_cb(self, channel_id: str) -> Callable[[Any], None]:
        """Return a callback that enqueues payload for the given channel."""

        def cb(payload: Any) -> None:
            self.enqueue(channel_id, payload)

        return cb

    def _extract_session_id(
        self,
        ch: BaseChannel,
        payload: Any,
    ) -> str:
        """Extract normalized session_id from payload.

        Args:
            ch: Channel instance
            payload: Native dict or AgentRequest

        Returns:
            Normalized session_id (e.g. "console:user1")

        Note:
            Uses same logic as ch.get_debounce_key for consistency
        """
        # Check if payload already has normalized session_id
        # (e.g. from batch merge or previous processing)
        if isinstance(payload, dict):
            existing_sid = payload.get("session_id")
            if existing_sid:
                return existing_sid

        if hasattr(payload, "session_id"):
            existing_sid = payload.session_id
            if existing_sid:
                return existing_sid

        # Use channel's debounce key (delegates to resolve_session_id)
        return ch.get_debounce_key(payload)

    def _enqueue_one(self, channel_id: str, payload: Any) -> None:
        """Run on event loop: classify priority and route to queue manager.

        Note:
            This is the new routing layer using UnifiedQueueManager
        """
        if self._queue_manager is None:
            logger.warning(
                "enqueue: queue_manager not initialized for channel=%s",
                channel_id,
            )
            return

        # Get channel instance
        ch = next(
            (c for c in self.channels if c.channel == channel_id),
            None,
        )
        if not ch:
            logger.warning(
                "enqueue: channel not found: channel_id=%s",
                channel_id,
            )
            return

        # Extract query text for priority classification
        query = ch._extract_query_from_payload(payload)

        # Get priority level
        priority_level = self._command_registry.get_priority_level(query)

        # Extract normalized session_id
        session_id = self._extract_session_id(ch, payload)

        # Register before scheduling so a duplicate arriving in this same
        # event-loop turn cannot reject a task already admitted but not started.
        overloaded = len(self._enqueue_tasks) >= _CHANNEL_MAX_PENDING_ENQUEUE_TASKS
        registration = self._delivery_ledger.register(
            channel_id,
            session_id,
            priority_level,
            payload,
            rejection_reason=(
                "enqueue pending task limit reached" if overloaded else None
            ),
        )
        if not registration.accepted:
            return
        # Keep the existing task bound; rejected work is visible in the
        # durable dead-letter path without creating another asynchronous task.
        if overloaded:
            self._rejected_enqueue_count += 1
            logger.error(
                "enqueue rejected: pending task limit reached "
                "channel=%s session=%s pending=%s rejected_total=%s",
                channel_id,
                session_id[:30],
                len(self._enqueue_tasks),
                self._rejected_enqueue_count,
            )
            return

        # Route to unified queue manager with task tracking
        task = asyncio.create_task(
            self._enqueue_with_timeout(
                channel_id,
                session_id,
                priority_level,
                payload,
                query,
                registration=registration,
            ),
        )
        self._enqueue_tasks.add(task)
        task.add_done_callback(self._enqueue_tasks.discard)

    async def _enqueue_with_timeout(
        self,
        channel_id: str,
        session_id: str,
        priority_level: int,
        payload: Any,
        query: str,
        *,
        registration: DeliveryRegistration | None = None,
    ) -> None:
        """Enqueue with timeout protection to prevent unbounded blocking.

        Args:
            channel_id: Channel identifier
            session_id: Normalized session ID
            priority_level: Priority level
            payload: Message payload
            query: Extracted query text for logging
        """
        if registration is None:
            registration = self._delivery_ledger.register(
                channel_id,
                session_id,
                priority_level,
                payload,
            )
        if not registration.accepted:
            logger.info(
                "duplicate channel delivery ignored: channel=%s "
                "session=%s delivery=%s status=%s",
                channel_id,
                session_id[:30],
                registration.delivery_id,
                registration.status,
            )
            return

        queued = _QueuedDelivery(payload, registration.delivery_id)
        try:
            await asyncio.wait_for(
                self._queue_manager.enqueue(
                    channel_id,
                    session_id,
                    priority_level,
                    queued,
                ),
                timeout=30.0,
            )
            logger.debug(
                f"Enqueued: channel={channel_id} "
                f"session={session_id[:30]} "
                f"priority={priority_level} "
                f"query={query[:40] if query else '(empty)'}",
            )
        except asyncio.TimeoutError:
            self._delivery_ledger.record_failure(
                registration.delivery_id,
                "enqueue timeout",
            )
            await self._notify_enqueue_failure(
                channel_id, payload, registration.delivery_id,
            )
        except asyncio.CancelledError:
            logger.debug(
                f"Enqueue cancelled: channel={channel_id} "
                f"session={session_id[:30]}",
            )
            raise
        except Exception as e:
            self._delivery_ledger.record_failure(
                registration.delivery_id,
                str(e),
            )
            logger.exception(
                f"Enqueue failed: channel={channel_id} "
                f"session={session_id[:30]} error={e}",
            )
            await self._notify_enqueue_failure(
                channel_id, payload, registration.delivery_id,
            )

    async def _notify_enqueue_failure(
        self,
        channel_id: str,
        payload: Any,
        delivery_id: str,
    ) -> None:
        """Send an operational receipt without repeating an uncertain effect."""
        try:
            ch = await self.get_channel(channel_id)
            if ch is None:
                return
            request = (
                ch._payload_to_request(payload)
                if ch._is_native_payload(payload) else payload
            )
            target = ch.get_to_handle_from_request(request)
            if not target or not self._delivery_ledger.claim_notice(delivery_id):
                return
            notice_task = asyncio.ensure_future(
                ch.send_content_parts(
                    target,
                    [TextContent(
                        type=ContentType.TEXT,
                        text="消息暂时未能进入处理队列。"
                        "已保留待处理记录，请稍后查看处理状态。",
                    )],
                    getattr(request, "channel_meta", None) or {},
                ),
            )
            try:
                done, _ = await asyncio.wait(
                    {notice_task}, timeout=_CHANNEL_FAILURE_NOTICE_TIMEOUT_SECONDS,
                )
                if not done:
                    raise asyncio.TimeoutError
                notice_task.result()
            finally:
                # Keep the enqueue owner alive until adapter cleanup settles,
                # even if shutdown cancels it while timeout cleanup is pending.
                if not notice_task.done():
                    notice_task.cancel()

                async def settle_notice() -> None:
                    await asyncio.gather(notice_task, return_exceptions=True)

                await await_settled(asyncio.create_task(settle_notice()))
            # Adapter return values do not prove remote delivery. Keep the
            # durable state unconfirmed even after the callback returns.
        except asyncio.CancelledError:
            # The durable unconfirmed state survives shutdown; no blind retry.
            raise
        except Exception as exc:
            # Preserve the original delivery failure. Receipt errors may mean
            # the channel accepted the send but its acknowledgement was lost.
            logger.warning(
                "Queue failure notice unconfirmed: channel=%s delivery=%s error=%s",
                channel_id, delivery_id, type(exc).__name__,
            )

    def enqueue(self, channel_id: str, payload: Any) -> None:
        """Enqueue a payload for the channel. Thread-safe (e.g. from sync
        WebSocket or polling thread). Call after start_all().
        """
        if self._loop is None:
            logger.warning("enqueue: loop not set for channel=%s", channel_id)
            return
        self._loop.call_soon_threadsafe(
            self._enqueue_one,
            channel_id,
            payload,
        )

    async def _consume_queue(
        self,
        queue: asyncio.Queue,
        channel_id: str,
        session_id: str,
        priority_level: int,
    ) -> None:
        """Consumer function for UnifiedQueueManager.

        This implements the per-queue consumer loop with batch merging.

        Args:
            queue: The queue to consume from
            channel_id: Channel identifier
            session_id: Normalized session ID
            priority_level: Priority level

        Note:
            Preserves original batch merging logic (drain + merge)
        """
        logger.info(
            f"Consumer started: channel={channel_id} "
            f"session={session_id[:30]} "
            f"priority={priority_level}",
        )

        while True:
            try:
                # Get first payload
                queued_item = await queue.get()

                # Re-fetch channel each iteration so replace_channel()
                # swaps are picked up automatically.
                ch = await self.get_channel(channel_id)
                if not ch:
                    # Channel may be temporarily absent during a
                    # replace_channel() swap.  Retry a few times before
                    # giving up so we don't silently drop the payload.
                    for _retry in range(3):
                        await asyncio.sleep(0.5)
                        ch = await self.get_channel(channel_id)
                        if ch:
                            break
                    if not ch:
                        logger.error(
                            "Consumer: channel not found after"
                            " retries: channel_id=%s",
                            channel_id,
                        )
                        return

                # Drain queue for same-key payloads (batch merge logic)
                # Note: In new architecture, same-key means same QueueKey,
                # so all payloads in this queue already have same
                # (channel_id, session_id, priority_level).
                # We still drain to merge rapid-fire messages (e.g. images)
                batch = [queued_item]
                while True:
                    try:
                        batch.append(queue.get_nowait())
                    except asyncio.QueueEmpty:
                        break

                payloads = [
                    item.payload if isinstance(item, _QueuedDelivery) else item
                    for item in batch
                ]
                deliveries = [
                    item
                    for item in batch
                    if isinstance(item, _QueuedDelivery)
                ]

                try:
                    await _process_batch(ch, payloads)
                except Exception as exc:
                    for item in deliveries:
                        status = self._delivery_ledger.record_failure(
                            item.delivery_id,
                            str(exc),
                        )
                        if status == "pending":
                            record = self._delivery_record(item.delivery_id)
                            delay = min(2 ** max(0, record.attempts - 1), 30)
                            await asyncio.sleep(delay)
                            await queue.put(item)
                        else:
                            logger.error(
                                "channel delivery moved to dead letter: "
                                "channel=%s delivery=%s",
                                channel_id,
                                item.delivery_id,
                            )
                    continue
                for item in deliveries:
                    self._delivery_ledger.mark_delivered(item.delivery_id)

                # Update processed count
                if self._queue_manager is not None:
                    await self._queue_manager.increment_processed(
                        channel_id,
                        session_id,
                        priority_level,
                        count=len(batch),
                    )

                logger.debug(
                    f"Processed batch: channel={channel_id} "
                    f"session={session_id[:30]} "
                    f"priority={priority_level} "
                    f"batch_size={len(batch)}",
                )

            except asyncio.CancelledError:
                logger.debug(
                    f"Consumer cancelled: channel={channel_id} "
                    f"session={session_id[:30]} "
                    f"priority={priority_level}",
                )
                break
            except Exception:
                logger.exception(
                    f"Consumer failed: channel={channel_id} "
                    f"session={session_id[:30]} "
                    f"priority={priority_level}",
                )

    async def start_all(self) -> None:
        """Start all channels and queue manager."""
        self._loop = asyncio.get_running_loop()
        self._delivery_ledger.prune_delivered()

        # Initialize UnifiedQueueManager with consumer function
        self._queue_manager = UnifiedQueueManager(
            consumer_fn=self._consume_queue,
            queue_maxsize=_CHANNEL_QUEUE_MAXSIZE,
            max_queues=_CHANNEL_MAX_QUEUES,
        )

        # Start cleanup loop
        self._queue_manager.start_cleanup_loop()

        # Set enqueue callback for each channel
        async with self._lock:
            snapshot = list(self.channels)

        for ch in snapshot:
            if getattr(ch, "uses_manager_queue", True):
                ch.set_enqueue(self._make_enqueue_cb(ch.channel))

        logger.debug(
            f"Starting channels: {[g.channel for g in snapshot]}",
        )

        # Start each channel.  External channel startup may involve QR login,
        # websocket handshakes, or provider network calls; those must not block
        # the console's first chat request.  Give every channel a short window
        # to become ready, then let slow starts finish in the background.
        for g in snapshot:
            await self._start_channel_without_blocking(g)

        await self._restore_pending_deliveries()

    def _delivery_record(self, delivery_id: str) -> DeliveryRecord:
        records = (
            self._delivery_ledger.list_pending()
            + self._delivery_ledger.list_dead_letters()
        )
        for record in records:
            if record.delivery_id == delivery_id:
                return record
        raise KeyError(delivery_id)

    @staticmethod
    def _restore_payload(payload: Any) -> Any:
        if isinstance(payload, dict) and {
            "session_id",
            "user_id",
            "channel",
            "input",
        }.issubset(payload):
            from .runtime_schema import AgentRequest

            return AgentRequest.model_validate(payload)
        return payload

    async def _restore_pending_deliveries(self) -> None:
        if self._queue_manager is None:
            return
        running = {channel.channel for channel in self.channels}
        restored = 0
        for record in self._delivery_ledger.list_pending():
            if record.channel_id not in running or record.payload is None:
                continue
            await self._queue_manager.enqueue(
                record.channel_id,
                record.session_id,
                record.priority_level,
                _QueuedDelivery(
                    self._restore_payload(record.payload),
                    record.delivery_id,
                ),
            )
            restored += 1
        if restored:
            logger.info("restored %s pending channel deliveries", restored)

    def list_dead_letters(self) -> list[Dict[str, Any]]:
        return [
            {
                "deliveryId": record.delivery_id,
                "channel": record.channel_id,
                "sessionId": record.session_id,
                "attempts": record.attempts,
                "lastError": record.last_error,
                "createdAt": record.created_at,
                "updatedAt": record.updated_at,
            }
            for record in self._delivery_ledger.list_dead_letters()
        ]

    async def replay_dead_letter(self, delivery_id: str) -> Dict[str, Any]:
        if self._queue_manager is None:
            raise RuntimeError("Channel manager is not running")
        record = self._delivery_ledger.requeue(delivery_id)
        if record.payload is None:
            raise RuntimeError("Delivery payload cannot be restored")
        if await self.get_channel(record.channel_id) is None:
            raise RuntimeError(
                f"Channel '{record.channel_id}' is not running",
            )
        await self._queue_manager.enqueue(
            record.channel_id,
            record.session_id,
            record.priority_level,
            _QueuedDelivery(
                self._restore_payload(record.payload),
                record.delivery_id,
            ),
        )
        return {
            "deliveryId": record.delivery_id,
            "status": "queued",
            "channel": record.channel_id,
        }

    async def _start_channel_without_blocking(self, channel: BaseChannel) -> None:
        """Start a channel, continuing in background if it is slow."""
        task = asyncio.create_task(
            channel.start(),
            name=f"channel-start:{channel.channel}",
        )
        self._startup_tasks.add(task)
        self._channel_start_tasks[id(channel)] = task
        task.add_done_callback(
            lambda done_task, ch=channel: self._on_channel_start_done(ch, done_task),
        )
        try:
            await asyncio.wait_for(
                asyncio.shield(task),
                timeout=_CHANNEL_START_TIMEOUT_SECONDS,
            )
        except asyncio.TimeoutError:
            logger.warning(
                "channel start still pending after %.1fs; "
                "continuing in background: channel=%s",
                _CHANNEL_START_TIMEOUT_SECONDS,
                channel.channel,
            )
        except asyncio.CancelledError:
            self._cancel_channel_start(task)
            await asyncio.wait({task}, timeout=_CHANNEL_STOP_TIMEOUT_SECONDS)
            raise
        except Exception:
            logger.exception("failed to start channel=%s", channel.channel)

    def _cancel_channel_start(self, task: asyncio.Task) -> None:
        if not task.done() and task not in self._startup_cancellation_requested:
            self._startup_cancellation_requested.add(task)
            task.cancel()

    def _on_channel_start_done(
        self,
        channel: BaseChannel,
        task: asyncio.Task,
    ) -> None:
        self._startup_tasks.discard(task)
        self._startup_cancellation_requested.discard(task)
        if self._channel_start_tasks.get(id(channel)) is task:
            self._channel_start_tasks.pop(id(channel), None)
        if task.cancelled():
            logger.debug(
                "background channel start cancelled: channel=%s",
                channel.channel,
            )
            return
        try:
            task.result()
        except Exception:
            logger.exception(
                "background channel start failed: channel=%s",
                channel.channel,
            )
        else:
            logger.info(
                "background channel start completed: channel=%s",
                channel.channel,
            )

    async def _stop_channel(self, channel: BaseChannel) -> None:
        """Keep slow transport teardown owned without blocking channel lookup."""
        key = id(channel)
        task = self._channel_stop_tasks.get(key)
        if task is None:
            startup = self._channel_start_tasks.get(key)
            if startup is not None:
                self._cancel_channel_start(startup)

            async def stop_after_start():
                # A late startup may still create transport resources. Keep
                # teardown owned and ordered after its cancellation cleanup.
                if startup is not None:
                    await asyncio.wait({startup})
                await channel.stop()

            task = asyncio.create_task(stop_after_start(), name=f"channel-stop:{channel.channel}")
            self._channel_stop_tasks[key] = task

            def completed(done):
                if self._channel_stop_tasks.get(key) is done:
                    self._channel_stop_tasks.pop(key, None)
                if not done.cancelled() and done.exception() is not None:
                    logger.warning("Failed to stop channel=%s: %s", channel.channel, done.exception())

            task.add_done_callback(completed)
        try:
            await asyncio.wait_for(asyncio.shield(task), timeout=_CHANNEL_STOP_TIMEOUT_SECONDS)
        except asyncio.TimeoutError:
            logger.warning("Channel cleanup still pending: channel=%s", channel.channel)
        except Exception:
            # The completion callback records the error and consumes it.
            pass

    async def stop_all(self) -> None:
        """Stop all channels and queue manager."""
        if self._startup_tasks:
            logger.info(
                "Cancelling %s pending channel startup task(s)",
                len(self._startup_tasks),
            )
            for task in list(self._startup_tasks):
                self._cancel_channel_start(task)
            # Each channel's owned stop task waits for its startup to settle.
            # Other channels can stop immediately; callbacks retain ownership.

        # Retain ownership until cancellation cleanup actually finishes. A later
        # stop must not interrupt cleanup by injecting another cancellation.
        enqueue_tasks = set(self._enqueue_tasks)
        if enqueue_tasks:
            logger.info("Cancelling %s pending enqueue tasks", len(enqueue_tasks))
            for task in enqueue_tasks:
                if task not in self._enqueue_cancellation_requested:
                    self._enqueue_cancellation_requested.add(task)
                    task.add_done_callback(self._enqueue_cancellation_requested.discard)
                    task.cancel()
            done, pending = await asyncio.wait(
                enqueue_tasks,
                timeout=2.0,
                return_when=asyncio.ALL_COMPLETED,
            )
            self._enqueue_tasks.difference_update(done)
            if pending:
                logger.warning(
                    "stop_all: %s enqueue task(s) still pending after 2s",
                    len(pending),
                )

        # Stop queue manager (stops all consumers and cleanup task)
        if self._queue_manager is not None:
            await self._queue_manager.stop_all()
            self._queue_manager = None

        # Stop channels
        async with self._lock:
            snapshot = list(self.channels)

        for ch in snapshot:
            ch.set_enqueue(None)

        await asyncio.gather(*[self._stop_channel(ch) for ch in reversed(snapshot)])

        pending_stops = list(self._channel_stop_tasks.values())
        if pending_stops:
            await asyncio.wait(pending_stops, timeout=_CHANNEL_STOP_TIMEOUT_SECONDS)
        if self._channel_stop_tasks or self._startup_tasks or self._enqueue_tasks:
            logger.warning(
                "ChannelManager cleanup remains pending: stops=%s starts=%s enqueues=%s",
                len(self._channel_stop_tasks),
                len(self._startup_tasks),
                len(self._enqueue_tasks),
            )
        else:
            logger.info("ChannelManager stopped")

    async def get_channel(self, channel: str) -> Optional[BaseChannel]:
        async with self._lock:
            for ch in self.channels:
                if ch.channel == channel:
                    return ch
            return None

    async def get_channel_health(
        self,
        channel_name: str,
    ) -> Dict[str, Any]:
        """Get health status for a specific channel.

        Args:
            channel_name: Channel identifier (e.g. "dingtalk", "telegram")

        Returns:
            Health status dict from the channel's health_check() method.

        Raises:
            KeyError: If channel is not found in this manager.
        """
        channel_instance = await self.get_channel(channel_name)
        if channel_instance is None:
            raise KeyError(f"Channel not found: {channel_name}")
        try:
            return await channel_instance.health_check()
        except Exception as exc:
            logger.exception(
                "health_check failed for channel=%s",
                channel_name,
            )
            return {
                "channel": channel_name,
                "status": "unhealthy",
                "detail": str(exc),
            }

    async def restart_channel(self, channel_name: str) -> Dict[str, Any]:
        """Restart a single channel by stopping and re-starting it.

        The channel is stopped, then a fresh instance is created via
        clone() with the current config, and started via replace_channel().

        Args:
            channel_name: Channel identifier (e.g. "dingtalk", "telegram")

        Returns:
            Dict with restart result: channel, status, detail.

        Raises:
            KeyError: If channel is not found in this manager.
        """
        # Per-channel lock prevents concurrent restarts from
        # leaking resources (two clones started, one discarded).
        lock = self._restart_locks.setdefault(
            channel_name,
            asyncio.Lock(),
        )
        async with lock:
            channel_instance = await self.get_channel(channel_name)
            if channel_instance is None:
                raise KeyError(
                    f"Channel not found: {channel_name}",
                )

            logger.info("Restarting channel: %s", channel_name)

            # Load the latest config for this channel
            from ...config.config import load_agent_config

            agent_id = self._workspace.agent_id if self._workspace else None
            if agent_id is None:
                raise RuntimeError(
                    "Cannot restart channel: workspace not set"
                    " on ChannelManager",
                )

            agent_config = load_agent_config(agent_id)
            channels_cfg = agent_config.channels
            if channels_cfg is None:
                raise RuntimeError(
                    f"No channels config found for agent" f" {agent_id}",
                )

            # Get channel-specific config
            channel_cfg = getattr(
                channels_cfg,
                channel_name,
                None,
            )
            if channel_cfg is None:
                extra = (
                    getattr(
                        channels_cfg,
                        "__pydantic_extra__",
                        None,
                    )
                    or {}
                )
                channel_cfg = extra.get(channel_name)
            if channel_cfg is None:
                raise RuntimeError(
                    f"No config found for channel" f" '{channel_name}'",
                )

            # Clone a fresh instance and replace
            new_channel = channel_instance.clone(channel_cfg)
            if self._workspace is not None:
                new_channel.set_workspace(
                    self._workspace,
                    self._command_registry,
                )
            await self.replace_channel(new_channel)

            logger.info(
                "Channel restarted successfully: %s",
                channel_name,
            )
            return {
                "channel": channel_name,
                "status": "restarted",
                "detail": (f"Channel '{channel_name}'" " has been restarted."),
            }

    def set_workspace(self, workspace) -> None:
        """Set workspace and inject to all channels.

        Args:
            workspace: Workspace instance with task_tracker and chat_manager
        """
        self._workspace = workspace
        for ch in self.channels:
            ch.set_workspace(workspace, self._command_registry)
        logger.info(
            f"Injected workspace into {len(self.channels)} channels",
        )

    async def clear_queue(
        self,
        channel_id: str,
        session_id: str,
        priority_level: int,
    ) -> int:
        """Clear a specific queue.

        Args:
            channel_id: Channel identifier
            session_id: Session identifier
            priority_level: Priority level

        Returns:
            Number of messages cleared
        """
        if self._queue_manager is None:
            return 0
        return await self._queue_manager.clear_queue(
            channel_id,
            session_id,
            priority_level,
        )

    async def replace_channel(
        self,
        new_channel: BaseChannel,
    ) -> None:
        """Replace a single channel by name.

        Flow: set enqueue callback → start new (outside lock)
        → swap (inside lock) → stop old (outside lock).

        Args:
            new_channel: New channel instance to replace with

        Note:
            Queue and consumer are created on-demand by UnifiedQueueManager
        """
        new_channel_name = new_channel.channel

        # 1) Set enqueue callback before start() so the channel
        #    (e.g. DingTalk) can register its handler
        if getattr(new_channel, "uses_manager_queue", True):
            new_channel.set_enqueue(self._make_enqueue_cb(new_channel_name))

        # Connect before publishing, with a deadline. Cancellation while
        # waiting for either startup or the manager lock must reclaim the
        # unpublished instance and leave the current channel in service.
        logger.info("Pre-starting new channel: %s", new_channel_name)
        try:
            await asyncio.wait_for(
                new_channel.start(), timeout=_CHANNEL_REPLACE_TIMEOUT_SECONDS,
            )
            async with self._lock:
                old_channel = None
                for i, ch in enumerate(self.channels):
                    if ch.channel == new_channel_name:
                        old_channel = ch
                        self.channels[i] = new_channel
                        break
                if old_channel is None:
                    self.channels.append(new_channel)
        except BaseException:
            await self._stop_channel(new_channel)
            raise

        # A slow retired transport must not hold the registry lock for every
        # other channel (including console chat).
        if old_channel is not None:
            await self._stop_channel(old_channel)

    async def send_event(
        self,
        *,
        channel: str,
        user_id: str,
        session_id: str,
        event: Any,
        meta: Optional[Dict[str, Any]] = None,
    ) -> None:
        ch = await self.get_channel(channel)
        if not ch:
            raise KeyError(f"channel not found: {channel}")
        merged_meta = dict(meta or {})
        merged_meta["session_id"] = session_id
        merged_meta["user_id"] = user_id
        bot_prefix = getattr(ch, "bot_prefix", None) or getattr(
            ch,
            "_bot_prefix",
            None,
        )
        if bot_prefix and "bot_prefix" not in merged_meta:
            merged_meta["bot_prefix"] = bot_prefix
        await ch.send_event(
            user_id=user_id,
            session_id=session_id,
            event=event,
            meta=merged_meta,
        )

    async def send_text(
        self,
        *,
        channel: str,
        user_id: str,
        session_id: str,
        text: str,
        meta: Optional[Dict[str, Any]] = None,
    ) -> None:
        """Send plain text to a specific channel
        (used for scheduled jobs like task_type='text').
        """
        ch = await self.get_channel(channel.lower())
        if not ch:
            raise KeyError(f"channel not found: {channel}")

        # Convert (user_id, session_id) into the channel-specific target handle
        to_handle = ch.to_handle_from_target(
            user_id=user_id,
            session_id=session_id,
        )
        ch_name = getattr(ch, "channel", channel)
        logger.info(
            "channel send_text: channel=%s user_id=%s session_id=%s "
            "to_handle=%s",
            ch_name,
            (user_id or "")[:40],
            (session_id or "")[:40],
            (to_handle or "")[:60],
        )

        # Keep the same behavior as the agent pipeline:
        # if the channel has a fixed bot prefix, merge it into meta so
        # send_content_parts can use it.
        merged_meta = dict(meta or {})
        bot_prefix = getattr(ch, "bot_prefix", None) or getattr(
            ch,
            "_bot_prefix",
            None,
        )
        if bot_prefix and "bot_prefix" not in merged_meta:
            merged_meta["bot_prefix"] = bot_prefix
        merged_meta["session_id"] = session_id
        merged_meta["user_id"] = user_id

        # Send as content parts (single text part; use TextContent so channel
        # getattr(p, "type") / getattr(p, "text") work)
        await ch.send_content_parts(
            to_handle,
            [TextContent(type=ContentType.TEXT, text=text)],
            merged_meta,
        )
