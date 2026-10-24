# -*- coding: utf-8 -*-
"""Behavior locks for startup reconciliation and durable wake polling."""

import asyncio
from pathlib import Path
from types import SimpleNamespace

import pytest

from lantai.app.recovery_coordinator import RecoveryReport
from lantai.app.runner.manager import ChatManager
from lantai.app.runner.models import ChatSpec
from lantai.app.runner.repo.json_repo import JsonChatRepository
from lantai.app.workspace import workspace as workspace_module
from lantai.app.workspace.workspace import Workspace
from lantai.analysis import AnalysisError, AnalysisService


@pytest.mark.asyncio
async def test_workspace_reconciles_once_then_scans_only_due_wakes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[dict[str, bool]] = []

    class _Coordinator:
        def __init__(self, _workspace) -> None:
            pass

        async def recover(self, **kwargs):
            calls.append(kwargs)
            if len(calls) == 2:
                raise asyncio.CancelledError
            return RecoveryReport()

    import lantai.app.recovery_coordinator as recovery_module

    monkeypatch.setattr(recovery_module, "RecoveryCoordinator", _Coordinator)
    monkeypatch.setattr(
        workspace_module,
        "RUNTIME_WAKE_SCAN_INTERVAL_SECONDS",
        0,
    )
    workspace = SimpleNamespace(agent_id="default")

    with pytest.raises(asyncio.CancelledError):
        await Workspace._run_runtime_recovery_loop(workspace)

    assert calls == [
        {
            "auto_resume": True,
            "require_autonomy_grant": True,
            "due_wakes_only": False,
            "autonomy_resume_condition": "app_reopen",
        },
        {
            "auto_resume": True,
            "require_autonomy_grant": True,
            "due_wakes_only": True,
            "autonomy_resume_condition": "network_recovery",
        },
    ]


@pytest.mark.asyncio
async def test_workspace_recovery_retires_pending_analysis_without_chat_list(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    manager = ChatManager(repo=JsonChatRepository(tmp_path / "chats.json"))
    chat = await manager.create_chat(ChatSpec(
        id="recovery-analysis-chat", name="分析", session_id="console:default",
        user_id="default", channel="console",
    ))
    source = tmp_path / "source.csv"
    source.write_text("amount\n7\n", encoding="utf-8")
    service = AnalysisService(tmp_path, scope=chat.meta["session_state_id"])
    service.ingest(source)
    await manager.delete_chats_with_specs([chat.id])
    restarted_manager = ChatManager(repo=JsonChatRepository(tmp_path / "chats.json"))
    assert len(await restarted_manager.pending_analysis_cleanup_jobs()) == 1
    assert service.root.exists()

    class _Coordinator:
        calls = 0

        def __init__(self, _workspace) -> None:
            pass

        async def recover(self, **_kwargs):
            type(self).calls += 1
            if type(self).calls == 2:
                raise asyncio.CancelledError
            return RecoveryReport()

    import lantai.app.recovery_coordinator as recovery_module

    monkeypatch.setattr(recovery_module, "RecoveryCoordinator", _Coordinator)
    monkeypatch.setattr(workspace_module, "RUNTIME_WAKE_SCAN_INTERVAL_SECONDS", 0)
    workspace = SimpleNamespace(
        agent_id="default", workspace_dir=tmp_path, chat_manager=restarted_manager,
    )

    with pytest.raises(asyncio.CancelledError):
        await Workspace._run_runtime_recovery_loop(workspace)

    assert await restarted_manager.pending_analysis_cleanup_jobs() == []
    assert not service.root.exists()
    with pytest.raises(AnalysisError, match="retired"):
        service.ingest(source)
    assert source.exists()


@pytest.mark.asyncio
async def test_workspace_recovery_retries_transient_analysis_cleanup_without_reopen(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    manager = ChatManager(repo=JsonChatRepository(tmp_path / "chats.json"))
    chat = await manager.create_chat(ChatSpec(
        id="retry-analysis-chat", name="分析", session_id="console:default",
        user_id="default", channel="console",
    ))
    source = tmp_path / "source.csv"
    source.write_text("amount\n7\n", encoding="utf-8")
    service = AnalysisService(tmp_path, scope=chat.meta["session_state_id"])
    service.ingest(source)
    await manager.delete_chats_with_specs([chat.id])
    restarted_manager = ChatManager(repo=JsonChatRepository(tmp_path / "chats.json"))

    import lantai.app.recovery_coordinator as recovery_module
    import lantai.app.runner.api as runner_api

    original_retire = runner_api._retire_analysis_cleanup_job
    attempts = 0

    def fail_once(job, workspace) -> None:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise OSError("transient cleanup failure")
        original_retire(job, workspace)

    class _Coordinator:
        calls = 0

        def __init__(self, _workspace) -> None:
            pass

        async def recover(self, **_kwargs):
            type(self).calls += 1
            if type(self).calls == 3:
                raise asyncio.CancelledError
            return RecoveryReport()

    monkeypatch.setattr(runner_api, "_retire_analysis_cleanup_job", fail_once)
    monkeypatch.setattr(recovery_module, "RecoveryCoordinator", _Coordinator)
    monkeypatch.setattr(workspace_module, "RUNTIME_WAKE_SCAN_INTERVAL_SECONDS", 0)
    workspace = SimpleNamespace(
        agent_id="default", workspace_dir=tmp_path, chat_manager=restarted_manager,
    )

    with pytest.raises(asyncio.CancelledError):
        await Workspace._run_runtime_recovery_loop(workspace)

    assert attempts == 2
    assert await restarted_manager.pending_analysis_cleanup_jobs() == []
    assert not service.root.exists()
    assert source.exists()
