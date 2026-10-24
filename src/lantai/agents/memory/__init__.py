# -*- coding: utf-8 -*-
"""Memory management module for 澜台 agents."""

from typing import TYPE_CHECKING

from .canonical_ledger import CanonicalMemoryLedger, ConfirmedFact, MemoryEvent

# Proactive symbols are lazily re-exported via __getattr__ at runtime to
# avoid circular imports (proactive -> legacy_lantai_agent -> agents.memory loop).
# The TYPE_CHECKING block below satisfies static analysis tools (pylint, mypy).
if TYPE_CHECKING:  # pragma: no cover
    from .agent_md_manager import AgentMdManager
    from .base_memory_manager import BaseMemoryManager
    from .reme_light_memory_manager import ReMeLightMemoryManager
    from .proactive import (
        ProactiveConfig,
        ProactiveQueryResult,
        ProactiveTask,
        enable_proactive_for_session,
        extract_content,
        generate_proactive_response,
        proactive_configs,
        proactive_tasks,
        proactive_trigger_loop,
    )

# pylint: disable=undefined-all-variable
__all__ = [
    "AgentMdManager",
    "BaseMemoryManager",
    "ReMeLightMemoryManager",
    "CanonicalMemoryLedger",
    "ConfirmedFact",
    "MemoryEvent",
    # proactive symbols resolved lazily at runtime via __getattr__
    "ProactiveConfig",
    "ProactiveTask",
    "ProactiveQueryResult",
    "enable_proactive_for_session",
    "proactive_trigger_loop",
    "proactive_tasks",
    "proactive_configs",
    "generate_proactive_response",
    "extract_content",
]

_PROACTIVE_EXPORTS = {
    "ProactiveConfig",
    "ProactiveTask",
    "ProactiveQueryResult",
    "enable_proactive_for_session",
    "proactive_trigger_loop",
    "proactive_tasks",
    "proactive_configs",
    "generate_proactive_response",
    "extract_content",
}


def __getattr__(name: str):
    # Persona stores use the memory fence before their classes are defined.
    # The manager imports migration, which consumes those completed classes.
    if name == "ReMeLightMemoryManager":
        from .reme_light_memory_manager import ReMeLightMemoryManager

        return ReMeLightMemoryManager
    if name == "AgentMdManager":
        from .agent_md_manager import AgentMdManager

        return AgentMdManager
    if name == "BaseMemoryManager":
        from .base_memory_manager import BaseMemoryManager

        return BaseMemoryManager
    if name in _PROACTIVE_EXPORTS:
        from . import proactive as _proactive  # noqa: PLC0415

        return getattr(_proactive, name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
