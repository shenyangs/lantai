# -*- coding: utf-8 -*-
"""Tests for Workspace class."""
import asyncio
import tempfile
import threading
from pathlib import Path
import pytest


@pytest.mark.asyncio
async def test_workspace_start_runs_blocking_bootstrap_off_event_loop(
    monkeypatch,
    tmp_path,
):
    """Synchronous workspace reads must not freeze unrelated HTTP work."""
    from lantai.agents import skills_manager
    from lantai.app.workspace import workspace as workspace_module
    from lantai.app.workspace import Workspace

    main_thread = threading.get_ident()
    worker_threads: list[int] = []

    def record_worker(*_args, **_kwargs):
        worker_threads.append(threading.get_ident())
        return object()

    monkeypatch.setattr(skills_manager, "ensure_skill_pool_initialized", record_worker)
    monkeypatch.setattr(workspace_module, "load_agent_config", record_worker)

    workspace = Workspace("default", str(tmp_path / "workspace"))
    monkeypatch.setattr(workspace, "_migrate_legacy_weixin_data", record_worker)

    async def start_all():
        return None

    async def skip_runtime_recovery():
        return None

    monkeypatch.setattr(workspace._service_manager, "start_all", start_all)
    monkeypatch.setattr(
        workspace,
        "_run_runtime_recovery_loop",
        skip_runtime_recovery,
    )

    task = asyncio.create_task(workspace.start())
    await asyncio.sleep(0)
    await task

    assert len(worker_threads) == 3
    assert all(thread_id != main_thread for thread_id in worker_threads)


@pytest.mark.asyncio
async def test_workspace_creation():
    """Test workspace instance creation."""
    from lantai.app.workspace import Workspace

    with tempfile.TemporaryDirectory() as tmpdir:
        workspace_dir = Path(tmpdir) / "test_agent"
        workspace = Workspace(
            agent_id="test123",
            workspace_dir=str(workspace_dir),
        )

        assert workspace.agent_id == "test123"
        assert workspace.workspace_dir == workspace_dir
        assert workspace_dir.exists()
        assert not workspace._started  # pylint: disable=W0212


@pytest.mark.asyncio
async def test_workspace_components_none_before_start():
    """Test that workspace components are None before start()."""
    from lantai.app.workspace import Workspace

    with tempfile.TemporaryDirectory() as tmpdir:
        workspace_dir = Path(tmpdir) / "test_agent"
        workspace = Workspace(
            agent_id="test123",
            workspace_dir=str(workspace_dir),
        )

        assert workspace.runner is None
        assert workspace.channel_manager is None
        assert workspace.memory_manager is None
        assert workspace.mcp_manager is None
        assert workspace.cron_manager is None
        assert workspace.chat_manager is None


@pytest.mark.asyncio
async def test_workspace_runner_start_service_initializes_session():
    """Lock the service seam used before channel_manager startup."""
    from lantai.app.workspace import Workspace

    with tempfile.TemporaryDirectory() as tmpdir:
        workspace_dir = Path(tmpdir) / "test_agent"
        workspace = Workspace(
            agent_id="test123",
            workspace_dir=str(workspace_dir),
        )
        service_manager = workspace._service_manager  # pylint: disable=W0212

        await service_manager._start_service(  # pylint: disable=W0212
            service_manager.descriptors["runner"],
        )
        await service_manager._start_service(  # pylint: disable=W0212
            service_manager.descriptors["runner_start"],
        )

        assert workspace.runner is not None
        assert workspace.runner.session is not None
        assert workspace.runner.session.save_dir == str(workspace_dir / "sessions")


@pytest.mark.asyncio
async def test_workspace_default_agent():
    """Test workspace with 'default' agent ID."""
    from lantai.app.workspace import Workspace

    with tempfile.TemporaryDirectory() as tmpdir:
        workspace_dir = Path(tmpdir) / "default"
        workspace = Workspace(
            agent_id="default",
            workspace_dir=str(workspace_dir),
        )

        assert workspace.agent_id == "default"
        assert workspace.workspace_dir.name == "default"


@pytest.mark.asyncio
async def test_workspace_short_uuid_agent():
    """Test workspace with short UUID agent ID."""
    from lantai.app.workspace import Workspace
    from lantai.config.config import generate_short_agent_id

    short_id = generate_short_agent_id()

    with tempfile.TemporaryDirectory() as tmpdir:
        workspace_dir = Path(tmpdir) / short_id
        workspace = Workspace(
            agent_id=short_id,
            workspace_dir=str(workspace_dir),
        )

        assert workspace.agent_id == short_id
        assert len(workspace.agent_id) == 6
        assert workspace.workspace_dir.name == short_id


def test_workspace_repr():
    """Test workspace string representation."""
    from lantai.app.workspace import Workspace

    with tempfile.TemporaryDirectory() as tmpdir:
        workspace_dir = Path(tmpdir) / "test_agent"
        workspace = Workspace(
            agent_id="test123",
            workspace_dir=str(workspace_dir),
        )

        repr_str = repr(workspace)
        assert "test123" in repr_str
        assert "stopped" in repr_str
        assert "Workspace" in repr_str
