# -*- coding: utf-8 -*-
"""Legacy message, memory, and plan classes used by compatibility converters."""

from __future__ import annotations

import inspect
from copy import deepcopy
from collections import OrderedDict
from collections.abc import Awaitable, Callable
from datetime import datetime
from enum import Enum
from typing import Any, Literal
from uuid import uuid4

from pydantic import BaseModel, Field

from ..tool_response import TextBlock, ToolResponse


class LegacyMsg:
    """Lantai-owned message facade compatible with the legacy loop."""

    def __init__(
        self,
        name: str,
        content: str | list[dict[str, Any]],
        role: str,
        metadata: dict[str, Any] | None = None,
        timestamp: str | None = None,
        invocation_id: str | None = None,
    ) -> None:
        if not isinstance(content, (list, str)):
            raise AssertionError(
                "The content must be a string or a list of content blocks.",
            )
        if role not in {"user", "assistant", "system"}:
            raise AssertionError("The role must be user, assistant, or system.")

        self.name = name
        self.content = content
        self.role = role
        self.metadata = metadata or {}
        self.id = uuid4().hex
        self.timestamp: str | None = (
            timestamp
            or datetime.now().strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]
        )
        self.invocation_id = invocation_id

    def to_dict(self) -> dict[str, Any]:
        """Return the persisted legacy message dictionary shape."""

        return {
            "id": self.id,
            "name": self.name,
            "role": self.role,
            "content": self.content,
            "metadata": self.metadata,
            "timestamp": self.timestamp,
        }

    @classmethod
    def from_dict(cls, json_data: dict[str, Any]) -> "LegacyMsg":
        """Rehydrate a message from persisted dictionary data."""

        persisted_timestamp = json_data.get("timestamp")
        msg = cls(
            name=json_data["name"],
            content=json_data["content"],
            role=json_data["role"],
            metadata=json_data.get("metadata", None),
            timestamp=persisted_timestamp,
            invocation_id=json_data.get("invocation_id", None),
        )
        if not persisted_timestamp:
            # Old records without a timestamp have no trustworthy per-message
            # time. Preserve that absence instead of relabeling them with the
            # time at which history happened to be loaded.
            msg.timestamp = None
        msg.id = json_data.get("id", msg.id)
        return msg

    def has_content_blocks(self, block_type: str | list[str] | None = None) -> bool:
        """Return True when content contains at least one matching block."""

        return len(self.get_content_blocks(block_type)) > 0

    def get_text_content(self, separator: str = "\n") -> str | None:
        """Return plain text content from text blocks or string content."""

        if isinstance(self.content, str):
            return self.content
        gathered_text = [
            str(block.get("text") or "")
            for block in self.content
            if isinstance(block, dict) and block.get("type") == "text"
        ]
        if gathered_text:
            return separator.join(gathered_text)
        return None

    def get_content_blocks(
        self,
        block_type: str | list[str] | None = None,
    ) -> list[dict[str, Any]]:
        """Return content blocks, converting string content to one text block."""

        if isinstance(self.content, str):
            blocks = [{"type": "text", "text": self.content}]
        else:
            blocks = list(self.content or [])

        if isinstance(block_type, str):
            return [
                block
                for block in blocks
                if isinstance(block, dict) and block.get("type") == block_type
            ]
        if isinstance(block_type, list):
            return [
                block
                for block in blocks
                if isinstance(block, dict) and block.get("type") in block_type
            ]
        return blocks

    def __repr__(self) -> str:
        return (
            f"Msg(id='{self.id}', "
            f"name='{self.name}', "
            f"content={repr(self.content)}, "
            f"role='{self.role}', "
            f"metadata={repr(self.metadata)}, "
            f"timestamp='{self.timestamp}', "
            f"invocation_id='{self.invocation_id}')"
        )


def is_legacy_msg_like(value: Any) -> bool:
    """Return True for local or old message objects with the same surface."""

    return (
        isinstance(value, LegacyMsg)
        or hasattr(value, "content")
        and hasattr(value, "role")
        and hasattr(value, "name")
        and callable(getattr(value, "get_text_content", None))
        and callable(getattr(value, "get_content_blocks", None))
    )


class LegacyMemoryMark(str, Enum):
    """Memory marks used by the legacy reactive loop."""

    HINT = "hint"
    COMPRESSED = "compressed"


