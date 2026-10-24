# -*- coding: utf-8 -*-
"""Native Runtime interface for Lantai's persisted plan state.

The plan notebook remains a state adapter during the runtime migration.  This
module owns the Native Runtime-facing tool specs, trusted hint projection, and
the planning-turn boundary that prevents execution before user confirmation.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from ..runtime import RuntimeMessage
from ..runtime.converters import legacy_create_plan_notebook
from ..runtime.tool_response import TextBlock, ToolResponse
from ..runtime.tools import LantaiToolRegistry, ToolSpec
from .hints import SimplePlanToHint, set_plan_gate
from .schemas import plan_to_response


_PLAN_TOOL_SCHEMAS: dict[str, dict[str, Any]] = {
    "create_plan": {
        "type": "object",
        "properties": {
            "name": {"type": "string"},
            "description": {"type": "string"},
            "expected_outcome": {"type": "string"},
            "subtasks": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "name": {"type": "string"},
                        "description": {"type": "string"},
                        "expected_outcome": {"type": "string"},
                    },
                    "required": ["name", "description", "expected_outcome"],
                },
            },
        },
        "required": ["name", "description", "expected_outcome", "subtasks"],
    },
    "revise_current_plan": {
        "type": "object",
        "properties": {
            "subtask_idx": {"type": "integer"},
            "action": {"type": "string", "enum": ["add", "revise", "delete"]},
            "subtask": {
                "type": ["object", "null"],
                "properties": {
                    "name": {"type": "string"},
                    "description": {"type": "string"},
                    "expected_outcome": {"type": "string"},
                },
            },
        },
        "required": ["subtask_idx", "action"],
    },
    "update_subtask_state": {
        "type": "object",
        "properties": {
            "subtask_idx": {"type": "integer"},
            "state": {
                "type": "string",
                "enum": ["todo", "in_progress", "abandoned"],
            },
        },
        "required": ["subtask_idx", "state"],
    },
    "finish_subtask": {
        "type": "object",
        "properties": {
            "subtask_idx": {"type": "integer"},
            "subtask_outcome": {"type": "string"},
        },
        "required": ["subtask_idx", "subtask_outcome"],
    },
    "finish_plan": {
        "type": "object",
        "properties": {
            "state": {"type": "string", "enum": ["done", "abandoned"]},
            "outcome": {"type": "string"},
        },
        "required": ["state", "outcome"],
    },
}

_PLAN_TOOL_DESCRIPTIONS = {
    "create_plan": "Create a structured plan and stop for user confirmation.",
    "revise_current_plan": "Add, revise, or delete one plan subtask.",
    "update_subtask_state": "Change one subtask's execution state.",
    "finish_subtask": "Record a completed subtask and activate the next one.",
    "finish_plan": "Complete or abandon the active plan.",
}


def split_explicit_plan_request(
    messages: list[RuntimeMessage],
) -> tuple[bool, list[RuntimeMessage]]:
    """Strip ``/plan`` from the latest user message without mutating input."""

    latest_user_idx = next(
        (
            idx
            for idx in range(len(messages) - 1, -1, -1)
            if messages[idx].role == "user"
        ),
        None,
    )
    if latest_user_idx is None:
        return False, messages
    text = messages[latest_user_idx].text_content().strip()
    if text.lower() == "/plan":
        plan_text = ""
    elif text.lower().startswith("/plan "):
        plan_text = text[6:].strip()
    else:
        return False, messages
    if not plan_text:
        return False, messages

    prepared = list(messages)
    original = messages[latest_user_idx]
    prepared[latest_user_idx] = RuntimeMessage.text(
        "user",
        plan_text,
        id=original.id,
        name=original.name,
        metadata=original.metadata,
    )
    return True, prepared


@dataclass
class PlanRuntimeModule:
    """Plan-mode Module exposed through Native Runtime Interfaces."""

    notebook: Any
    hint_generator: SimplePlanToHint
    explicit_entry: bool

    @classmethod
    async def load(
        cls,
        *,
        session: Any,
        agent_id: str,
        session_id: str,
        user_id: str,
        channel: str,
        explicit_entry: bool,
        broadcast: Callable[[dict[str, Any]], None] | None = None,
    ) -> "PlanRuntimeModule":
        """Load persisted plan state and attach persistence/broadcast hooks."""

        hint_generator = SimplePlanToHint()
        notebook = legacy_create_plan_notebook(plan_to_hint=hint_generator)
        hint_generator.bind_notebook(notebook)

        states = await session.get_session_state_dict(
            session_id=session_id,
            user_id=user_id,
            channel=channel,
            allow_not_exist=True,
        )
        agent_state = states.get("agent", {}) if isinstance(states, dict) else {}
        notebook_state = (
            agent_state.get("plan_notebook")
            if isinstance(agent_state, dict)
            else None
        )
        if isinstance(notebook_state, dict):
            notebook.load_state_dict(notebook_state, strict=False)

        notebook._qp_had_plan = notebook.current_plan is not None
        notebook._qp_prev_plan_id = (
            notebook.current_plan.id if notebook.current_plan is not None else None
        )
        if explicit_entry:
            set_plan_gate(notebook, enabled=True)

        def emit(payload: dict[str, Any]) -> None:
            if broadcast is not None:
                broadcast(payload)
                return
            from .broadcast import broadcast_plan_update

            broadcast_plan_update(agent_id, payload, session_id=session_id)

        async def on_plan_change(nb: Any, plan: Any) -> None:
            had_plan = bool(getattr(nb, "_qp_had_plan", False))
            previous_id = getattr(nb, "_qp_prev_plan_id", None)
            if plan is not None:
                if not had_plan or plan.id != previous_id:
                    nb._plan_just_mutated = True
                nb._qp_prev_plan_id = plan.id
            else:
                if had_plan:
                    nb._plan_recently_finished = True
                nb._qp_prev_plan_id = None
            nb._qp_had_plan = plan is not None

            await session.update_session_state(
                session_id=session_id,
                key="agent.plan_notebook",
                value=nb.state_dict(),
                user_id=user_id,
                channel=channel,
            )
            emit(
                {
                    "type": "plan_update",
                    "plan": (
                        plan_to_response(plan).model_dump()
                        if plan is not None
                        else None
                    ),
                }
            )

        notebook.register_plan_change_hook("native_runtime", on_plan_change)
        return cls(
            notebook=notebook,
            hint_generator=hint_generator,
            explicit_entry=explicit_entry,
        )

    @property
    def is_active(self) -> bool:
        """Whether this turn should see plan tools and trusted plan hints."""

        return self.explicit_entry or self.notebook.current_plan is not None

    def trusted_hint_message(self) -> RuntimeMessage | None:
        """Project notebook state into a trusted Native Runtime message."""

        if not self.is_active:
            return None
        hint = self.hint_generator(self.notebook.current_plan)
        if not hint:
            return None
        return RuntimeMessage.text(
            "system",
            hint,
            metadata={
                "instruction_trust": "trusted_system",
                "instruction_origin": "plan.runtime",
            },
        )

    def tool_registry(self) -> LantaiToolRegistry:
        """Return internal-state plan controls for the active plan flow."""

        registry = LantaiToolRegistry()
        if not self.is_active:
            return registry

        for name in _PLAN_TOOL_SCHEMAS:
            notebook_executor = getattr(self.notebook, name)

            async def execute_plan_tool(
                _executor=notebook_executor,
                _name=name,
                **kwargs: Any,
            ) -> ToolResponse:
                if self.explicit_entry and _name != "create_plan":
                    return ToolResponse(
                        content=[
                            TextBlock(
                                text=(
                                    "Plan creation must finish before execution. "
                                    "Present the plan and wait for user confirmation."
                                )
                            )
                        ],
                        is_error=True,
                    )
                return await _executor(**kwargs)

            registry.register_tool(
                ToolSpec(
                    name=name,
                    description=_PLAN_TOOL_DESCRIPTIONS[name],
                    input_schema=_PLAN_TOOL_SCHEMAS[name],
                    origin="builtin",
                    # These tools mutate only the internal plan notebook.  They
                    # must remain available while permission_mode=plan blocks
                    # environment and external-state effects.
                    read_only=True,
                    concurrency_safe=False,
                    destructive=False,
                    metadata={
                        "runtime_owned": True,
                        "module": "plan",
                        "internal_state_only": True,
                    },
                ),
                execute_plan_tool,
            )
        return registry


__all__ = ["PlanRuntimeModule", "split_explicit_plan_request"]
