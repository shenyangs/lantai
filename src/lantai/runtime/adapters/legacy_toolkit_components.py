# -*- coding: utf-8 -*-
"""Legacy toolkit classes used by the compatibility adapter."""

from __future__ import annotations

import asyncio
import contextvars
import inspect
import json
import logging
import os
import uuid
from collections.abc import AsyncGenerator, Awaitable, Callable, Generator
from copy import deepcopy
from dataclasses import dataclass
from functools import partial
from typing import Any

import mcp.types
from pydantic import BaseModel, Field

from ..progressive_tools import transition_progressive_tool_groups
from ..tool_response import AudioBlock, ImageBlock, TextBlock, ToolResponse

logger = logging.getLogger(__name__)


@dataclass
class LegacyToolGroup:
    """Small tool-group record compatible with the old caller surface."""

    name: str
    description: str
    active: bool = False
    notes: str | None = None
    transient: bool = False


@dataclass
class LegacyRegisteredToolFunction:
    """Registered tool metadata used by the compatibility toolkit."""

    name: str
    group: str
    source: str
    original_func: Callable[..., Any]
    json_schema: dict[str, Any]
    preset_kwargs: dict[str, Any]
    original_name: str | None = None
    extended_model: type[BaseModel] | None = None
    mcp_name: str | None = None
    postprocess_func: Callable[..., Any] | None = None
    async_execution: bool = False

    @property
    def extended_json_schema(self) -> dict[str, Any]:
        """Return the callable schema, with structured-output model merged."""

        schema = deepcopy(self.json_schema)
        if self.extended_model is not None:
            schema.setdefault("function", {})["parameters"] = (
                self.extended_model.model_json_schema()
            )
        return schema


