# -*- coding: utf-8 -*-
"""Runtime adapter protocol owned by Lantai."""

from __future__ import annotations

from typing import AsyncIterator, Protocol

from .events import RuntimeEvent
from .input import RuntimeInput


class RuntimeAdapter(Protocol):
    """Concrete runtime implementations emit RuntimeEvent streams."""

    async def run_turn(
        self,
        runtime_input: RuntimeInput,
    ) -> AsyncIterator[RuntimeEvent]:
        """Run one turn and yield normalized runtime events."""
        ...
