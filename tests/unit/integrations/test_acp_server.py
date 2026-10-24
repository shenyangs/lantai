from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from acp import PROTOCOL_VERSION, RequestError
from acp.schema import TextContentBlock

from lantai.integrations.acp_server.bridge import (
    AcpBridgeError,
    AcpPromptResult,
    AcpWorkspaceGrant,
)
from lantai.integrations.acp_server.server import LantaiAcpAgent


class _FakeBridge:
    def __init__(self, root: Path) -> None:
        self.workspace = AcpWorkspaceGrant(
            grant_id="grant-1",
            project_root=root,
            access_mode="read_write",
            trust_state="trusted",
        )
        self.prompts: list[dict[str, Any]] = []
        self.cancelled: list[str] = []
        self.workspace_error: str | None = None

    async def require_workspace_grant(self, cwd: str) -> AcpWorkspaceGrant:
        if self.workspace_error:
            raise AcpBridgeError(self.workspace_error)
        assert Path(cwd) == self.workspace.project_root
        return self.workspace

    async def run_prompt(self, **kwargs: Any) -> AcpPromptResult:
        self.prompts.append(kwargs)
        return AcpPromptResult(text="Lantai reply", stop_reason="end_turn")

    async def cancel(self, session_id: str) -> bool:
        self.cancelled.append(session_id)
        return True


class _FakeClient:
    def __init__(self) -> None:
        self.updates: list[tuple[str, Any]] = []

    async def session_update(self, session_id: str, update: Any) -> None:
        self.updates.append((session_id, update))


@pytest.mark.asyncio
async def test_agent_advertises_only_the_capabilities_it_implements(
    tmp_path: Path,
) -> None:
    agent = LantaiAcpAgent(_FakeBridge(tmp_path))  # type: ignore[arg-type]

    response = await agent.initialize(protocol_version=1)

    assert response.protocol_version == PROTOCOL_VERSION
    assert response.agent_info is not None
    assert response.agent_info.name == "lantai"
    assert response.agent_capabilities is not None
    assert response.agent_capabilities.load_session is False
    assert response.agent_capabilities.prompt_capabilities is not None
    assert response.agent_capabilities.prompt_capabilities.image is False
    assert response.agent_capabilities.prompt_capabilities.audio is False
    assert response.agent_capabilities.mcp_capabilities is not None
    assert response.agent_capabilities.mcp_capabilities.http is False
    assert response.agent_capabilities.mcp_capabilities.sse is False


@pytest.mark.asyncio
async def test_session_requires_existing_lantai_workspace_authority(
    tmp_path: Path,
) -> None:
    bridge = _FakeBridge(tmp_path)
    bridge.workspace_error = "authorize this folder first"
    agent = LantaiAcpAgent(bridge)  # type: ignore[arg-type]

    with pytest.raises(RequestError, match="authorize this folder first"):
        await agent.new_session(str(tmp_path))


@pytest.mark.asyncio
async def test_prompt_reuses_session_and_emits_one_visible_reply(
    tmp_path: Path,
) -> None:
    bridge = _FakeBridge(tmp_path)
    client = _FakeClient()
    agent = LantaiAcpAgent(bridge)  # type: ignore[arg-type]
    agent.on_connect(client)  # type: ignore[arg-type]
    session = await agent.new_session(str(tmp_path))

    response = await agent.prompt(
        session.session_id,
        [
            TextContentBlock(type="text", text="first"),
            TextContentBlock(type="text", text="second"),
        ],
    )

    assert response.stop_reason == "end_turn"
    assert bridge.prompts == [
        {
            "session_id": session.session_id,
            "text": "first\n\nsecond",
            "workspace": bridge.workspace,
        },
    ]
    assert len(client.updates) == 1
    update_session, update = client.updates[0]
    assert update_session == session.session_id
    assert update.content.text == "Lantai reply"


@pytest.mark.asyncio
async def test_cancel_delegates_to_the_same_session_task(tmp_path: Path) -> None:
    bridge = _FakeBridge(tmp_path)
    agent = LantaiAcpAgent(bridge)  # type: ignore[arg-type]
    session = await agent.new_session(str(tmp_path))

    await agent.cancel(session.session_id)

    assert bridge.cancelled == [session.session_id]


@pytest.mark.asyncio
async def test_unadvertised_prompt_content_is_rejected(tmp_path: Path) -> None:
    bridge = _FakeBridge(tmp_path)
    client = _FakeClient()
    agent = LantaiAcpAgent(bridge)  # type: ignore[arg-type]
    agent.on_connect(client)  # type: ignore[arg-type]
    session = await agent.new_session(str(tmp_path))

    with pytest.raises(RequestError, match="text prompt blocks only"):
        await agent.prompt(
            session.session_id,
            [{"type": "image", "data": "ignored"}],  # type: ignore[list-item]
        )