class LegacyToolkit:
    """Lantai-owned toolkit object for the legacy reactive loop.

    The compatibility agent only needs a small duck-typed surface:
    registering tools/skills, producing tool schemas, executing tools as
    async streams, async-task helpers, and tool-group state.  Keeping this
    implementation local lets the rest of the compatibility path shed the old
    toolkit dependency without changing user-visible behavior.
    """

    _DEFAULT_AGENT_SKILL_INSTRUCTION = (
        "# Agent Skills\n"
        "Agent skills contain instructions, scripts, and resources for "
        "specialized tasks. Before using a skill, you MUST load its "
        "`SKILL.md` with the `read_skill` tool. Do not use `read_file` on "
        "skill installation paths. Load relative skill resources by calling "
        "`read_skill` again with the same skill name and relative file path. "
        "Use `list_skill_resources` to discover bundled resources and "
        "`run_skill_script` to execute a documented `scripts/...` resource. "
        "Use `copy_skill_resource` for binary assets and templates. "
        "Never search the filesystem for Skill install directories."
    )
    _DEFAULT_AGENT_SKILL_TEMPLATE = """## {name}
{description}
Load with `read_skill(skill_name="{name}", file_path="SKILL.md")`."""

    tool_response_type = ToolResponse

    def __init__(
        self,
        agent_skill_instruction: str | None = None,
        agent_skill_template: str | None = None,
    ) -> None:
        self.tools: dict[str, LegacyRegisteredToolFunction] = {}
        self.groups: dict[str, LegacyToolGroup] = {}
        self.skills: dict[str, dict[str, str]] = {}
        self._middlewares: list[Callable[..., Any]] = []
        self._agent_skill_instruction = (
            agent_skill_instruction or self._DEFAULT_AGENT_SKILL_INSTRUCTION
        )
        self._agent_skill_template = (
            agent_skill_template or self._DEFAULT_AGENT_SKILL_TEMPLATE
        )
        self._async_tasks: dict[str, asyncio.Task[Any]] = {}
        self._async_results: dict[str, Any] = {}
        self._async_cancel_requested: set[str] = set()
        self._attribute_dict: dict[str, Any] = {}

    def create_tool_group(
        self,
        group_name: str,
        description: str,
        active: bool = False,
        notes: str | None = None,
        transient: bool = False,
    ) -> None:
        """Create a named group for optionally active tools."""

        if group_name in self.groups or group_name == "basic":
            raise ValueError(
                f"Tool group '{group_name}' is already registered in the "
                "toolkit.",
            )
        self.groups[group_name] = LegacyToolGroup(
            name=group_name,
            description=description,
            active=active,
            notes=notes,
            transient=transient,
        )

    def update_tool_groups(self, group_names: list[str], active: bool) -> None:
        """Activate or deactivate existing non-basic groups."""

        for group_name in group_names:
            if group_name == "basic":
                logger.warning(
                    "The 'basic' tool group is always active, skipping it.",
                )
                continue
            if group_name in self.groups:
                self.groups[group_name].active = active

    def deactivate_transient_groups(self) -> None:
        """Reset per-turn capabilities without changing persistent groups."""

        self.update_tool_groups(
            [name for name, group in self.groups.items() if group.transient],
            active=False,
        )

    def remove_tool_groups(self, group_names: str | list[str]) -> None:
        """Remove tool groups and any tools assigned to them."""

        if isinstance(group_names, str):
            group_names = [group_names]
        if not isinstance(group_names, list) or not all(
            isinstance(item, str) for item in group_names
        ):
            raise TypeError(
                f"The group_names must be a list of strings, "
                f"but got {type(group_names)}.",
            )
        if "basic" in group_names:
            raise ValueError("Cannot remove the default 'basic' tool group.")

        for group_name in group_names:
            self.groups.pop(group_name, None)
        for tool_name in list(self.tools):
            if self.tools[tool_name].group in group_names:
                self.tools.pop(tool_name, None)

    def register_tool_function(
        self,
        tool_func: Callable[..., Any],
        group_name: str = "basic",
        preset_kwargs: dict[str, Any] | None = None,
        func_name: str | None = None,
        func_description: str | None = None,
        json_schema: dict[str, Any] | None = None,
        include_long_description: bool = True,
        include_var_positional: bool = False,
        include_var_keyword: bool = False,
        postprocess_func: Callable[..., Any] | None = None,
        namesake_strategy: str = "raise",
        async_execution: bool = False,
    ) -> None:
        """Register a callable tool on the compatibility toolkit."""

        del include_long_description, include_var_positional, include_var_keyword

        if group_name not in self.groups and group_name != "basic":
            raise ValueError(f"Tool group '{group_name}' not found.")

        original_func = tool_func
        local_preset_kwargs = dict(preset_kwargs or {})
        if isinstance(tool_func, partial):
            original_func = tool_func.func
            local_preset_kwargs = {
                **dict(tool_func.keywords or {}),
                **local_preset_kwargs,
            }

        input_func_name = getattr(original_func, "__name__", None) or repr(
            original_func,
        )
        original_name = input_func_name if func_name else None
        func_name = func_name or input_func_name

        schema = _normalize_tool_json_schema(
            json_schema or getattr(tool_func, "json_schema", None),
            func=original_func,
            func_name=func_name,
            func_description=func_description,
        )
        _strip_preset_kwargs_from_schema(schema, local_preset_kwargs)

        func_obj = LegacyRegisteredToolFunction(
            name=func_name,
            group=group_name,
            source="function",
            original_func=original_func,
            json_schema=schema,
            preset_kwargs=local_preset_kwargs,
            original_name=original_name,
            postprocess_func=postprocess_func,
            async_execution=async_execution,
        )
        self._store_tool_function(func_obj, namesake_strategy=namesake_strategy)

    def _store_tool_function(
        self,
        func_obj: LegacyRegisteredToolFunction,
        *,
        namesake_strategy: str,
    ) -> None:
        if func_obj.name not in self.tools:
            self.tools[func_obj.name] = func_obj
            return

        if namesake_strategy == "raise":
            raise ValueError(
                f"A function with name '{func_obj.name}' is already "
                "registered in the toolkit.",
            )
        if namesake_strategy == "skip":
            logger.warning(
                "A function with name '%s' is already registered in the "
                "toolkit. Skipping registration.",
                func_obj.name,
            )
            return
        if namesake_strategy == "override":
            logger.warning(
                "A function with name '%s' is already registered in the "
                "toolkit. Overriding with the new function.",
                func_obj.name,
            )
            self.tools[func_obj.name] = func_obj
            return
        if namesake_strategy == "rename":
            base_name = func_obj.name
            for _ in range(100):
                candidate = f"{base_name}_{uuid.uuid4().hex[:5]}"
                if candidate not in self.tools:
                    func_obj.original_name = func_obj.original_name or base_name
                    func_obj.name = candidate
                    func_obj.json_schema["function"]["name"] = candidate
                    self.tools[candidate] = func_obj
                    return
            raise RuntimeError(
                f"Failed to register tool function '{base_name}' with a "
                "unique name after 100 attempts.",
            )
        raise ValueError(
            f"Invalid namesake_strategy: {namesake_strategy}. Supported "
            "strategies are 'raise', 'override', 'skip', and 'rename'.",
        )

    def remove_tool_function(
        self,
        tool_name: str,
        allow_not_exist: bool = True,
    ) -> None:
        """Remove a tool by name."""

        if tool_name not in self.tools and not allow_not_exist:
            raise ValueError(
                f"Tool function '{tool_name}' does not exist in the toolkit.",
            )
        self.tools.pop(tool_name, None)

    def get_json_schemas(self) -> list[dict[str, Any]]:
        """Return schemas for basic tools and active tool groups."""

        progressive_tool_name = next(
            (
                name
                for name in ("equip_lantai_tools", "reset_equipped_tools")
                if name in self.tools
            ),
            None,
        )
        if progressive_tool_name is not None:
            fields: dict[str, tuple[type[bool], Any]] = {}
            for group_name, group in self.groups.items():
                if group.transient:
                    tool_names = sorted(
                        tool.name
                        for tool in self.tools.values()
                        if tool.group == group_name
                    )
                    description = group.description
                    if tool_names:
                        description += (
                            " Currently available tools: "
                            + ", ".join(f"`{name}`" for name in tool_names)
                            + "."
                        )
                    else:
                        description += (
                            " No callable tools are currently registered."
                        )
                    fields[group_name] = (
                        bool,
                        Field(default=False, description=description),
                    )
            if fields:
                from pydantic import create_model

                self.set_extended_model(
                    progressive_tool_name,
                    create_model("_DynamicModel", **fields),
                )

        schemas: list[dict[str, Any]] = []
        for tool in self.tools.values():
            if tool.group == "basic":
                schemas.append(tool.extended_json_schema)
            elif self.groups.get(tool.group, LegacyToolGroup("", "")).active:
                schemas.append(tool.extended_json_schema)
        return schemas

    async def call_tool_function(
        self,
        tool_call: dict[str, Any],
    ) -> AsyncGenerator[Any, None]:
        """Execute a tool call and return an async stream of response chunks."""

        async def terminal_handler(**kwargs: Any) -> AsyncGenerator[Any, None]:
            return await self._call_tool_function_without_middleware(
                kwargs["tool_call"],
            )

        handler = terminal_handler
        for middleware in reversed(self._middlewares):
            next_handler = handler

            async def handler(  # type: ignore[no-redef]
                *,
                middleware: Callable[..., Any] = middleware,
                next_handler: Callable[..., Any] = next_handler,
                **kwargs: Any,
            ) -> AsyncGenerator[Any, None]:
                result = middleware(kwargs, next_handler)
                if inspect.isawaitable(result):
                    result = await result
                return result

        return await handler(tool_call=tool_call)

    async def _call_tool_function_without_middleware(
        self,
        tool_call: dict[str, Any],
    ) -> AsyncGenerator[Any, None]:
        tool_name = tool_call.get("name")
        if tool_name not in self.tools:
            return _object_wrapper(
                ToolResponse(
                    content=[
                        TextBlock(
                            type="text",
                            text="FunctionNotFoundError: Cannot find the "
                            f"function named {tool_name}",
                        ),
                    ],
                ),
                None,
            )

        tool_func = self.tools[str(tool_name)]
        if tool_func.group != "basic" and not self.groups[tool_func.group].active:
            return _object_wrapper(
                ToolResponse(
                    content=[
                        TextBlock(
                            type="text",
                            text="FunctionInactiveError: The function "
                            f"'{tool_name}' is in the inactive group "
                            f"'{tool_func.group}'. Activate the tool group "
                            "by calling 'reset_equipped_tools' first to use "
                            "this tool.",
                        ),
                    ],
                ),
                None,
            )

        kwargs = {
            **tool_func.preset_kwargs,
            **(tool_call.get("input", {}) or {}),
        }
        partial_postprocess_func = (
            partial(tool_func.postprocess_func, tool_call)
            if tool_func.postprocess_func
            else None
        )

        if tool_func.async_execution:
            task_id = uuid.uuid4().hex
            execution_context = contextvars.copy_context()
            task = asyncio.create_task(
                self._execute_tool_in_background(
                    task_id=task_id,
                    tool_func=tool_func,
                    kwargs=kwargs,
                    partial_postprocess_func=partial_postprocess_func,
                    execution_context=execution_context,
                ),
            )
            self._async_tasks[task_id] = task
            return _object_wrapper(
                ToolResponse(
                    content=[
                        TextBlock(
                            type="text",
                            text=f"<system-reminder>"
                            f"Tool '{tool_name}' is executing "
                            f"asynchronously. Task ID: {task_id}. "
                            f"Use view_task('{task_id}') to check status, "
                            f"wait_task('{task_id}') to wait for completion, "
                            f"or cancel_task('{task_id}') to cancel the task."
                            f"</system-reminder>",
                        ),
                    ],
                ),
                None,
            )

        res = await self._execute_registered_tool(tool_func, kwargs)
        return _wrap_tool_execution_result(res, partial_postprocess_func)

    async def _execute_registered_tool(
        self,
        tool_func: LegacyRegisteredToolFunction,
        kwargs: dict[str, Any],
        *,
        run_sync_in_thread: bool = False,
        execution_context: contextvars.Context | None = None,
    ) -> Any:
        try:
            if inspect.iscoroutinefunction(tool_func.original_func):
                try:
                    return await tool_func.original_func(**kwargs)
                except asyncio.CancelledError:
                    return ToolResponse(
                        content=[
                            TextBlock(
                                type="text",
                                text="<system-info>"
                                "The tool call has been interrupted by the user."
                                "</system-info>",
                            ),
                        ],
                        stream=True,
                        is_last=True,
                        is_interrupted=True,
                    )
            if run_sync_in_thread:
                context = execution_context or contextvars.copy_context()
                return await asyncio.to_thread(
                    context.run,
                    tool_func.original_func,
                    **kwargs,
                )
            return tool_func.original_func(**kwargs)
        except Exception as exc:  # pylint: disable=broad-except
            return ToolResponse(
                content=[
                    TextBlock(type="text", text=f"Error: {exc}"),
                ],
            )

    async def _execute_tool_in_background(
        self,
        task_id: str,
        tool_func: LegacyRegisteredToolFunction,
        kwargs: dict[str, Any],
        partial_postprocess_func: Callable[..., Any] | None,
        execution_context: contextvars.Context,
    ) -> None:
        """Execute a tool in the background and store its accumulated result."""

        final_result: Any = ToolResponse(content=[])
        try:
            res = await self._execute_registered_tool(
                tool_func,
                kwargs,
                run_sync_in_thread=True,
                execution_context=execution_context,
            )
            final_result = await _accumulate_tool_result(res)
            if partial_postprocess_func:
                processed = await _execute_async_or_sync_func(
                    partial_postprocess_func,
                    final_result,
                )
                if processed is not None:
                    final_result = processed
        except asyncio.CancelledError:
            final_result = ToolResponse(
                content=[
                    TextBlock(
                        type="text",
                        text="<system-info>"
                        "The tool call has been cancelled by the user."
                        "</system-info>",
                    ),
                ],
                is_interrupted=True,
                is_last=True,
            )
        except Exception as exc:  # pylint: disable=broad-except
            final_result = ToolResponse(
                content=[
                    TextBlock(
                        type="text",
                        text=f"Error during async execution: {exc}",
                    ),
                ],
            )
        finally:
            if task_id not in self._async_cancel_requested:
                self._async_results[task_id] = final_result
            self._async_cancel_requested.discard(task_id)
            self._async_tasks.pop(task_id, None)

    async def view_task(self, task_id: str) -> Any:
        """Return current status or finished result for an async tool task."""

        if task_id not in self._async_tasks and task_id not in self._async_results:
            return ToolResponse(
                content=[
                    TextBlock(
                        type="text",
                        text=f"InvalidTaskIdError: Cannot find async task "
                        f"with ID {task_id}.",
                    ),
                ],
            )
        if task_id in self._async_tasks:
            return ToolResponse(
                content=[
                    TextBlock(type="text", text=f"Task {task_id} is still running."),
                ],
            )
        return self._async_results.pop(task_id)

    async def cancel_task(self, task_id: str) -> Any:
        """Cancel an async tool task."""

        if task_id not in self._async_tasks and task_id not in self._async_results:
            return ToolResponse(
                content=[
                    TextBlock(
                        type="text",
                        text=f"InvalidTaskIdError: Cannot find async task "
                        f"with ID {task_id}.",
                    ),
                ],
            )
        if task_id in self._async_results:
            return ToolResponse(
                content=[
                    TextBlock(
                        type="text",
                        text=f"Task {task_id} has already completed and "
                        "cannot be cancelled.",
                    ),
                ],
            )
        task = self._async_tasks.pop(task_id)
        self._async_cancel_requested.add(task_id)
        task.cancel()
        return ToolResponse(
            content=[
                TextBlock(type="text", text=f"Task {task_id} has been cancelled."),
            ],
        )

    async def wait_task(self, task_id: str, timeout: float = 10) -> Any:
        """Wait briefly for an async task to finish."""

        if task_id not in self._async_tasks and task_id not in self._async_results:
            return ToolResponse(
                content=[
                    TextBlock(
                        type="text",
                        text=f"InvalidTaskIdError: Cannot find async task "
                        f"with ID {task_id}.",
                    ),
                ],
            )
        if task_id in self._async_results:
            return self._async_results.pop(task_id)
        task = self._async_tasks[task_id]
        try:
            await asyncio.wait_for(asyncio.shield(task), timeout=timeout)
        except asyncio.TimeoutError:
            return ToolResponse(
                content=[
                    TextBlock(
                        type="text",
                        text=f"Task {task_id} is still running after waiting "
                        f"for {timeout} seconds.",
                    ),
                ],
            )
        return self._async_results.pop(task_id)

    def set_extended_model(
        self,
        func_name: str,
        model: type[BaseModel] | None,
    ) -> None:
        """Attach an extended Pydantic model to a registered tool schema."""

        if model is not None and not issubclass(model, BaseModel):
            raise TypeError(
                "The extended model must be a child class of pydantic "
                f"BaseModel, but got {type(model)}.",
            )
        if func_name not in self.tools:
            raise ValueError(
                f"Tool function '{func_name}' not found in the toolkit.",
            )
        self.tools[func_name].extended_model = model

    def register_middleware(self, middleware: Callable[..., Any]) -> None:
        """Register an onion-style tool-call middleware."""

        self._middlewares.append(middleware)

    async def register_mcp_client(
        self,
        mcp_client: Any,
        group_name: str = "basic",
        namesake_strategy: str = "raise",
        execution_timeout: float | None = None,
        **_kwargs: Any,
    ) -> None:
        """Register MCP tools from clients exposing list/callable methods."""

        if not hasattr(mcp_client, "list_tools") or not hasattr(
            mcp_client,
            "get_callable_function",
        ):
            raise AttributeError(
                "MCP client must provide list_tools() and "
                "get_callable_function().",
            )
        for mcp_tool in await mcp_client.list_tools():
            func_obj = await mcp_client.get_callable_function(
                func_name=mcp_tool.name,
                wrap_tool_result=True,
                execution_timeout=execution_timeout,
            )
            self.register_tool_function(
                tool_func=func_obj,
                group_name=group_name,
                namesake_strategy=namesake_strategy,
            )

    def register_agent_skill(self, skill_dir: str) -> None:
        """Register one skill directory for prompt injection and slash use."""

        import frontmatter

        if not os.path.isdir(skill_dir):
            raise ValueError(
                f"The skill directory '{skill_dir}' does not exist or is "
                "not a directory.",
            )
        skill_md = os.path.join(skill_dir, "SKILL.md")
        if not os.path.isfile(skill_md):
            raise ValueError(
                f"The skill directory '{skill_dir}' must include a SKILL.md "
                "file at the top level.",
            )
        post = frontmatter.load(skill_md)
        name = str(post.get("name") or "").strip()
        description = str(post.get("description") or "").strip()
        if not name or not description:
            raise ValueError(
                f"The SKILL.md file in '{skill_dir}' must have a YAML Front "
                "Matter including `name` and `description` fields.",
            )
        if name in self.skills:
            raise ValueError(
                f"An agent skill with name '{name}' is already registered "
                "in the toolkit.",
            )
        self.skills[name] = {
            "name": name,
            "description": description,
            "dir": skill_dir,
        }
        logger.info("Registered agent skill '%s' from directory '%s'.", name, skill_dir)

    def remove_agent_skill(self, name: str) -> None:
        """Remove a registered skill by name."""

        if name in self.skills:
            self.skills.pop(name)
        else:
            logger.warning(
                "Agent skill '%s' not found in the toolkit, skipping removal.",
                name,
            )

    def get_agent_skill_prompt(self) -> str | None:
        """Return the combined skill prompt, or None when no skills exist."""

        if not self.skills:
            return None
        skill_descriptions = [
            self._agent_skill_instruction,
            *[
                self._agent_skill_template.format(
                    name=skill["name"],
                    description=skill["description"],
                    dir=skill["dir"],
                )
                for skill in self.skills.values()
            ],
        ]
        return "\n".join(skill_descriptions)

    def get_activated_notes(self) -> str:
        """Return notes for active tool groups."""

        collected_notes = []
        for group_name, group in self.groups.items():
            if group.active and group.notes:
                collected_notes.append(
                    "\n".join(
                        [f"## About Tool Group '{group_name}'", group.notes],
                    ),
                )
        return "\n".join(collected_notes)

    def reset_equipped_tools(self, **kwargs: Any) -> Any:
        """Activate/deactivate tool groups and return usage notes."""

        self.update_tool_groups(list(self.groups.keys()), active=False)
        to_activate = []
        for key, value in kwargs.items():
            if not isinstance(value, bool):
                return ToolResponse(
                    content=[
                        TextBlock(
                            type="text",
                            text=f"Invalid arguments: the argument {key} "
                            f"should be a bool value, but got {type(value)}.",
                        ),
                    ],
                )
            if value:
                to_activate.append(key)

        self.update_tool_groups(to_activate, active=True)
        notes = self.get_activated_notes()
        text_response = ""
        if to_activate:
            text_response += (
                "Now tool groups "
                + ", ".join([f"'{item}'" for item in to_activate])
                + " are activated."
            )
        if notes:
            text_response += (
                f" You MUST follow these notes to use these tools:\n"
                f"<notes>{notes}</notes>"
            )
        if not text_response:
            text_response = "All tool groups are now deactivated currently."
        return ToolResponse(content=[TextBlock(type="text", text=text_response)])

    def equip_lantai_tools(self, **kwargs: Any) -> Any:
        """Equip capability groups needed for the current task.

        True fields accumulate across calls in the current user turn. Call
        with no true fields to clear every progressive capability group.
        """

        progressive_groups = {
            name for name, group in self.groups.items() if group.transient
        }
        current_groups = {
            name
            for name in progressive_groups
            if self.groups[name].active
        }
        try:
            transition = transition_progressive_tool_groups(
                current_groups,
                kwargs,
                available_groups=progressive_groups,
            )
        except ValueError as exc:
            return ToolResponse(
                content=[TextBlock(type="text", text=str(exc))],
                is_error=True,
            )
        self.update_tool_groups(list(progressive_groups), active=False)
        self.update_tool_groups(list(transition.active_groups), active=True)
        if transition.cleared:
            text_response = "All progressive tool groups are deactivated."
        else:
            text_response = (
                "Equipped tool groups: "
                + ", ".join(sorted(transition.active_groups))
            )
        notes = self.get_activated_notes()
        if notes:
            text_response += f"\n<notes>{notes}</notes>"
        return ToolResponse(
            content=[TextBlock(type="text", text=text_response)],
            metadata={
                "equipped_tool_groups": sorted(transition.active_groups),
                "progress_made": transition.changed,
            },
        )

    def clear(self) -> None:
        """Remove all registered tools and groups."""

        self.tools.clear()
        self.groups.clear()

    def state_dict(self) -> dict[str, Any]:
        """Return toolkit state used in persisted sessions."""

        return {
            "active_groups": [
                name
                for name, group in self.groups.items()
                if group.active and not group.transient
            ],
        }

    def load_state_dict(
        self,
        state_dict: dict[str, Any],
        strict: bool = True,
    ) -> None:
        """Restore active tool-group state."""

        if (
            not isinstance(state_dict, dict)
            or "active_groups" not in state_dict
            or not isinstance(state_dict["active_groups"], list)
        ):
            raise ValueError(
                "The state_dict for toolkit must be a dictionary with "
                "active_groups key and its value must be a list, "
                f"but got {type(state_dict)}.",
            )
        if strict and list(state_dict.keys()) != ["active_groups"]:
            raise ValueError(
                "Get additional keys in the state_dict: "
                f'{list(state_dict.keys())}, but only "active_groups" '
                "is expected.",
            )
        active_groups = set(state_dict["active_groups"])
        for group_name, group in self.groups.items():
            group.active = (
                group_name in active_groups if not group.transient else False
            )

    def register_state(
        self,
        attr_name: str,
        custom_to_json: Callable[[Any], Any] | None = None,
        custom_from_json: Callable[[Any], Any] | None = None,
    ) -> None:
        """Track a simple serializable attribute for compatibility callers."""

        del custom_to_json, custom_from_json
        if not hasattr(self, attr_name):
            raise AttributeError(attr_name)
        self._attribute_dict[attr_name] = None