class LegacyInMemoryMemory:
    """Lantai-owned in-memory message store compatible with legacy callers."""

    def __init__(self) -> None:
        self.content: list[tuple[LegacyMsg, list[str]]] = []
        self._compressed_summary: str = ""

    async def get_memory(
        self,
        mark: str | None = None,
        exclude_mark: str | None = None,
        prepend_summary: bool = True,
        **_kwargs: Any,
    ) -> list[LegacyMsg]:
        """Return stored messages, optionally filtered by mark."""

        if not (mark is None or isinstance(mark, str)):
            raise TypeError(
                f"The mark should be a string or None, but got {type(mark)}.",
            )
        if not (exclude_mark is None or isinstance(exclude_mark, str)):
            raise TypeError(
                "The exclude_mark should be a string or None, but got "
                f"{type(exclude_mark)}.",
            )

        filtered_content = [
            (msg, marks)
            for msg, marks in self.content
            if mark is None or mark in marks
        ]
        if exclude_mark is not None:
            filtered_content = [
                (msg, marks)
                for msg, marks in filtered_content
                if exclude_mark not in marks
            ]

        if prepend_summary and self._compressed_summary:
            return [
                LegacyMsg("user", self._compressed_summary, "user"),
                *[msg for msg, _marks in filtered_content],
            ]
        return [msg for msg, _marks in filtered_content]

    async def add(
        self,
        memories: LegacyMsg | list[LegacyMsg] | None,
        marks: str | list[str] | None = None,
        allow_duplicates: bool = False,
        **_kwargs: Any,
    ) -> None:
        """Add message(s) to memory with optional marks."""

        if memories is None:
            return
        if is_legacy_msg_like(memories):
            memories = [memories]
        else:
            memories = list(memories)

        if marks is None:
            normalized_marks: list[str] = []
        elif isinstance(marks, str):
            normalized_marks = [marks]
        elif isinstance(marks, list) and all(isinstance(m, str) for m in marks):
            normalized_marks = marks
        else:
            raise TypeError(
                "The mark should be a string, a list of strings, or None, "
                f"but got {type(marks)}.",
            )

        if not allow_duplicates:
            existing_ids = {msg.id for msg, _marks in self.content}
            memories = [msg for msg in memories if msg.id not in existing_ids]

        for msg in memories:
            self.content.append((deepcopy(msg), deepcopy(normalized_marks)))

    async def delete(self, msg_ids: list[str], **_kwargs: Any) -> int:
        """Remove messages by ids."""

        initial_size = len(self.content)
        self.content = [
            (msg, marks)
            for msg, marks in self.content
            if msg.id not in msg_ids
        ]
        return initial_size - len(self.content)

    async def delete_by_mark(
        self,
        mark: str | list[str],
        **_kwargs: Any,
    ) -> int:
        """Remove messages by one or more marks."""

        if isinstance(mark, str):
            marks = [mark]
        elif isinstance(mark, list) and all(isinstance(m, str) for m in mark):
            marks = mark
        else:
            raise TypeError(
                "The mark should be a string or a list of strings, "
                f"but got {type(mark)}.",
            )

        initial_size = len(self.content)
        for item in marks:
            self.content = [
                (msg, msg_marks)
                for msg, msg_marks in self.content
                if item not in msg_marks
            ]
        return initial_size - len(self.content)

    async def clear(self) -> None:
        """Clear memory content."""

        self.content.clear()

    async def size(self) -> int:
        """Return number of stored messages."""

        return len(self.content)

    async def update_compressed_summary(self, summary: str) -> None:
        """Update compressed summary text."""

        self._compressed_summary = summary

    async def update_messages_mark(
        self,
        new_mark: str | None,
        old_mark: str | None = None,
        msg_ids: list[str] | None = None,
    ) -> int:
        """Add, remove, or replace marks on stored messages."""

        updated_count = 0
        for index, (msg, marks) in enumerate(self.content):
            if msg_ids is not None and msg.id not in msg_ids:
                continue
            if old_mark is not None and old_mark not in marks:
                continue
            if new_mark is None:
                if old_mark in marks:
                    marks.remove(old_mark)
                    updated_count += 1
            else:
                if old_mark is not None and old_mark in marks:
                    marks.remove(old_mark)
                if new_mark not in marks:
                    marks.append(new_mark)
                    updated_count += 1
            self.content[index] = (msg, marks)
        return updated_count

    def state_dict(self) -> dict[str, Any]:
        """Return persisted memory state."""

        return {
            "_compressed_summary": self._compressed_summary,
            "content": [[msg.to_dict(), marks] for msg, marks in self.content],
        }

    def load_state_dict(
        self,
        state_dict: dict[str, Any],
        strict: bool = True,
    ) -> None:
        """Load persisted memory state."""

        if strict and "content" not in state_dict:
            raise KeyError(
                "The state_dict does not contain 'content' "
                "keys required for LegacyInMemoryMemory.",
            )

        self._compressed_summary = state_dict.get("_compressed_summary", "")
        self.content = []
        for item in state_dict.get("content", []):
            if isinstance(item, (tuple, list)) and len(item) == 2:
                msg_dict, marks = item
                msg = LegacyMsg.from_dict(msg_dict)
                self.content.append((msg, marks))
            elif isinstance(item, dict):
                msg = LegacyMsg.from_dict(item)
                self.content.append((msg, []))
            else:
                raise ValueError(
                    "Invalid item format in state_dict for LegacyInMemoryMemory.",
                )


