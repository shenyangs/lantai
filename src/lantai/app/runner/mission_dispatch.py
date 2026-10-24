# -*- coding: utf-8 -*-
"""Mission Mode dispatch for the runner.

Integration layer between ``runner.py`` and the Mission Mode engine:

- Detects ``/mission`` in the user query.
- Delegates command parsing to :mod:`~lantai.agents.mission.handler`.
"""
from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Callable

from ...agents.mission.handler import (
    handle_mission_command,
    is_mission_command,
)
from ...runtime.converters import legacy_assistant_text_msg

logger = logging.getLogger(__name__)


# ── Public API ───────────────────────────────────────────────────────────


async def maybe_handle_mission_command(
    query: str | None,
    msgs: list,
    workspace_dir: Path,
    agent_id: str,
    rewrite_fn: Callable[[list, str], None],
    session_id: str = "",
) -> Any | dict[str, Any] | None:
    """Handle ``/mission`` if the query matches.

    Returns:
        Legacy assistant message for info sub-commands.
        ``dict`` with ``{"mission_phase": 1, "loop_dir": ..., ...}``
            if the caller should enter Mission Phase 1 execution.
        ``None`` if the query is not a mission command.
    """
    if not query or not is_mission_command(query):
        return None

    result = await handle_mission_command(
        query=query,
        msgs=msgs,
        workspace_dir=workspace_dir,
        agent_id=agent_id,
        rewrite_fn=rewrite_fn,
        session_id=session_id,
    )

    if isinstance(result, str):
        return legacy_assistant_text_msg(result)

    if isinstance(result, dict):
        return result

    return None