def _json_schema_for_annotation(annotation: Any) -> dict[str, Any]:
    if annotation is inspect.Signature.empty:
        return {}
    if annotation is str:
        return {"type": "string"}
    if annotation is int:
        return {"type": "integer"}
    if annotation is float:
        return {"type": "number"}
    if annotation is bool:
        return {"type": "boolean"}
    if annotation is dict:
        return {"type": "object"}
    if annotation is list:
        return {"type": "array"}
    origin = getattr(annotation, "__origin__", None)
    args = getattr(annotation, "__args__", ())
    if origin is list:
        item_schema = _json_schema_for_annotation(args[0]) if args else {}
        return {"type": "array", "items": item_schema or {}}
    if origin is dict:
        return {"type": "object"}
    if origin is not None and str(origin) == "typing.Union":
        non_none = [arg for arg in args if arg is not type(None)]
        if len(non_none) == 1:
            return _json_schema_for_annotation(non_none[0])
    return {}


def _input_schema_from_callable(func: Callable[..., Any]) -> dict[str, Any]:
    try:
        signature = inspect.signature(func)
    except (TypeError, ValueError):
        return {"type": "object", "properties": {}}

    properties: dict[str, Any] = {}
    required: list[str] = []
    for name, parameter in signature.parameters.items():
        if name in {"self", "cls"}:
            continue
        if parameter.kind in {
            inspect.Parameter.VAR_POSITIONAL,
            inspect.Parameter.VAR_KEYWORD,
        }:
            continue
        schema = _json_schema_for_annotation(parameter.annotation)
        if not schema:
            schema = {"type": "string"}
        properties[name] = schema
        if parameter.default is inspect.Signature.empty:
            required.append(name)

    result: dict[str, Any] = {
        "type": "object",
        "properties": properties,
    }
    if required:
        result["required"] = required
    return result


