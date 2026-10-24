"""Regression locks for workspace service factory defaults."""

from __future__ import annotations

from types import SimpleNamespace

import pytest


@pytest.mark.asyncio
async def test_missing_agent_channels_still_creates_console_channel_manager(
    monkeypatch,
    tmp_path,
) -> None:
    """Old agent profiles without channels must keep desktop chat available."""

    from lantai.app.channels import manager as manager_module
    from lantai.app.channels import utils as channel_utils
    from lantai.app.workspace.service_factories import create_channel_service

    class FakeChannelManager:
        def __init__(self) -> None:
            self.workspace = None

        @classmethod
        def from_config(cls, **kwargs):
            instance = cls()
            instance.config = kwargs["config"]
            return instance

        def set_workspace(self, workspace) -> None:
            self.workspace = workspace

    class FakeRunner:
        def __init__(self) -> None:
            self.workspace = None

        def set_workspace(self, workspace) -> None:
            self.workspace = workspace

    monkeypatch.setattr(manager_module, "ChannelManager", FakeChannelManager)
    monkeypatch.setattr(
        channel_utils,
        "make_process_from_runner",
        lambda _runner: object(),
    )
    runner = FakeRunner()
    workspace = SimpleNamespace(
        _config=SimpleNamespace(channels=None),
        _service_manager=SimpleNamespace(services={"runner": runner}),
        agent_id="default",
        workspace_dir=tmp_path,
    )

    channel_manager = await create_channel_service(workspace, None)

    assert channel_manager is not None
    assert channel_manager.config.channels.console.enabled is True
    assert workspace._service_manager.services["channel_manager"] is channel_manager
    assert channel_manager.workspace is workspace
    assert runner.workspace is workspace
