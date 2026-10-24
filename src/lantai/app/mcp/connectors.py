# -*- coding: utf-8 -*-
"""Deep Connector adapters for MCP call preparation and finalization.

Transport lifecycle remains generic in ``stateful_client``. Product-specific
argument repair, transactional writes, result compaction, and cleanup live
behind this seam.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
import logging
from pathlib import Path
import re
from typing import Any

import anyio

from .chrome_devtools import CHROME_DEVTOOLS_PATH_FIELDS

logger = logging.getLogger(__name__)

_OFFICECLI_REQUEST_TIMEOUT_SECONDS = 300.0
_OFFICECLI_CLOSE_TIMEOUT_SECONDS = 60.0
_OFFICECLI_CANCEL_DRAIN_SECONDS = 5.0
_OFFICECLI_PENDING_REQUESTS: set[asyncio.Task] = set()


def _finish_officecli_request(task: asyncio.Task) -> None:
    _OFFICECLI_PENDING_REQUESTS.discard(task)
    if not task.cancelled():
        task.exception()


@dataclass
class PreparedConnectorCall:
    arguments: dict[str, Any]
    read_timeout_seconds: float | None = None
    state: Any = None
    notices: tuple[Any, ...] = field(default_factory=tuple)


class ConnectorAdapter:
    """Default no-op Adapter for an ordinary MCP connector."""

    def prepare(
        self,
        name: str,
        arguments: dict[str, Any],
        *,
        input_schema: dict[str, Any],
        read_timeout_seconds: float | None,
    ) -> PreparedConnectorCall:
        _ = name, input_schema
        return PreparedConnectorCall(
            arguments=dict(arguments),
            read_timeout_seconds=read_timeout_seconds,
        )

    async def execute(
        self,
        protocol: Any,
        name: str,
        prepared: PreparedConnectorCall,
    ) -> Any:
        if prepared.read_timeout_seconds is None:
            return await protocol.call_tool(name, prepared.arguments)
        return await protocol.call_tool(
            name,
            arguments=prepared.arguments,
            read_timeout_seconds=prepared.read_timeout_seconds,
        )

    async def finalize(
        self,
        session: Any,
        name: str,
        result: Any,
        prepared: PreparedConnectorCall,
    ) -> Any:
        _ = session, name, prepared
        return result

    def cleanup(self, prepared: PreparedConnectorCall) -> None:
        _ = prepared

    async def settle(
        self,
        session: Any,
        name: str,
        prepared: PreparedConnectorCall,
    ) -> None:
        """Finish call-owned resources, including interrupted tool calls."""
        _ = session, name
        self.cleanup(prepared)


class GuanlanConnectorAdapter(ConnectorAdapter):
    """Official Guanlan read-oriented integration policy."""

    def prepare(
        self,
        name: str,
        arguments: dict[str, Any],
        *,
        input_schema: dict[str, Any],
        read_timeout_seconds: float | None,
    ) -> PreparedConnectorCall:
        from .guanlan import repair_guanlan_tool_arguments
        from .guanlan_native import prepare_guanlan_tool_call

        product_call = prepare_guanlan_tool_call(
            name,
            dict(arguments),
            input_schema=input_schema,
        )
        repaired, repairs = repair_guanlan_tool_arguments(
            name,
            product_call.arguments,
            input_schema,
        )
        return PreparedConnectorCall(
            arguments=repaired,
            read_timeout_seconds=float(product_call.timeout_seconds),
            notices=tuple(repairs),
        )

    async def finalize(
        self,
        session: Any,
        name: str,
        result: Any,
        prepared: PreparedConnectorCall,
    ) -> Any:
        from mcp.types import TextContent  # pylint: disable=no-name-in-module

        from .guanlan import format_guanlan_argument_repair_notice
        from .guanlan_native import compact_guanlan_mcp_result

        _ = session
        result = compact_guanlan_mcp_result(
            name,
            result,
            prepared.arguments,
        )
        if prepared.notices and isinstance(
            getattr(result, "content", None),
            list,
        ):
            result.content.insert(
                0,
                TextContent(
                    type="text",
                    text=format_guanlan_argument_repair_notice(
                        name,
                        prepared.notices,
                    ),
                ),
            )
        return result


@dataclass
class _OfficeCLICallState:
    transaction: Any
    operation_started: bool = False
    operation_outcome_known: bool = True
    retain_working_copy: bool = False
    resident_closed: bool = False
    close_attempted: bool = False
    close_error: str | None = None


async def _wait_officecli_request(
    task: asyncio.Task,
    deadline: float,
) -> asyncio.CancelledError | None:
    """Observe caller cancellation without abandoning an owned request."""
    cancelled = None
    while not task.done():
        remaining = deadline - asyncio.get_running_loop().time()
        if remaining <= 0:
            break
        try:
            await asyncio.wait({task}, timeout=remaining)
        except asyncio.CancelledError as exc:
            cancelled = exc
    return cancelled


class OfficeCLIConnectorAdapter(ConnectorAdapter):
    """Official OfficeCLI transactional write integration policy."""

    def prepare(
        self,
        name: str,
        arguments: dict[str, Any],
        *,
        input_schema: dict[str, Any],
        read_timeout_seconds: float | None,
    ) -> PreparedConnectorCall:
        from ..officecli_safe_bridge import prepare_officecli_mcp_call

        _ = input_schema
        transaction = (
            prepare_officecli_mcp_call(arguments)
            if name == "officecli"
            else None
        )
        return PreparedConnectorCall(
            arguments=(
                transaction.arguments if transaction else dict(arguments)
            ),
            read_timeout_seconds=read_timeout_seconds,
            state=_OfficeCLICallState(transaction) if transaction else None,
        )

    async def _await_owned_request(
        self,
        request: Any,
        state: _OfficeCLICallState,
        *,
        timeout: float,
        operation: bool = False,
    ) -> Any:
        """Keep the RPC waiter alive until a response or a bounded failure."""
        cancelled = None
        error = None
        result = None
        with anyio.CancelScope(shield=True):
            task = asyncio.create_task(request)
            _OFFICECLI_PENDING_REQUESTS.add(task)
            task.add_done_callback(_finish_officecli_request)
            deadline = asyncio.get_running_loop().time() + timeout
            cancelled = await _wait_officecli_request(task, deadline)
            if not task.done():
                if operation:
                    # A late reply can follow the cleanup close and reopen the
                    # resident. That earlier close cannot authorize deletion.
                    state.retain_working_copy = True
                    state.operation_outcome_known = False
                task.cancel()
                drain_deadline = (
                    asyncio.get_running_loop().time()
                    + _OFFICECLI_CANCEL_DRAIN_SECONDS
                )
                cancelled = (
                    await _wait_officecli_request(task, drain_deadline)
                ) or cancelled
                error = TimeoutError(
                    "OfficeCLI request timed out; working copy retained at "
                    f"{state.transaction.working_path}",
                )
            else:
                try:
                    result = task.result()
                except BaseException as exc:
                    error = exc
        if cancelled is not None:
            if error is not None:
                logger.error(
                    "OfficeCLI cleanup failed during cancellation; "
                    "working copy "
                    "retained at %s: %s",
                    state.transaction.working_path,
                    error,
                )
            raise cancelled from error
        # A cancelled AnyIO scope can defer cancellation until the next
        # checkpoint. It must be observed before synchronous atomic commit.
        await anyio.lowlevel.checkpoint_if_cancelled()
        if error is not None:
            raise error
        return result

    async def _call_operation(
        self,
        protocol: Any,
        name: str,
        arguments: dict[str, Any],
        prepared: PreparedConnectorCall,
    ) -> Any:
        state = prepared.state
        await anyio.lowlevel.checkpoint()
        state.operation_started = True
        state.operation_outcome_known = False

        async def request() -> Any:
            if prepared.read_timeout_seconds is None:
                result = await protocol.call_tool(name, arguments)
            else:
                result = await protocol.call_tool(
                    name,
                    arguments=arguments,
                    read_timeout_seconds=prepared.read_timeout_seconds,
                )
            if not state.retain_working_copy:
                state.operation_outcome_known = True
            return result

        try:
            return await self._await_owned_request(
                request(),
                state,
                timeout=(
                    prepared.read_timeout_seconds
                    if prepared.read_timeout_seconds is not None
                    else _OFFICECLI_REQUEST_TIMEOUT_SECONDS
                ),
                operation=True,
            )
        except BaseException:
            if not state.operation_outcome_known:
                state.retain_working_copy = True
            raise

    async def execute(
        self,
        protocol: Any,
        name: str,
        prepared: PreparedConnectorCall,
    ) -> Any:
        if prepared.state is None:
            return await super().execute(protocol, name, prepared)
        return await self._call_operation(
            protocol,
            name,
            prepared.arguments,
            prepared,
        )

    async def _close_working_copy(
        self,
        session: Any,
        name: str,
        state: _OfficeCLICallState,
    ) -> None:
        state.close_attempted = True
        state.resident_closed = False

        async def close() -> None:
            try:
                result = await session.call_tool(
                    name,
                    {
                        "command": [
                            "close",
                            str(state.transaction.working_path),
                        ],
                    },
                    read_timeout_seconds=_OFFICECLI_CLOSE_TIMEOUT_SECONDS,
                )
                if _is_error(result):
                    raise RuntimeError(
                        "OfficeCLI could not close the working copy; "
                        "retained at "
                        f"{state.transaction.working_path}",
                    )
                state.resident_closed = True
            except BaseException as exc:
                state.close_error = str(exc) or type(exc).__name__
                raise

        try:
            await self._await_owned_request(
                close(),
                state,
                timeout=_OFFICECLI_CLOSE_TIMEOUT_SECONDS,
            )
        except BaseException as exc:
            if not state.resident_closed:
                state.close_error = (
                    state.close_error or str(exc) or type(exc).__name__
                )
                logger.error(
                    "OfficeCLI close failed; working copy retained at %s: %s",
                    state.transaction.working_path,
                    state.close_error,
                )
            raise

    async def finalize(
        self,
        session: Any,
        name: str,
        result: Any,
        prepared: PreparedConnectorCall,
    ) -> Any:
        state = prepared.state
        if state is None or _is_error(result):
            return result
        transaction = state.transaction
        await self._close_working_copy(session, name, state)
        # Validation can reopen a native resident, even when it fails.
        state.resident_closed = False
        state.close_attempted = False
        try:
            validation = await self._call_operation(
                session,
                name,
                {
                    "command": [
                        "validate",
                        str(transaction.working_path),
                        "--json",
                    ],
                },
                prepared,
            )
            if _is_error(validation):
                raise RuntimeError("OfficeCLI OpenXML validation failed")
        except BaseException:
            try:
                await self._close_working_copy(session, name, state)
            except Exception:
                logger.exception(
                    "OfficeCLI validation cleanup failed; "
                    "working copy retained at %s",
                    transaction.working_path,
                )
            raise
        await self._close_working_copy(session, name, state)

        from mcp.types import TextContent  # pylint: disable=no-name-in-module

        from ..officecli_safe_bridge import commit_officecli_transaction

        receipt = commit_officecli_transaction(transaction)
        if isinstance(getattr(result, "content", None), list):
            result.content.append(
                TextContent(type="text", text=receipt.format_for_tool()),
            )
        return result

    def cleanup(self, prepared: PreparedConnectorCall) -> None:
        state = prepared.state
        if (
            state is None
            or not state.resident_closed
            or not state.operation_outcome_known
            or state.retain_working_copy
        ):
            return
        from ..officecli_safe_bridge import cleanup_officecli_transaction

        cleanup_officecli_transaction(state.transaction)

    async def settle(
        self,
        session: Any,
        name: str,
        prepared: PreparedConnectorCall,
    ) -> None:
        state = prepared.state
        if state is None:
            return
        if not state.operation_started:
            state.resident_closed = True
            self.cleanup(prepared)
            return
        try:
            if not state.resident_closed and not state.close_attempted:
                await self._close_working_copy(session, name, state)
        finally:
            if not state.operation_outcome_known:
                logger.error(
                    "OfficeCLI operation outcome is unknown; "
                    "working copy retained at %s",
                    state.transaction.working_path,
                )
            self.cleanup(prepared)


class OpenChatCutConnectorAdapter(ConnectorAdapter):
    """Keep the experimental video connector behind human review."""

    def prepare(
        self,
        name: str,
        arguments: dict[str, Any],
        *,
        input_schema: dict[str, Any],
        read_timeout_seconds: float | None,
    ) -> PreparedConnectorCall:
        _ = input_schema
        prepared_arguments = dict(arguments)
        if name == "begin_edit_session":
            requested_mode = str(
                prepared_arguments.get("approvalMode") or "manual",
            ).strip()
            if requested_mode != "manual":
                raise PermissionError(
                    "OpenChatCut sessions require manual review in Lantai",
                )
            prepared_arguments["approvalMode"] = "manual"
        return PreparedConnectorCall(
            arguments=prepared_arguments,
            read_timeout_seconds=read_timeout_seconds,
        )


_PATHISH_KEY = re.compile(r"(?:^|_)(?:file|path)|(?:file|path)(?:$|_)", re.I)
_URL_KEY = re.compile(r"(?:^|_)(?:url|uri)|(?:url|uri)(?:$|_)", re.I)


def _walk_argument_fields(
    value: Any,
    *,
    prefix: tuple[str, ...] = (),
):
    if isinstance(value, dict):
        for key, item in value.items():
            field_path = (*prefix, str(key))
            yield field_path, item
            yield from _walk_argument_fields(item, prefix=field_path)
    elif isinstance(value, list):
        for index, item in enumerate(value):
            yield from _walk_argument_fields(
                item,
                prefix=(*prefix, str(index)),
            )


def _require_plain_path(value: Any, *, field_name: str) -> str:
    if not isinstance(value, str):
        raise PermissionError(f"Chrome DevTools {field_name} must be a string")
    path = value.strip()
    if not path or "\x00" in path:
        raise PermissionError(f"Chrome DevTools {field_name} is invalid")
    lowered = path.casefold()
    if lowered.startswith("file:") or "://" in lowered:
        raise PermissionError(
            f"Chrome DevTools {field_name} must be a local path, not a URL",
        )
    return path


def _contained_path(root: Path, value: str, *, field_name: str) -> Path:
    raw = Path(value).expanduser()
    candidate = raw if raw.is_absolute() else root / raw
    resolved = candidate.resolve(strict=False)
    if resolved != root and root not in resolved.parents:
        raise PermissionError(
            f"Chrome DevTools {field_name} escapes the active workspace",
        )
    return resolved


def _resolve_chrome_devtools_path(
    value: Any,
    *,
    field_name: str,
    mode: str,
    root: Path,
    access_mode: str,
    workspace_grant_id: str,
) -> str:
    path = _require_plain_path(value, field_name=field_name)
    resolved = _contained_path(root, path, field_name=field_name)

    if mode.startswith("write_"):
        if access_mode != "read_write":
            raise PermissionError(
                "Chrome DevTools cannot write files in a read-only workspace",
            )
        parent = resolved if mode == "write_dir" else resolved.parent
        parent_resolved = parent.resolve(strict=False)
        if parent_resolved != root and root not in parent_resolved.parents:
            raise PermissionError(
                f"Chrome DevTools {field_name} parent escapes the workspace",
            )
        if resolved.is_symlink() or parent.is_symlink():
            raise PermissionError(
                f"Chrome DevTools {field_name} cannot target a symlink",
            )
        return str(resolved)

    if resolved.is_symlink():
        raise PermissionError(
            f"Chrome DevTools {field_name} cannot use a symlink",
        )
    try:
        strict = resolved.resolve(strict=True)
    except OSError as exc:
        raise PermissionError(
            f"Chrome DevTools {field_name} does not exist",
        ) from exc
    if strict != root and root not in strict.parents:
        raise PermissionError(
            f"Chrome DevTools {field_name} resolves outside the workspace",
        )
    if mode in {"read_file", "upload_files"} and not strict.is_file():
        raise PermissionError(
            f"Chrome DevTools {field_name} must reference a regular file",
        )
    if mode == "read_dir" and not strict.is_dir():
        raise PermissionError(
            f"Chrome DevTools {field_name} must reference a directory",
        )
    if mode == "upload_files" and not workspace_grant_id:
        raise PermissionError(
            "Chrome DevTools uploads require a host-issued workspace grant",
        )
    return str(strict)


class ChromeDevToolsConnectorAdapter(ConnectorAdapter):
    """Enforce Lantai roots before the full-capability upstream call."""

    def prepare(
        self,
        name: str,
        arguments: dict[str, Any],
        *,
        input_schema: dict[str, Any],
        read_timeout_seconds: float | None,
    ) -> PreparedConnectorCall:
        from ...config.context import get_current_workspace_context

        _ = input_schema
        prepared_arguments = dict(arguments)
        allowed_fields = CHROME_DEVTOOLS_PATH_FIELDS.get(name, {})
        for field_path, value in _walk_argument_fields(prepared_arguments):
            field_name = field_path[-1]
            if field_name.casefold() in {
                "file_grant_id",
                "grant_id",
                "workspace_grant_id",
            }:
                raise PermissionError(
                    "Chrome DevTools grants cannot be supplied by tool "
                    "arguments",
                )
            if _URL_KEY.search(field_name) and isinstance(value, str):
                if value.strip().casefold().startswith("file:"):
                    raise PermissionError(
                        "Chrome DevTools file URLs are not allowed",
                    )
            if not _PATHISH_KEY.search(field_name):
                continue
            if len(field_path) != 1 or field_name not in allowed_fields:
                joined = ".".join(field_path)
                raise PermissionError(
                    f"Chrome DevTools path field is not trusted: {joined}",
                )

        populated_fields = [
            field_name
            for field_name in allowed_fields
            if prepared_arguments.get(field_name) not in (None, "", [])
        ]
        if not populated_fields:
            return PreparedConnectorCall(
                arguments=prepared_arguments,
                read_timeout_seconds=read_timeout_seconds,
            )

        context = get_current_workspace_context()
        if context is None:
            raise PermissionError(
                "Chrome DevTools file access requires an active workspace",
            )
        root = context.project_root.expanduser().resolve(strict=True)
        workspace_grant_id = str(context.workspace_grant_id or "").strip()
        for field_name in populated_fields:
            mode = allowed_fields[field_name]
            raw_value = prepared_arguments[field_name]
            if mode == "upload_files":
                if not isinstance(raw_value, list) or not raw_value:
                    raise PermissionError(
                        "Chrome DevTools filePaths must be a non-empty array",
                    )
                prepared_arguments[field_name] = [
                    _resolve_chrome_devtools_path(
                        item,
                        field_name=f"{field_name}[{index}]",
                        mode=mode,
                        root=root,
                        access_mode=context.access_mode,
                        workspace_grant_id=workspace_grant_id,
                    )
                    for index, item in enumerate(raw_value)
                ]
                continue
            prepared_arguments[field_name] = _resolve_chrome_devtools_path(
                raw_value,
                field_name=field_name,
                mode=mode,
                root=root,
                access_mode=context.access_mode,
                workspace_grant_id=workspace_grant_id,
            )
        return PreparedConnectorCall(
            arguments=prepared_arguments,
            read_timeout_seconds=read_timeout_seconds,
        )


_DEFAULT_ADAPTER = ConnectorAdapter()
_CONNECTOR_ADAPTERS = {
    "chrome-devtools": ChromeDevToolsConnectorAdapter(),
    "guanlan": GuanlanConnectorAdapter(),
    "officecli": OfficeCLIConnectorAdapter(),
    "openchatcut": OpenChatCutConnectorAdapter(),
}


def _is_error(result: Any) -> bool:
    return bool(
        getattr(result, "is_error", False)
        or getattr(result, "isError", False),
    )


def connector_adapter_for_key(key: str | None) -> ConnectorAdapter:
    return _CONNECTOR_ADAPTERS.get(str(key or "").strip(), _DEFAULT_ADAPTER)


__all__ = [
    "ConnectorAdapter",
    "ChromeDevToolsConnectorAdapter",
    "GuanlanConnectorAdapter",
    "OfficeCLIConnectorAdapter",
    "OpenChatCutConnectorAdapter",
    "PreparedConnectorCall",
    "connector_adapter_for_key",
]