def _normalize_tool_json_schema(
    json_schema: dict[str, Any] | None,
    *,
    func: Callable[..., Any],
    func_name: str,
    func_description: str | None,
) -> dict[str, Any]:
    if isinstance(json_schema, dict):
        schema = deepcopy(json_schema)
        if schema.get("type") == "function" and isinstance(
            schema.get("function"),
            dict,
        ):
            function = schema["function"]
            function["name"] = func_name
            if func_description:
                function["description"] = func_description
            function.setdefault("description", _callable_description(func))
            function.setdefault(
                "parameters",
                {"type": "object", "properties": {}},
            )
            return schema
        parameters = schema
    else:
        parameters = _input_schema_from_callable(func)

    return {
        "type": "function",
        "function": {
            "name": func_name,
            "description": func_description or _callable_description(func),
            "parameters": parameters or {"type": "object", "properties": {}},
        },
    }


def _callable_description(func: Callable[..., Any]) -> str:
    return (getattr(func, "__doc__", "") or "").strip()


def _strip_preset_kwargs_from_schema(
    schema: dict[str, Any],
    preset_kwargs: dict[str, Any],
) -> None:
    if not preset_kwargs:
        return
    parameters = (
        schema.get("function", {})
        .get("parameters", {})
        if isinstance(schema.get("function"), dict)
        else {}
    )
    properties = parameters.get("properties")
    if isinstance(properties, dict):
        for arg_name in preset_kwargs:
            properties.pop(arg_name, None)
    required = parameters.get("required")
    if isinstance(required, list):
        parameters["required"] = [
            arg_name for arg_name in required if arg_name not in preset_kwargs
        ]
        if not parameters["required"]:
            parameters.pop("required", None)


