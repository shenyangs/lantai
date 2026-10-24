"""Task-local identity for the Runtime Effect currently being executed."""

from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from typing import Iterator


@dataclass(frozen=True, slots=True)
class RuntimeEffectContext:
    """The already-started Runtime Effect that owns an external operation."""

    run_id: str
    attempt_id: str
    effect_id: str
    state_workspace_dir: str
    idempotency_key: str | None = None


_CURRENT_RUNTIME_EFFECT: ContextVar[RuntimeEffectContext | None] = ContextVar(
    "lantai_runtime_effect_context",
    default=None,
)


@contextmanager
def bind_runtime_effect_context(
    context: RuntimeEffectContext,
) -> Iterator[RuntimeEffectContext]:
    """Expose one started Effect only for the duration of its tool call."""

    token = _CURRENT_RUNTIME_EFFECT.set(context)
    try:
        yield context
    finally:
        _CURRENT_RUNTIME_EFFECT.reset(token)


def current_runtime_effect_context() -> RuntimeEffectContext | None:
    """Return the task-local Effect identity, if this call owns one."""

    return _CURRENT_RUNTIME_EFFECT.get()


__all__ = [
    "RuntimeEffectContext",
    "bind_runtime_effect_context",
    "current_runtime_effect_context",
]
