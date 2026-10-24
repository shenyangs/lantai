# -*- coding: utf-8 -*-
"""Reliability guards for desktop workspaces outside app-managed storage."""

from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from lantai.app import migration
from lantai.app.agent_context import get_agent_for_request
from lantai.app.multi_agent_manager import MultiAgentManager
from lantai.app.routers import agents as agents_router
from lantai.app.routers import providers as providers_router
from lantai.config import utils as config_utils
from lantai.app.workspace_access import should_defer_desktop_workspace_io
from lantai.config.config import AgentProfileRef, Config
from lantai.exceptions import ConfigurationException


def _external_config(path: Path, *, agent_id: str = "default") -> Config:
    config = Config()
    config.agents.active_agent = agent_id
    config.agents.profiles = {
        agent_id: AgentProfileRef(id=agent_id, workspace_dir=str(path)),
    }
    return config


def test_desktop_defers_only_workspaces_outside_managed_root(
    monkeypatch,
    tmp_path,
):
    managed_root = tmp_path / "working"
    monkeypatch.setenv("LANTAI_DESKTOP_APP", "1")

    assert not should_defer_desktop_workspace_io(
        managed_root / "workspaces" / "default",
        working_dir=managed_root,
    )
    assert should_defer_desktop_workspace_io(
        tmp_path / "external",
        working_dir=managed_root,
    )


def test_cli_does_not_defer_external_workspace(monkeypatch, tmp_path):
    monkeypatch.delenv("LANTAI_DESKTOP_APP", raising=False)

    assert not should_defer_desktop_workspace_io(
        tmp_path / "external",
        working_dir=tmp_path / "working",
    )


def test_desktop_accepts_managed_workspace_through_a_symlinked_root(
    monkeypatch,
    tmp_path,
):
    managed_root = tmp_path / "managed"
    managed_root.mkdir()
    alias_root = tmp_path / "managed-alias"
    alias_root.symlink_to(managed_root, target_is_directory=True)
    monkeypatch.setenv("LANTAI_DESKTOP_APP", "1")

    assert not should_defer_desktop_workspace_io(
        alias_root / "workspaces" / "default",
        working_dir=managed_root,
    )


def test_default_agent_startup_repair_skips_external_desktop_workspace(
    monkeypatch,
    tmp_path,
):
    monkeypatch.setenv("LANTAI_DESKTOP_APP", "1")
    config = _external_config(tmp_path / "external")
    monkeypatch.setattr(migration, "load_config", lambda: config)
    monkeypatch.setattr(
        migration,
        "load_agent_config",
        lambda *_args, **_kwargs: pytest.fail("external config was opened"),
    )
    monkeypatch.setattr(
        migration,
        "_ensure_workspace_json_files",
        lambda *_args, **_kwargs: pytest.fail("external workspace was written"),
    )

    migration._do_ensure_default_agent()  # pylint: disable=protected-access


@pytest.mark.asyncio
async def test_agent_list_does_not_probe_external_desktop_workspace(
    monkeypatch,
    tmp_path,
):
    monkeypatch.setenv("LANTAI_DESKTOP_APP", "1")
    config = _external_config(tmp_path / "external")
    monkeypatch.setattr(agents_router, "load_config", lambda: config)
    monkeypatch.setattr(
        agents_router,
        "load_agent_config",
        lambda *_args, **_kwargs: pytest.fail("external config was opened"),
    )

    response = await agents_router.list_agents()

    assert response.agents[0].id == "default"
    assert response.agents[0].workspace_dir == str(tmp_path / "external")
    assert response.agents[0].workspace_access_required is True


@pytest.mark.asyncio
async def test_agent_manager_fails_before_touching_external_workspace(
    monkeypatch,
    tmp_path,
):
    monkeypatch.setenv("LANTAI_DESKTOP_APP", "1")
    config = _external_config(tmp_path / "external")
    monkeypatch.setattr(
        "lantai.app.multi_agent_manager.load_config",
        lambda: config,
    )
    monkeypatch.setattr(
        "lantai.app.multi_agent_manager.Workspace",
        lambda *_args, **_kwargs: pytest.fail("external workspace was opened"),
    )

    with pytest.raises(ConfigurationException, match="must be restored"):
        await MultiAgentManager().get_agent("default")


@pytest.mark.asyncio
async def test_agent_context_fails_before_touching_external_workspace(
    monkeypatch,
    tmp_path,
):
    monkeypatch.setenv("LANTAI_DESKTOP_APP", "1")
    config = _external_config(tmp_path / "external")
    monkeypatch.setattr(
        "lantai.app.agent_context.load_config",
        lambda: config,
    )

    request = SimpleNamespace(headers={}, state=SimpleNamespace())

    with pytest.raises(HTTPException) as exc_info:
        await get_agent_for_request(request)

    assert exc_info.value.status_code == 423
    assert exc_info.value.detail["code"] == "workspace_access_required"
    assert exc_info.value.detail["agent_id"] == "default"