def _looks_like_tool_response(value: Any) -> bool:
    return hasattr(value, "content") and hasattr(value, "is_last")


async def _execute_async_or_sync_func(
    func: Callable[..., Any],
    *args: Any,
    **kwargs: Any,
) -> Any:
    result = func(*args, **kwargs)
    if inspect.isawaitable(result):
        return await result
    return result


async def _postprocess_tool_response(
    obj: Any,
    postprocess_func: Callable[..., Any] | None,
) -> Any:
    if postprocess_func is None:
        return obj
    processed = await _execute_async_or_sync_func(postprocess_func, obj)
    return processed if processed is not None else obj


async def _object_wrapper(
    obj: Any,
    postprocess_func: Callable[..., Any] | None,
) -> AsyncGenerator[Any, None]:
    yield await _postprocess_tool_response(obj, postprocess_func)


async def _async_generator_wrapper(
    async_func: AsyncGenerator[Any, None],
    postprocess_func: Callable[..., Any] | None,
) -> AsyncGenerator[Any, None]:
    last_chunk = None
    try:
        async for chunk in async_func:
            processed_chunk = await _postprocess_tool_response(
                chunk,
                postprocess_func,
            )
            yield processed_chunk
            last_chunk = processed_chunk
    except asyncio.CancelledError:
        interrupted_info = TextBlock(
            type="text",
            text="<system-info>"
            "The tool call has been interrupted by the user."
            "</system-info>",
        )
        if last_chunk and hasattr(last_chunk, "content"):
            last_chunk.content.append(interrupted_info)
            last_chunk.is_interrupted = True
            last_chunk.is_last = True
            yield await _postprocess_tool_response(last_chunk, postprocess_func)
        else:
            yield await _postprocess_tool_response(
                ToolResponse(
                    content=[interrupted_info],
                    is_interrupted=True,
                    is_last=True,
                ),
                postprocess_func,
            )