def _get_timestamp() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]


async def _execute_async_or_sync_func(
    func: Callable[..., Any],
    *args: Any,
    **kwargs: Any,
) -> Any:
    result = func(*args, **kwargs)
    if inspect.isawaitable(result):
        return await result
    return result


class LegacySubTask(BaseModel):
    """Subtask model used by the local legacy plan notebook."""

    name: str = Field(
        description="The subtask name, should be concise and descriptive.",
    )
    description: str = Field(
        description="The subtask description and constraints.",
    )
    expected_outcome: str = Field(
        description="The concrete measurable expected outcome.",
    )
    outcome: str | None = None
    state: Literal["todo", "in_progress", "done", "abandoned"] = "todo"
    created_at: str = Field(default_factory=_get_timestamp)
    finished_at: str | None = None

    def finish(self, outcome: str) -> None:
        """Mark the subtask as done with the concrete outcome."""

        self.state = "done"
        self.outcome = outcome
        self.finished_at = _get_timestamp()

    def to_oneline_markdown(self) -> str:
        """Convert the subtask to one-line Markdown."""

        status_map = {
            "todo": "- []",
            "in_progress": "- [][WIP]",
            "done": "- [x]",
            "abandoned": "- [][Abandoned]",
        }
        return f"{status_map[self.state]} {self.name}"

    def to_markdown(self, detailed: bool = False) -> str:
        """Convert the subtask to Markdown."""

        status_map = {
            "todo": "- [ ] ",
            "in_progress": "- [ ] [WIP]",
            "done": "- [x] ",
            "abandoned": "- [ ] [Abandoned]",
        }
        if not detailed:
            return f"{status_map[self.state]}{self.name}"

        lines = [
            f"{status_map[self.state]}{self.name}",
            f"\t- Created At: {self.created_at}",
            f"\t- Description: {self.description}",
            f"\t- Expected Outcome: {self.expected_outcome}",
            f"\t- State: {self.state}",
        ]
        if self.state == "done":
            lines.extend(
                [
                    f"\t- Finished At: {self.finished_at}",
                    f"\t- Actual Outcome: {self.outcome}",
                ],
            )
        return "\n".join(lines)


class LegacyPlan(BaseModel):
    """Plan model used by the local legacy plan notebook."""

    id: str = Field(default_factory=lambda: uuid4().hex[:22])
    name: str
    description: str
    expected_outcome: str
    subtasks: list[LegacySubTask] = Field(default_factory=list)
    created_at: str = Field(default_factory=_get_timestamp)
    state: Literal["todo", "in_progress", "done", "abandoned"] = "todo"
    finished_at: str | None = None
    outcome: str | None = None

    def refresh_plan_state(self) -> str:
        """Refresh todo/in_progress state from subtask states."""

        if self.state in {"done", "abandoned"}:
            return ""
        any_in_progress = any(
            subtask.state == "in_progress" for subtask in self.subtasks
        )
        if any_in_progress and self.state == "todo":
            self.state = "in_progress"
            return "The plan state has been updated to 'in_progress'."
        if not any_in_progress and self.state == "in_progress":
            self.state = "todo"
            return "The plan state has been updated to 'todo'."
        return ""

    def finish(
        self,
        state: Literal["done", "abandoned"],
        outcome: str,
    ) -> None:
        """Finish the plan."""

        self.state = state
        self.outcome = outcome
        self.finished_at = _get_timestamp()

    def to_markdown(self, detailed: bool = False) -> str:
        """Convert the plan to Markdown."""

        subtasks_markdown = "\n".join(
            [
                subtask.to_markdown(detailed=detailed)
                for subtask in self.subtasks
            ],
        )
        return "\n".join(
            [
                f"# {self.name}",
                f"**Description**: {self.description}",
                f"**Expected Outcome**: {self.expected_outcome}",
                f"**State**: {self.state}",
                f"**Created At**: {self.created_at}",
                "## Subtasks",
                subtasks_markdown,
            ],
        )


