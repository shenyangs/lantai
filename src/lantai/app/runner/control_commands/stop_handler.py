# -*- coding: utf-8 -*-
"""Handler for /stop command.

The /stop command requests cancellation and reports actual settlement.
"""

from __future__ import annotations

import logging

from .base import BaseControlCommandHandler, ControlContext

logger = logging.getLogger(__name__)


class StopCommandHandler(BaseControlCommandHandler):
    """Handler for /stop command.

    Features:
    - Immediate response (priority level 0)
    - Stops task via TaskTracker.request_stop (native cancellation)
    - Default: stops current session
    - Optional: specify target session_id

    Usage:
        /stop                  # Stop current session
        /stop session=console:user1  # Stop specific session
    """

    command_name = "/stop"

    async def handle(self, context: ControlContext) -> str:
        """Handle /stop command.

        Args:
            context: Control command context

        Returns:
            Response text (success or error message)
        """
        target_session_id = context.args.get(
            "session",
            context.session_id,
        )

        logger.info(
            f"/stop command: current_session={context.session_id[:30]} "
            f"target_session={target_session_id[:30]}",
        )

        workspace = context.workspace
        channel_id = context.channel.channel

        chat_id = await workspace.chat_manager.get_chat_id_by_session(
            target_session_id,
            channel_id,
        )

        if chat_id is None:
            logger.warning(
                f"/stop: No active chat found for "
                f"session={target_session_id[:30]} channel={channel_id}",
            )
            return (
                f"**No Active Task**\n\n"
                f"No running task found for session "
                f"`{target_session_id[:40]}`."
            )

        tracker = workspace.task_tracker
        observed = await tracker.get_settlement_snapshot(chat_id)
        stopped = False
        if observed is not None:
            stopped = await tracker.request_stop_if_attempt(
                chat_id,
                attempt_id=observed["attempt_id"],
                generation=observed["generation"],
            )

        cleared = await workspace.channel_manager.clear_queue(
            channel_id,
            target_session_id,
            20,
        )

        remaining = await tracker.get_settlement_snapshot(chat_id)
        queue_note = (
            f" {cleared} queued message(s) cleared." if cleared > 0 else ""
        )
        if remaining is not None:
            same_attempt = observed is not None and (
                remaining["attempt_id"] == observed["attempt_id"]
                and remaining["generation"] == observed["generation"]
            )
            if not same_attempt:
                return (
                    "**Task Changed**\n\n"
                    "The active task changed while stopping; the new task "
                    "was not stopped." + queue_note
                )
            if remaining["status"] == "cleanup_failed":
                return (
                    "**Cleanup Failed**\n\n"
                    "The task has not finished releasing its resources. "
                    "It cannot be reported as stopped." + queue_note
                )
            return (
                "**Stopping**\n\n"
                "The task is still finishing cancellation or cleanup. "
                "Its resources have not been released yet." + queue_note
            )

        if stopped:
            return (
                "**Task Stopped**\n\n"
                f"Session `{target_session_id[:40]}`: running task stopped."
                + queue_note
            )
        if cleared > 0:
            return "**Queue Cleared**\n\n" + queue_note.strip()
        return (
            "**Task Not Running**\n\n"
            f"No active task or queued messages for session "
            f"`{target_session_id[:40]}`."
        )