async def _sync_generator_wrapper(
    sync_generator: Generator[Any, None, None],
    postprocess_func: Callable[..., Any] | None,
) -> AsyncGenerator[Any, None]:
    for chunk in sync_generator:
        yield await _postprocess_tool_response(chunk, postprocess_func)


def _wrap_tool_execution_result(
    res: Any,
    postprocess_func: Callable[..., Any] | None,
) -> AsyncGenerator[Any, None]:
    if inspect.isasyncgen(res):
        return _async_generator_wrapper(res, postprocess_func)
    if inspect.isgenerator(res):
        return _sync_generator_wrapper(res, postprocess_func)
    if _looks_like_tool_response(res):
        return _object_wrapper(res, postprocess_func)
    if inspect.isawaitable(res):
        return _awaitable_tool_result_wrapper(res, postprocess_func)
    raise TypeError(
        "The tool function must return a ToolResponse object, or an "
        "AsyncGenerator/Generator of ToolResponse objects, "
        f"but got {type(res)}.",
    )


async def _awaitable_tool_result_wrapper(
    res: Awaitable[Any],
    postprocess_func: Callable[..., Any] | None,
) -> AsyncGenerator[Any, None]:
    resolved = await res
    async for chunk in _wrap_tool_execution_result(
        resolved,
        postprocess_func,
    ):
        yield chunk


