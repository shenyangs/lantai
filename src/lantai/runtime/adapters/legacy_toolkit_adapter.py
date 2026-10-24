# -*- coding: utf-8 -*-
"""Adapter projecting LantaiToolRegistry onto the legacy toolkit."""

from __future__ import annotations

import inspect
from collections.abc import AsyncGenerator, Awaitable, Generator
from functools import wraps
from typing import Any, Callable

from .legacy_toolkit_components import (
    legacy_tool_response_type_from_toolkit,
    legacy_mcp_content_to_blocks,
)
from ..tool_response import ToolResponse as LantaiToolResponse
from ..tools import LantaiToolRegistry, McpClientSpec, SkillSpec


class LegacyToolkitAdapter:
    """Register runtime-owned tool specs on a legacy toolkit."""

    def __init__(
        self,
        toolkit: Any,
        *,
        namesake_strategy: str = "skip",
    ) -> None:
        self._toolkit = toolkit
        self._namesake_strategy = namesake_strategy
        self._tool_response_type = legacy_tool_response_type_from_toolkit(
            toolkit,
        )

    @property
    def toolkit(self) -> Any:
        """Return the wrapped legacy toolkit."""

        return self._toolkit

    def register_tools(
        self,
        registry: LantaiToolRegistry,
        *,
        group_name_by_tool: Callable[[str], str] | None = None,
    ) -> None:
        """Register enabled tool executors and task helpers."""

        for spec, executor in registry.tool_items():
            register_kwargs: dict[str, Any] = {
                "namesake_strategy": self._namesake_strategy,
                "async_execution": spec.async_execution,
                "func_name": spec.name,
                "func_description": spec.description,
                "json_schema": {
                    "type": "function",
                    "function": {
                        "name": spec.name,
                        "description": spec.description,
                        "parameters": spec.input_schema,
                    },
                },
            }
            if group_name_by_tool is not None:
                register_kwargs["group_name"] = group_name_by_tool(spec.name)
            self._toolkit.register_tool_function(
                _wrap_tool_executor_for_legacy(
                    executor,
                    tool_response_type=self._tool_response_type,
                ),
                **register_kwargs,
            )

        if registry.has_async_tools():
            self.register_task_helpers()

    def register_task_helpers(self) -> None:
        """Register legacy async task management helpers."""

        self._toolkit.register_tool_function(
            self._toolkit.view_task,
            namesake_strategy=self._namesake_strategy,
        )
        self._toolkit.register_tool_function(
            self._toolkit.wait_task,
            namesake_strategy=self._namesake_strategy,
        )
        self._toolkit.register_tool_function(
            self._toolkit.cancel_task,
            namesake_strategy=self._namesake_strategy,
        )

    def register_skills(self, registry: LantaiToolRegistry) -> None:
        """Register enabled workspace skill directories."""

        for spec in registry.skill_specs():
            self.register_skill(spec)

    def register_skill(self, spec: SkillSpec) -> None:
        """Register one workspace skill directory."""

        self._toolkit.register_agent_skill(spec.path)

    async def register_mcp_client(
        self,
        spec: McpClientSpec,
        *,
        group_name: str = "basic",
    ) -> None:
        """Register one MCP client on the legacy toolkit."""

        if not hasattr(spec.client, "list_tools") and hasattr(
            self._toolkit,
            "register_mcp_client",
        ):
            await self._toolkit.register_mcp_client(
                spec.client,
                group_name=group_name,
                namesake_strategy=self._namesake_strategy,
                execution_timeout=spec.execution_timeout,
            )
            return

        raw_tools = await spec.client.list_tools()
        for raw_tool in raw_tools or []:
            tool_name = str(getattr(raw_tool, "name", "") or "")
            if not tool_name:
                continue
            tool_description = str(getattr(raw_tool, "description", "") or "")
            func = await spec.client.get_callable_function(
                func_name=tool_name,
                wrap_tool_result=False,
                execution_timeout=spec.execution_timeout,
            )
            json_schema = getattr(func, "json_schema", None)
            if not isinstance(json_schema, dict):
                json_schema = _json_schema_from_mcp_tool(raw_tool)
            legacy_func = _wrap_mcp_callable_for_legacy(
                func,
                tool_name=tool_name,
                description=tool_description,
                tool_response_type=self._tool_response_type,
            )
            self._toolkit.register_tool_function(
                legacy_func,
                group_name=group_name,
                func_name=tool_name,
                func_description=tool_description,
                json_schema=json_schema,
                namesake_strategy=self._namesake_strategy,
            )