class LegacyInMemoryPlanStorage:
    """In-memory storage compatible with the old plan notebook state shape."""

    def __init__(self) -> None:
        self.plans: OrderedDict[str, LegacyPlan] = OrderedDict()

    async def add_plan(
        self,
        plan: LegacyPlan,
        override: bool = True,
    ) -> None:
        """Add a plan to history."""

        if plan.id in self.plans and not override:
            raise ValueError(f"Plan with id {plan.id} already exists.")
        self.plans[plan.id] = plan

    async def delete_plan(self, plan_id: str) -> None:
        """Delete a historical plan by id."""

        self.plans.pop(plan_id, None)

    async def get_plans(self) -> list[LegacyPlan]:
        """Return all historical plans."""

        return list(self.plans.values())

    async def get_plan(self, plan_id: str) -> LegacyPlan | None:
        """Return a historical plan by id."""

        return self.plans.get(plan_id)

    def state_dict(self) -> dict[str, Any]:
        """Return persisted storage state."""

        return {
            "plans": {
                plan_id: plan.model_dump()
                for plan_id, plan in self.plans.items()
            },
        }

    def load_state_dict(
        self,
        state_dict: dict[str, Any],
        strict: bool = True,
    ) -> None:
        """Load persisted storage state."""

        if strict and "plans" not in state_dict:
            raise KeyError("Key 'plans' not found in plan storage state.")
        self.plans = OrderedDict()
        for plan_id, plan_data in (state_dict.get("plans") or {}).items():
            self.plans[str(plan_id)] = LegacyPlan.model_validate(plan_data)


class LegacyDefaultPlanToHint:
    """Default hint generator used when the runner does not inject one."""

    hint_prefix = "<system-hint>"
    hint_suffix = "</system-hint>"
    no_plan = (
        "If the user's query is complex, create a plan first by calling "
        "'create_plan'. Otherwise, answer directly without planning."
    )

    def __call__(self, plan: LegacyPlan | None) -> str | None:
        if plan is None:
            return f"{self.hint_prefix}{self.no_plan}{self.hint_suffix}"
        return (
            f"{self.hint_prefix}The current plan:\n```\n"
            f"{plan.to_markdown()}\n```{self.hint_suffix}"
        )