async def _accumulate_tool_result(res: Any) -> Any:
    if inspect.isasyncgen(res):
        accumulated_content: list[Any] = []
        last_chunk = None
        async for chunk in res:
            accumulated_content.extend(getattr(chunk, "content", []) or [])
            last_chunk = chunk
        return ToolResponse(
            content=accumulated_content,
            stream=False,
            is_last=True,
            is_interrupted=(
                bool(getattr(last_chunk, "is_interrupted", False))
                if last_chunk
                else False
            ),
        )
    if inspect.isgenerator(res):
        accumulated_content = []
        last_chunk = None
        for chunk in res:
            accumulated_content.extend(getattr(chunk, "content", []) or [])
            last_chunk = chunk
        return ToolResponse(
            content=accumulated_content,
            stream=False,
            is_last=True,
            is_interrupted=(
                bool(getattr(last_chunk, "is_interrupted", False))
                if last_chunk
                else False
            ),
        )
    if _looks_like_tool_response(res):
        return res
    if inspect.isawaitable(res):
        return await _accumulate_tool_result(await res)
    raise TypeError(
        "The tool function must return a ToolResponse object, or an "
        "AsyncGenerator/Generator of ToolResponse objects, "
        f"but got {type(res)}.",
    )


def legacy_tool_response_type_from_toolkit(toolkit: Any) -> type:
    """Return the response class expected by the adapted legacy toolkit."""

    explicit_response_type = getattr(toolkit, "tool_response_type", None)
    if isinstance(explicit_response_type, type):
        return explicit_response_type

    for attr_name in ("_execute_tool_in_background", "call_tool_function"):
        method = getattr(toolkit, attr_name, None)
        response_type = getattr(method, "__globals__", {}).get("ToolResponse")
        if isinstance(response_type, type):
            return response_type
    return ToolResponse


