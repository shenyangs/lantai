"""ACP protocol implementation backed by Lantai's existing runtime."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any
from uuid import uuid4

from acp import (
    PROTOCOL_VERSION,
    Agent,
    InitializeResponse,
    NewSessionResponse,
    PromptResponse,
    RequestError,
)
from acp.interfaces import Client
from acp.schema import (
    AgentCapabilities,
    AgentMessageChunk,
    AudioContentBlock,
    ClientCapabilities,
    EmbeddedResourceContentBlock,
    HttpMcpServer,
    ImageContentBlock,
    Implementation,
    McpServerStdio,
    ResourceContentBlock,
    SseMcpServer,
    TextContentBlock,
)

from ...__version__ import __version__
from .bridge import AcpBridgeError, AcpWorkspaceGrant, LantaiAcpBridge


@dataclass(frozen=True)
class _AcpSession:
    session_id: str
    workspace: AcpWorkspaceGrant


class LantaiAcpAgent(Agent):
    """Expose one honest, text-first ACP surface for IDE clients."""

    def __init__(self, bridge: LantaiAcpBridge) -> None:
        self._bridge = bridge
        self._connection: Client | None = None
        self._sessions: dict[str, _AcpSession] = {}

    def on_connect(self, conn: Client) -> None:
        self._connection = conn

    async def initialize(
        self,
        protocol_version: int,
        client_capabilities: ClientCapabilities | None = None,
        client_info: Implementation | None = None,
        **kwargs: Any,
    ) -> InitializeResponse:
        del protocol_version, client_capabilities, client_info, kwargs
        return InitializeResponse(
            protocol_version=PROTOCOL_VERSION,
            agent_capabilities=AgentCapabilities(),
            agent_info=Implementation(
                name="lantai",
                title="Lantai",
                version=__version__,
            ),
        )

    async def new_session(
        self,
        cwd: str,
        additional_directories: list[str] | None = None,
        mcp_servers: list[
            HttpMcpServer | SseMcpServer | McpServerStdio
        ]
        | None = None,
        **kwargs: Any,
    ) -> NewSessionResponse:
        del kwargs
        if additional_directories:
            raise RequestError(
                -32602,
                "Lantai ACP currently supports one authorized IDE workspace per session.",
            )
        if mcp_servers:
            raise RequestError(
                -32602,
                "Lantai ACP does not advertise client-supplied MCP servers.",
            )
        try:
            workspace = await self._bridge.require_workspace_grant(cwd)
        except AcpBridgeError as exc:
            raise RequestError(-32002, str(exc)) from exc
        session_id = f"acp-{uuid4().hex}"
        self._sessions[session_id] = _AcpSession(
            session_id=session_id,
            workspace=workspace,
        )
        return NewSessionResponse(session_id=session_id)

    async def prompt(
        self,
        session_id: str,
        prompt: list[
            TextContentBlock
            | ImageContentBlock
            | AudioContentBlock
            | ResourceContentBlock
            | EmbeddedResourceContentBlock
        ],
        **kwargs: Any,
    ) -> PromptResponse:
        del kwargs
        session = self._sessions.get(session_id)
        if session is None:
            raise RequestError(-32002, f"Unknown Lantai ACP session: {session_id}")
        if self._connection is None:
            raise RequestError(-32603, "ACP client connection is not ready.")

        text_parts: list[str] = []
        for block in prompt:
            block_type = (
                block.get("type")
                if isinstance(block, dict)
                else getattr(block, "type", None)
            )
            if block_type != "text":
                raise RequestError(
                    -32602,
                    "Lantai ACP currently accepts text prompt blocks only.",
                )
            text = (
                block.get("text")
                if isinstance(block, dict)
                else getattr(block, "text", None)
            )
            if isinstance(text, str) and text.strip():
                text_parts.append(text.strip())
        if not text_parts:
            raise RequestError(-32602, "The ACP prompt contains no text.")

        try:
            result = await self._bridge.run_prompt(
                session_id=session_id,
                text="\n\n".join(text_parts),
                workspace=session.workspace,
            )
        except AcpBridgeError as exc:
            raise RequestError(-32603, str(exc)) from exc

        if result.text:
            await self._connection.session_update(
                session_id,
                AgentMessageChunk(
                    session_update="agent_message_chunk",
                    content=TextContentBlock(type="text", text=result.text),
                ),
            )
        return PromptResponse(stop_reason=result.stop_reason)

    async def cancel(self, session_id: str, **kwargs: Any) -> None:
        del kwargs
        if session_id not in self._sessions:
            return
        try:
            await self._bridge.cancel(session_id)
        except AcpBridgeError as exc:
            raise RequestError(-32603, str(exc)) from exc