@pytest.mark.asyncio
async def test_agent_detail_does_not_probe_external_desktop_workspace(
    monkeypatch,
    tmp_path,
):
    monkeypatch.setenv("LANTAI_DESKTOP_APP", "1")
    config = _external_config(tmp_path / "external")
    monkeypatch.setattr(agents_router, "load_config", lambda: config)
    monkeypatch.setattr(
        agents_router,
        "load_agent_config",
        lambda *_args, **_kwargs: pytest.fail("external config was opened"),
    )

    with pytest.raises(HTTPException) as exc_info:
        await agents_router.get_agent("default")

    assert exc_info.value.status_code == 423
    assert exc_info.value.detail["code"] == "workspace_access_required"


@pytest.mark.asyncio
async def test_agent_delete_does_not_probe_external_desktop_workspace(
    monkeypatch,
    tmp_path,
):
    monkeypatch.setenv("LANTAI_DESKTOP_APP", "1")
    config = _external_config(tmp_path / "external", agent_id="other")
    monkeypatch.setattr(agents_router, "load_config", lambda: config)

    async def fail_stop_agent(*_args, **_kwargs):
        pytest.fail("unrelated multi-agent manager was touched")

    request = SimpleNamespace(
        app=SimpleNamespace(
            state=SimpleNamespace(
                multi_agent_manager=SimpleNamespace(stop_agent=fail_stop_agent),
            ),
        ),
    )

    with pytest.raises(HTTPException) as exc_info:
        await agents_router.delete_agent("other", request=request)

    assert exc_info.value.status_code == 423


@pytest.mark.asyncio
async def test_agent_reload_fails_before_touching_external_workspace(
    monkeypatch,
    tmp_path,
):
    monkeypatch.setenv("LANTAI_DESKTOP_APP", "1")
    config = _external_config(tmp_path / "external")
    monkeypatch.setattr(
        "lantai.app.multi_agent_manager.load_config",
        lambda: config,
    )
    monkeypatch.setattr(
        "lantai.app.multi_agent_manager.Workspace",
        lambda *_args, **_kwargs: pytest.fail("external workspace was opened"),
    )
    manager = MultiAgentManager()
    manager.agents["default"] = SimpleNamespace(
        _service_manager=SimpleNamespace(services={}),
    )

    with pytest.raises(ConfigurationException, match="must be restored"):
        await manager.reload_agent("default")


@pytest.mark.asyncio
async def test_effective_model_probe_skips_external_workspace(
    monkeypatch,
    tmp_path,
):
    monkeypatch.setenv("LANTAI_DESKTOP_APP", "1")
    config = _external_config(tmp_path / "external")
    monkeypatch.setattr(providers_router, "load_config", lambda: config)
    monkeypatch.setattr(
        providers_router,
        "load_agent_config",
        lambda *_args, **_kwargs: pytest.fail("external config was opened"),
    )

    assert await providers_router._load_agent_model(None, "default") is None


def test_agent_dirs_skip_external_desktop_workspace(
    monkeypatch,
    tmp_path,
):
    monkeypatch.setenv("LANTAI_DESKTOP_APP", "1")
    external_root = tmp_path / "external"
    config = _external_config(external_root)
    monkeypatch.setattr(config_utils, "load_config", lambda: config)
    original_exists = Path.exists

    def guarded_exists(path):
        if path == external_root or external_root in path.parents:
            pytest.fail("external path was stat'ed")
        # App-managed restore journals and pytest's own paths remain readable.
        return original_exists(path)

    monkeypatch.setattr(
        Path,
        "exists",
        guarded_exists,
    )

    assert config_utils.get_agent_dirs() == []


def test_agent_config_helpers_skip_external_desktop_workspace(
    monkeypatch,
    tmp_path,
):
    monkeypatch.setenv("LANTAI_DESKTOP_APP", "1")
    config = _external_config(tmp_path / "external")
    monkeypatch.setattr(config_utils, "load_config", lambda: config)
    monkeypatch.setattr(
        config_utils,
        "load_agent_config",
        lambda *_args, **_kwargs: pytest.fail("external config was opened"),
    )

    assert config_utils.get_heartbeat_config("default")
    assert config_utils.get_dream_cron("default") == ""


def test_background_preload_skips_external_desktop_workspace(
    monkeypatch,
    tmp_path,
):
    monkeypatch.setenv("LANTAI_DESKTOP_APP", "1")

    assert not MultiAgentManager._agent_needs_background_preload(  # pylint: disable=protected-access
        str(tmp_path / "external"),
    )