def legacy_mcp_content_to_blocks(content: Any) -> list[Any]:
    """Convert MCP content into legacy toolkit-compatible blocks."""

    blocks: list[Any] = []
    for item in content or []:
        if isinstance(item, mcp.types.TextContent):
            blocks.append(TextBlock(type="text", text=item.text))
        elif isinstance(item, mcp.types.ImageContent):
            blocks.append(
                ImageBlock(
                    source={
                        "type": "base64",
                        "media_type": getattr(
                            item,
                            "mime_type",
                            getattr(item, "mimeType", ""),
                        ),
                        "data": item.data,
                    },
                ),
            )
        elif isinstance(item, mcp.types.AudioContent):
            blocks.append(
                AudioBlock(
                    source={
                        "type": "base64",
                        "media_type": getattr(
                            item,
                            "mime_type",
                            getattr(item, "mimeType", ""),
                        ),
                        "data": item.data,
                    },
                ),
            )
        elif isinstance(item, mcp.types.EmbeddedResource):
            resource = item.resource
            if isinstance(resource, mcp.types.TextResourceContents):
                blocks.append(
                    TextBlock(
                        type="text",
                        text=resource.model_dump_json(indent=2),
                    ),
                )
            else:
                logger.error(
                    "Unsupported EmbeddedResource content type: %s. "
                    "Skipping this content.",
                    type(resource),
                )
        else:
            logger.warning(
                "Unsupported MCP content type: %s. Skipping this content.",
                type(item),
            )
    return blocks


__all__ = [
    "LegacyToolkit",
    "LegacyRegisteredToolFunction",
    "LegacyToolGroup",
    "legacy_tool_response_type_from_toolkit",
    "legacy_mcp_content_to_blocks",
]