class LegacyPlanNotebook:
    """Lantai-owned plan notebook compatible with the legacy ReAct loop."""

    description = (
        "The plan-related tools. Activate this tool when you need to execute "
        "complex task, e.g. building a website or a game. Once activated, "
        "you'll enter the plan mode, where you will be guided to complete "
        "the given query by creating and following a plan, and hint message "
        "wrapped by <system-hint></system-hint> will guide you to complete "
        "the task. If you think the user no longer wants to perform the "
        "current task, you need to confirm with the user and call the "
        "'finish_plan' function."
    )

    def __init__(
        self,
        max_subtasks: int | None = None,
        plan_to_hint: Callable[[LegacyPlan | None], str | None] | None = None,
        storage: LegacyInMemoryPlanStorage | None = None,
    ) -> None:
        self.max_tasks = max_subtasks
        self.plan_to_hint = plan_to_hint or LegacyDefaultPlanToHint()
        self.storage = storage or LegacyInMemoryPlanStorage()
        self.current_plan: LegacyPlan | None = None
        self._plan_change_hooks: OrderedDict[
            str,
            Callable[["LegacyPlanNotebook", LegacyPlan | None], None]
            | Callable[["LegacyPlanNotebook", LegacyPlan | None], Awaitable[None]],
        ] = OrderedDict()

    async def create_plan(
        self,
        name: str,
        description: str,
        expected_outcome: str,
        subtasks: list[LegacySubTask | dict[str, Any]],
    ) -> ToolResponse:
        """Create a plan by given name and sub-tasks."""

        normalized_subtasks = [
            subtask
            if isinstance(subtask, LegacySubTask)
            else LegacySubTask.model_validate(subtask)
            for subtask in subtasks
        ]
        plan = LegacyPlan(
            name=name,
            description=description,
            expected_outcome=expected_outcome,
            subtasks=normalized_subtasks,
        )
        if self.current_plan is None:
            text = f"Plan '{name}' created successfully."
        else:
            text = (
                "The current plan named "
                f"'{self.current_plan.name}' is replaced by the newly "
                f"created plan named '{name}'."
            )

        self.current_plan = plan
        await self._trigger_plan_change_hooks()
        return ToolResponse(content=[TextBlock(type="text", text=text)])

    def _validate_current_plan(self) -> None:
        if self.current_plan is None:
            raise ValueError(
                "The current plan is None, you need to create a plan by "
                "calling create_plan() first.",
            )

    async def revise_current_plan(
        self,
        subtask_idx: int,
        action: Literal["add", "revise", "delete"],
        subtask: LegacySubTask | dict[str, Any] | None = None,
    ) -> ToolResponse:
        """Revise the current plan by adding, revising or deleting a sub-task."""

        response: list[str] = []
        if isinstance(subtask_idx, str):
            try:
                subtask_idx = int(subtask_idx)
            except ValueError:
                return _plan_text_response(
                    f"Error: Invalid type for argument 'subtask_idx'. "
                    f"Expected 'int', but got '{type(subtask_idx)}'.",
                )

        if action not in ["add", "revise", "delete"]:
            response.append(
                f"Invalid action '{action}'. Must be one of 'add', 'revise', "
                "'delete'.",
            )
        if action in ["add", "revise"] and subtask is None:
            response.append(
                f"The subtask must be provided when action is '{action}', "
                "but got None.",
            )

        self._validate_current_plan()
        assert self.current_plan is not None

        if action != "add" and subtask_idx >= len(self.current_plan.subtasks):
            response.append(
                f"Invalid subtask_idx '{subtask_idx}' for action '{action}'. "
                f"Must be between 0 and {len(self.current_plan.subtasks) - 1}.",
            )
        if action == "add" and not (
            0 <= subtask_idx <= len(self.current_plan.subtasks)
        ):
            response.append(
                f"Invalid subtask_idx '{subtask_idx}' for action 'add'. "
                f"Must be between 0 and {len(self.current_plan.subtasks)}.",
            )
        if response:
            return _plan_text_response("Error: " + response[0])

        normalized_subtask = (
            subtask
            if isinstance(subtask, LegacySubTask) or subtask is None
            else LegacySubTask.model_validate(subtask)
        )
        if action == "delete":
            removed = self.current_plan.subtasks.pop(subtask_idx)
            await self._trigger_plan_change_hooks()
            return _plan_text_response(
                f"Subtask (named '{removed.name}') at index {subtask_idx} "
                "is deleted successfully.",
            )
        if action == "add" and normalized_subtask is not None:
            self.current_plan.subtasks.insert(subtask_idx, normalized_subtask)
            await self._trigger_plan_change_hooks()
            return _plan_text_response(
                f"New subtask is added successfully at index {subtask_idx}.",
            )

        self.current_plan.subtasks[subtask_idx] = normalized_subtask
        await self._trigger_plan_change_hooks()
        return _plan_text_response(
            f"Subtask at index {subtask_idx} is revised successfully.",
        )

    async def update_subtask_state(
        self,
        subtask_idx: int,
        state: Literal["todo", "in_progress", "abandoned"],
    ) -> ToolResponse:
        """Update the state of a subtask by index."""

        self._validate_current_plan()
        assert self.current_plan is not None
        if isinstance(subtask_idx, str):
            try:
                subtask_idx = int(subtask_idx)
            except ValueError:
                pass
        if not isinstance(subtask_idx, int):
            return _plan_text_response(
                f"Invalid type for argument 'subtask_idx'. Expected 'int', "
                f"but got '{type(subtask_idx)}'.",
            )
        if not 0 <= subtask_idx < len(self.current_plan.subtasks):
            return _plan_text_response(
                f"Invalid subtask_idx '{subtask_idx}'. Must be between 0 and "
                f"{len(self.current_plan.subtasks) - 1}.",
            )
        if state not in ["todo", "in_progress", "abandoned"]:
            return _plan_text_response(
                f"Invalid state '{state}'. Must be one of 'todo', "
                "'in_progress', 'abandoned'.",
            )

        if state == "in_progress":
            for idx, subtask in enumerate(self.current_plan.subtasks):
                if idx < subtask_idx and subtask.state not in [
                    "done",
                    "abandoned",
                ]:
                    return _plan_text_response(
                        f"Subtask (at index {idx}) named '{subtask.name}' "
                        "is not done yet. You should finish the previous "
                        "subtasks first.",
                    )
                if subtask.state == "in_progress":
                    return _plan_text_response(
                        f"Subtask (at index {idx}) named '{subtask.name}' "
                        "is already 'in_progress'. You should finish it "
                        "first before starting another subtask.",
                    )

        self.current_plan.subtasks[subtask_idx].state = state
        suffix = self.current_plan.refresh_plan_state()
        await self._trigger_plan_change_hooks()
        return _plan_text_response(
            f"Subtask at index {subtask_idx}, named "
            f"'{self.current_plan.subtasks[subtask_idx].name}' is marked as "
            f"'{state}' successfully. " + suffix,
        )

    async def finish_subtask(
        self,
        subtask_idx: int,
        subtask_outcome: str,
    ) -> ToolResponse:
        """Label the subtask as done by given index and outcome."""

        self._validate_current_plan()
        assert self.current_plan is not None
        if isinstance(subtask_idx, str):
            try:
                subtask_idx = int(subtask_idx)
            except ValueError:
                pass
        if not isinstance(subtask_idx, int):
            return _plan_text_response(
                f"Invalid type for argument 'subtask_idx'. Expected 'int', "
                f"but got '{type(subtask_idx)}'.",
            )
        if not 0 <= subtask_idx < len(self.current_plan.subtasks):
            return _plan_text_response(
                f"Invalid subtask_idx '{subtask_idx}'. Must be between 0 and "
                f"{len(self.current_plan.subtasks) - 1}.",
            )
        for idx, subtask in enumerate(self.current_plan.subtasks[:subtask_idx]):
            if subtask.state not in ["done", "abandoned"]:
                return _plan_text_response(
                    "Cannot finish subtask at index "
                    f"{subtask_idx} because the previous subtask (at index "
                    f"{idx}) named '{subtask.name}' is not done yet. You "
                    "should finish the previous subtasks first.",
                )

        self.current_plan.subtasks[subtask_idx].finish(subtask_outcome)
        if subtask_idx + 1 < len(self.current_plan.subtasks):
            self.current_plan.subtasks[subtask_idx + 1].state = "in_progress"
            next_subtask = self.current_plan.subtasks[subtask_idx + 1]
            await self._trigger_plan_change_hooks()
            return _plan_text_response(
                f"Subtask (at index {subtask_idx}) named "
                f"'{self.current_plan.subtasks[subtask_idx].name}' is marked "
                f"as done successfully. The next subtask named "
                f"'{next_subtask.name}' is activated.",
            )

        await self._trigger_plan_change_hooks()
        return _plan_text_response(
            f"Subtask (at index {subtask_idx}) named "
            f"'{self.current_plan.subtasks[subtask_idx].name}' is marked as "
            "done successfully. ",
        )

    async def view_subtasks(self, subtask_idx: list[int]) -> ToolResponse:
        """View the details of sub-tasks by indexes."""

        self._validate_current_plan()
        assert self.current_plan is not None
        gathered_strs: list[str] = []
        invalid_subtask_idx: list[int] = []
        for idx in subtask_idx:
            if not 0 <= idx < len(self.current_plan.subtasks):
                invalid_subtask_idx.append(idx)
                continue
            subtask_markdown = self.current_plan.subtasks[idx].to_markdown(
                detailed=True,
            )
            gathered_strs.append(
                f"Subtask at index {idx}:\n```\n{subtask_markdown}\n```\n",
            )
        if invalid_subtask_idx:
            gathered_strs.append(
                f"Invalid subtask_idx '{invalid_subtask_idx}'. Must be "
                f"between 0 and {len(self.current_plan.subtasks) - 1}.",
            )
        return _plan_text_response("\n".join(gathered_strs))

    async def finish_plan(
        self,
        state: Literal["done", "abandoned"],
        outcome: str,
    ) -> ToolResponse:
        """Finish or abandon the current plan."""

        if self.current_plan is None:
            return _plan_text_response("There is no plan to finish.")

        self.current_plan.finish(state, outcome)
        await self.storage.add_plan(self.current_plan)
        self.current_plan = None
        await self._trigger_plan_change_hooks()
        return _plan_text_response(
            f"The current plan is finished successfully as '{state}'.",
        )

    async def view_historical_plans(self) -> ToolResponse:
        """View historical plans."""

        historical_plans = await self.storage.get_plans()
        plans_str = [
            f"""Plan named '{plan.name}':
- ID: {plan.id}
- Created at: {plan.created_at}
- Description: {plan.description}
- State: {plan.state}
"""
            for plan in historical_plans
        ]
        return _plan_text_response("\n".join(plans_str))

    async def recover_historical_plan(self, plan_id: str) -> ToolResponse:
        """Recover a historical plan by plan ID."""

        historical_plan = await self.storage.get_plan(plan_id)
        if historical_plan is None:
            return _plan_text_response(f"Cannot find the plan with ID '{plan_id}'.")

        if self.current_plan:
            if self.current_plan.state != "done":
                self.current_plan.finish(
                    "abandoned",
                    "The plan execution is interrupted by a new plan "
                    f"with ID '{historical_plan.id}'.",
                )
            await self.storage.add_plan(self.current_plan)
            text = (
                "The current plan named "
                f"'{self.current_plan.name}' is replaced by the historical "
                f"plan named '{historical_plan.name}' with ID "
                f"'{historical_plan.id}'."
            )
        else:
            text = (
                f"Historical plan named '{historical_plan.name}' with ID "
                f"'{historical_plan.id}' is recovered successfully."
            )
        self.current_plan = historical_plan
        await self._trigger_plan_change_hooks()
        return _plan_text_response(text)

    def list_tools(
        self,
    ) -> list[Callable[..., Awaitable[ToolResponse]]]:
        """List all plan tool functions provided to the agent."""

        return [
            self.view_subtasks,
            self.update_subtask_state,
            self.finish_subtask,
            self.create_plan,
            self.revise_current_plan,
            self.finish_plan,
            self.view_historical_plans,
            self.recover_historical_plan,
        ]

    async def get_current_hint(self) -> LegacyMsg | None:
        """Return a hint message based on the current plan."""

        hint_content = self.plan_to_hint(self.current_plan)
        if hint_content:
            return LegacyMsg("user", hint_content, "user")
        return None

    def register_plan_change_hook(
        self,
        hook_name: str,
        hook: Callable[["LegacyPlanNotebook", LegacyPlan | None], None]
        | Callable[["LegacyPlanNotebook", LegacyPlan | None], Awaitable[None]],
    ) -> None:
        """Register a hook triggered when the plan changes."""

        self._plan_change_hooks[hook_name] = hook

    def remove_plan_change_hook(self, hook_name: str) -> None:
        """Remove a plan change hook by name."""

        if hook_name in self._plan_change_hooks:
            self._plan_change_hooks.pop(hook_name)
            return
        raise ValueError(f"Hook '{hook_name}' not found.")

    async def _trigger_plan_change_hooks(self) -> None:
        """Trigger registered plan change hooks."""

        for hook in self._plan_change_hooks.values():
            await _execute_async_or_sync_func(hook, self, self.current_plan)

    def state_dict(self) -> dict[str, Any]:
        """Return persisted notebook state using the old shape."""

        return {
            "storage": self.storage.state_dict(),
            "current_plan": (
                self.current_plan.model_dump() if self.current_plan else None
            ),
        }

    def load_state_dict(
        self,
        state_dict: dict[str, Any],
        strict: bool = True,
    ) -> None:
        """Load persisted notebook state."""

        if strict:
            for key in ("storage", "current_plan"):
                if key not in state_dict:
                    raise KeyError(f"Key '{key}' not found in plan notebook state.")
        self.storage.load_state_dict(state_dict.get("storage") or {}, strict=False)
        current_plan = state_dict.get("current_plan")
        self.current_plan = (
            LegacyPlan.model_validate(current_plan) if current_plan else None
        )


def _plan_text_response(text: str) -> ToolResponse:
    return ToolResponse(content=[TextBlock(type="text", text=text)])


__all__ = [
    "LegacyInMemoryMemory",
    "LegacyInMemoryPlanStorage",
    "LegacyMemoryMark",
    "LegacyMsg",
    "LegacyPlan",
    "LegacyPlanNotebook",
    "LegacySubTask",
    "is_legacy_msg_like",
]