def _mcp_tool_input_schema(tool: Any) -> dict[str, Any]:
    schema = getattr(tool, "inputSchema", None)
    if schema is None:
        schema = getattr(tool, "input_schema", None)
    if isinstance(schema, dict):
        return schema
    return {"type": "object", "properties": {}}


def _json_schema_from_mcp_tool(tool: Any) -> dict[str, Any]:
    return {
        "type": "function",
        "function": {
            "name": str(getattr(tool, "name", "") or ""),
            "description": str(getattr(tool, "description", "") or ""),
            "parameters": _mcp_tool_input_schema(tool),
        },
    }


def _wrap_mcp_callable_for_legacy(
    func: Any,
    *,
    tool_name: str,
    description: str,
    tool_response_type: type,
):
    async def call_mcp_tool(**kwargs: Any):
        result = await func(**kwargs)
        if hasattr(result, "content"):
            content = legacy_mcp_content_to_blocks(result.content)
            return tool_response_type(
                content=content,
                metadata=getattr(result, "meta", None),
            )
        return result

    call_mcp_tool.__name__ = tool_name
    call_mcp_tool.__doc__ = description
    return call_mcp_tool


def _copy_signature(wrapper: Any, wrapped: Any) -> Any:
    try:
        wrapper.__signature__ = inspect.signature(wrapped)
    except (TypeError, ValueError):
        pass
    return wrapper


def _wrap_tool_executor_for_legacy(
    executor: Any,
    *,
    tool_response_type: type,
) -> Any:
    """Convert Lantai-owned tool responses before legacy Toolkit sees them."""

    if inspect.isasyncgenfunction(executor):

        @wraps(executor)
        async def asyncgen_wrapper(*args: Any, **kwargs: Any):
            async for chunk in executor(*args, **kwargs):
                yield _to_legacy_tool_response(
                    chunk,
                    tool_response_type=tool_response_type,
                )

        return _copy_signature(asyncgen_wrapper, executor)

    if inspect.iscoroutinefunction(executor):

        @wraps(executor)
        async def async_wrapper(*args: Any, **kwargs: Any):
            result = await executor(*args, **kwargs)
            return _convert_tool_return_for_legacy(
                result,
                tool_response_type=tool_response_type,
            )

        return _copy_signature(async_wrapper, executor)

    @wraps(executor)
    def sync_wrapper(*args: Any, **kwargs: Any):
        result = executor(*args, **kwargs)
        return _convert_tool_return_for_legacy(
            result,
            tool_response_type=tool_response_type,
        )

    return _copy_signature(sync_wrapper, executor)


def _convert_tool_return_for_legacy(
    result: Any,
    *,
    tool_response_type: type,
) -> Any:
    if isinstance(result, LantaiToolResponse):
        return _to_legacy_tool_response(
            result,
            tool_response_type=tool_response_type,
        )

    if inspect.isasyncgen(result):
        return _convert_async_tool_stream(
            result,
            tool_response_type=tool_response_type,
        )

    if inspect.isgenerator(result):
        return _convert_sync_tool_stream(
            result,
            tool_response_type=tool_response_type,
        )

    if inspect.isawaitable(result):
        return _convert_awaitable_tool_result(
            result,
            tool_response_type=tool_response_type,
        )

    return result


async def _convert_awaitable_tool_result(
    result: Awaitable[Any],
    *,
    tool_response_type: type,
) -> Any:
    resolved = await result
    return _convert_tool_return_for_legacy(
        resolved,
        tool_response_type=tool_response_type,
    )


async def _convert_async_tool_stream(
    result: AsyncGenerator[Any, None],
    *,
    tool_response_type: type,
) -> AsyncGenerator[Any, None]:
    async for chunk in result:
        yield _to_legacy_tool_response(
            chunk,
            tool_response_type=tool_response_type,
        )


def _convert_sync_tool_stream(
    result: Generator[Any, None, None],
    *,
    tool_response_type: type,
):
    for chunk in result:
        yield _to_legacy_tool_response(
            chunk,
            tool_response_type=tool_response_type,
        )


def _to_legacy_tool_response(
    response: Any,
    *,
    tool_response_type: type,
) -> Any:
    if isinstance(response, tool_response_type):
        return response

    if isinstance(response, LantaiToolResponse):
        return tool_response_type(
            content=list(response.content),
            metadata=response.metadata,
            stream=response.stream,
            is_last=response.is_last,
            is_interrupted=response.is_interrupted,
            id=response.id,
        )

    return response
