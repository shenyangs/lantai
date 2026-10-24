# -*- coding: utf-8 -*-
"""Runtime run continuation route tests."""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from lantai.app.beta_access.admission import BetaAccessAdmissionGuard
from lantai.app.capabilities.artifacts import artifact_from_path, register_artifact
from lantai.app.experience.events import (
    append_chat_event,
    build_runtime_run_events_response,
)
from lantai.app.runner.manager import ChatManager
from lantai.app.runner.models import ChatSpec
from lantai.app.runner.repo.json_repo import JsonChatRepository
from lantai.runtime.effects import RuntimeEffectJournal
from lantai.runtime.runs import (
    RuntimeCheckpoint,
    RuntimeEffectRecord,
    RuntimeRunContract,
    RuntimeRunOwner,
)
from lantai.runtime.store import RuntimeRunStore, RuntimeRunStoreConflictError
from lantai.security.beta_access import AccessSnapshot, BetaAccessStatus


class FakeTaskTracker:
    def __init__(self, snapshot=None) -> None:
        self.snapshot = snapshot
        self.calls: list[str] = []

    async def get_snapshot(self, run_key: str):
        self.calls.append(run_key)
        return self.snapshot


def test_incomplete_response_uses_intent_preserving_continuation_prompt() -> None:
    from lantai.app.routers.runtime import _build_continue_prompt

    chat = ChatSpec(
        id="chat-incomplete",
        name="Incomplete",
        session_id="console:incomplete",
        user_id="test",
        channel="console",
        meta={
            "last_turn_text": "帮我整理值得逛的展台",
            "last_turn_outcome_status": "incomplete",
        },
    )

    prompt = _build_continue_prompt(
        run_id="run-incomplete",
        chat=chat,
        workspace_dir=None,
        latest_event=None,
        recent_events=[],
        requested_stage=None,
        after_seq=0,
    )

    assert prompt.startswith("上一轮运行已结束，但可能没有形成完整回答。")
    assert "只是在聊天或提问" in prompt
    assert "不要制造文件或制品" in prompt
    assert "本轮必须输出" not in prompt
    assert "似乎中途停住" not in prompt


async def _make_workspace(tmp_path, *, active_snapshot=None):
    from lantai.app.routers import runtime as runtime_router

    topic_workspace = tmp_path / "topic-workspaces" / "chat-continue"
    topic_workspace.mkdir(parents=True, exist_ok=True)
    manager = ChatManager(repo=JsonChatRepository(tmp_path / "chats.json"))
    chat = await manager.create_chat(
        ChatSpec(
            id="chat-continue",
            name="Continue",
            session_id="console:continue",
            user_id="test",
            channel="console",
            meta={
                "topic_workspace_dir": str(topic_workspace),
                "last_turn_text": "请继续实现任务",
                "pending_turn": {
                    "run_id": "run-continue",
                    "text": "请继续实现任务",
                },
            },
        ),
    )
    workspace = SimpleNamespace(
        agent_id="default",
        workspace_dir=tmp_path,
        chat_manager=manager,
        task_tracker=FakeTaskTracker(active_snapshot),
    )
    return runtime_router, workspace, chat, topic_workspace


async def _write_runtime_contract(
    topic_workspace,
    *,
    run_id: str = "run-continue",
    attempt_id: str = "run-continue:attempt-1",
    revision: int = 4,
    source: str = "chat",
    status: str = "stalled",
):
    await RuntimeRunStore(topic_workspace).save_contract(
        RuntimeRunContract(
            run_id=run_id,
            attempt_id=attempt_id,
            source=source,  # type: ignore[arg-type]
            owner=RuntimeRunOwner(
                user_id="default",
                agent_id="default",
                session_id="console:continue",
                workspace_id=str(topic_workspace),
                channel="console",
            ),
            status=status,  # type: ignore[arg-type]
            revision=revision,
            release_edition="official",
        ),
        expected_revision=None,
    )


def _deny_beta_access(workspace) -> None:
    workspace.beta_access_admission_guard = BetaAccessAdmissionGuard(
        lambda: AccessSnapshot(
            True,
            BetaAccessStatus.EXPIRED,
            "account-1",
            2,
            12,
        )
    )


def test_failure_diagnosis_classifies_preview_error() -> None:
    from lantai.app.routers import runtime as runtime_router

    diagnosis = runtime_router._failure_diagnosis_for_event(
        SimpleNamespace(
            seq=7,
            status="failed",
            type="tool.failed",
            summary="预览失败",
            payload={
                "phase": "preview",
                "trace_title": "预览错误：Vite",
                "error": "net::ERR_CONNECTION_REFUSED http://localhost:5173",
            },
        ),
    )

    assert diagnosis is not None
    assert diagnosis["category"] == "preview"
    assert diagnosis["summary"] == "前端预览或浏览器验证失败"
    assert "dev server" in diagnosis["next_action"]


@pytest.mark.asyncio
async def test_runtime_continue_reports_active_run_reconnected(
    monkeypatch,
    tmp_path,
):
    runtime_router, workspace, chat, topic_workspace = await _make_workspace(
        tmp_path,
        active_snapshot={"status": "running", "event_count": 3},
    )
    _deny_beta_access(workspace)

    await append_chat_event(
        workspace_dir=topic_workspace,
        agent_id="default",
        chat_id=chat.id,
        session_id=chat.session_id,
        run_id="run-continue",
        kind="chat.tool_call.started",
        status="running",
        summary="开始构建",
        payload={
            "tool_call_id": "tool-build",
            "trace_title": "构建项目：npm",
        },
    )

    async def fake_workspace(_request):
        return workspace

    monkeypatch.setattr(runtime_router, "_workspace_for_request", fake_workspace)

    result = await runtime_router.continue_runtime_run(
        SimpleNamespace(),
        "run-continue",
        runtime_router.RuntimeRunContinueRequest(
            mode="reconnect",
            chat_id=chat.id,
            session_id=chat.session_id,
            after_seq=1,
            stage_label="构建项目：npm",
        ),
    )

    assert result["reconnected"] is True
    assert result["mode"] == "reconnect"
    assert result["reason"] == "active_run_still_running"
    assert result["text"] is None
    assert workspace.task_tracker.calls == [chat.id]

    events = build_runtime_run_events_response([topic_workspace], "run-continue")
    assert events.events[-1].type == "turn.continue_requested"
    assert events.events[-1].payload["mode"] == "reconnect"
    assert events.events[-1].payload["active_run"] is True
    assert events.events[-1].status == "running"


@pytest.mark.asyncio
async def test_runtime_continue_rejects_stale_expected_attempt(
    monkeypatch,
    tmp_path,
):
    runtime_router, workspace, chat, topic_workspace = await _make_workspace(
        tmp_path,
        active_snapshot=None,
    )
    await _write_runtime_contract(topic_workspace)

    async def fake_workspace(_request):
        return workspace

    monkeypatch.setattr(runtime_router, "_workspace_for_request", fake_workspace)

    with pytest.raises(HTTPException) as exc_info:
        await runtime_router.continue_runtime_run(
            SimpleNamespace(),
            "run-continue",
            runtime_router.RuntimeRunContinueRequest(
                chat_id=chat.id,
                session_id=chat.session_id,
                expected_attempt_id="run-continue:old-attempt",
                expected_revision=4,
                workspace_dir=str(topic_workspace),
            ),
        )

    assert exc_info.value.status_code == 409
    assert exc_info.value.detail["code"] == "stale_attempt"
    assert exc_info.value.detail["expected_attempt_id"] == "run-continue:old-attempt"
    assert exc_info.value.detail["current_attempt_id"] == "run-continue:attempt-1"


@pytest.mark.asyncio
async def test_runtime_continue_beta_denial_preserves_existing_attempt(
    monkeypatch,
    tmp_path,
):
    runtime_router, workspace, chat, topic_workspace = await _make_workspace(
        tmp_path,
        active_snapshot=None,
    )
    await _write_runtime_contract(topic_workspace)
    _deny_beta_access(workspace)
    before = await RuntimeRunStore(topic_workspace).load_contract("run-continue")

    async def fake_workspace(_request):
        return workspace

    monkeypatch.setattr(runtime_router, "_workspace_for_request", fake_workspace)

    with pytest.raises(HTTPException) as raised:
        await runtime_router.continue_runtime_run(
            SimpleNamespace(),
            "run-continue",
            runtime_router.RuntimeRunContinueRequest(
                chat_id=chat.id,
                session_id=chat.session_id,
                workspace_dir=str(topic_workspace),
            ),
        )

    after = await RuntimeRunStore(topic_workspace).load_contract("run-continue")
    assert raised.value.status_code == 403
    assert raised.value.detail == {
        "code": "beta_access_denied",
        "reason": "beta_access_expired:resume_or_wake",
        "snapshot_revision": 12,
    }
    assert after == before


@pytest.mark.asyncio
async def test_runtime_retry_rejects_stale_expected_revision(
    monkeypatch,
    tmp_path,
):
    runtime_router, workspace, _chat, topic_workspace = await _make_workspace(
        tmp_path,
        active_snapshot=None,
    )
    await _write_runtime_contract(topic_workspace, revision=5)

    async def fake_workspace(_request):
        return workspace

    monkeypatch.setattr(runtime_router, "_workspace_for_request", fake_workspace)

    with pytest.raises(HTTPException) as exc_info:
        await runtime_router.retry_runtime_run(
            SimpleNamespace(),
            "run-continue",
            runtime_router.RuntimeRunControlRequest(
                expected_attempt_id="run-continue:attempt-1",
                expected_revision=4,
                workspace_dir=str(topic_workspace),
            ),
        )

    assert exc_info.value.status_code == 409
    assert exc_info.value.detail["code"] == "stale_attempt"
    assert exc_info.value.detail["expected_revision"] == 4
    assert exc_info.value.detail["current_revision"] == 5


@pytest.mark.asyncio
async def test_runtime_retry_beta_denial_preserves_existing_attempt(
    monkeypatch,
    tmp_path,
):
    runtime_router, workspace, _chat, topic_workspace = await _make_workspace(
        tmp_path,
        active_snapshot=None,
    )
    await _write_runtime_contract(
        topic_workspace,
        revision=4,
        status="failed",
    )
    _deny_beta_access(workspace)
    before = await RuntimeRunStore(topic_workspace).load_contract("run-continue")

    async def fake_workspace(_request):
        return workspace

    monkeypatch.setattr(runtime_router, "_workspace_for_request", fake_workspace)

    with pytest.raises(HTTPException) as raised:
        await runtime_router.retry_runtime_run(
            SimpleNamespace(),
            "run-continue",
            runtime_router.RuntimeRunControlRequest(
                expected_attempt_id="run-continue:attempt-1",
                expected_revision=4,
                workspace_dir=str(topic_workspace),
            ),
        )

    after = await RuntimeRunStore(topic_workspace).load_contract("run-continue")
    assert raised.value.status_code == 403
    assert raised.value.detail["code"] == "beta_access_denied"
    assert after == before


@pytest.mark.asyncio
async def test_runtime_retry_prepares_new_queued_attempt_contract(
    monkeypatch,
    tmp_path,
):
    runtime_router, workspace, chat, topic_workspace = await _make_workspace(
        tmp_path,
        active_snapshot=None,
    )
    await _write_runtime_contract(
        topic_workspace,
        revision=4,
        status="failed",
    )

    async def fake_workspace(_request):
        return workspace

    monkeypatch.setattr(runtime_router, "_workspace_for_request", fake_workspace)

    result = await runtime_router.retry_runtime_run(
        SimpleNamespace(),
        "run-continue",
        runtime_router.RuntimeRunControlRequest(
            expected_attempt_id="run-continue:attempt-1",
            expected_revision=4,
            workspace_dir=str(topic_workspace),
        ),
    )

    saved = await RuntimeRunStore(topic_workspace).load_contract("run-continue")
    events = build_runtime_run_events_response([topic_workspace], "run-continue")

    assert result["queued"] is False
    assert result["mode"] == "retry"
    assert result["attempt_prepared"] is True
    assert result["attempt_id"] != "run-continue:attempt-1"
    assert result["previous_attempt_id"] == "run-continue:attempt-1"
    assert result["revision"] == 0
    assert saved is not None
    assert saved.attempt_id == result["attempt_id"]
    assert saved.status == "queued"
    assert saved.revision == 0
    assert getattr(saved, "attempt_reason") == "retry"
    assert getattr(saved, "previous_attempt_id") == "run-continue:attempt-1"
    archives = list(
        (RuntimeRunStore(topic_workspace).root / "archives" / "runs").glob(
            "*/contract.rev4.*.archive.json"
        )
    )
    assert len(archives) == 1
    archived_payload = json.loads(archives[0].read_text(encoding="utf-8"))
    assert archived_payload["attempt_id"] == "run-continue:attempt-1"
    assert archived_payload["envelope"]["payload"]["revision"] == 4
    assert events.events[-1].type == "turn.continue_requested"
    assert events.events[-1].payload["mode"] == "retry"
    assert events.events[-1].payload["attempt_id"] == result["attempt_id"]
    assert events.events[-1].payload["revision"] == 0
    assert chat.id == result["chat_id"]


@pytest.mark.asyncio
async def test_runtime_pause_browser_run_updates_canonical_contract(
    monkeypatch,
    tmp_path,
):
    runtime_router, workspace, _chat, topic_workspace = await _make_workspace(
        tmp_path,
        active_snapshot=None,
    )
    await _write_runtime_contract(
        topic_workspace,
        source="browser",
        status="running",
        revision=4,
    )
    calls: list[str] = []

    fake_browser_runtime = SimpleNamespace(
        pause=lambda run_id: calls.append(run_id)
        or SimpleNamespace(status="paused"),
        resume=lambda run_id: SimpleNamespace(status="queued"),
    )

    async def fake_workspace(_request):
        return workspace

    monkeypatch.setattr(runtime_router, "_workspace_for_request", fake_workspace)
    monkeypatch.setattr(
        runtime_router,
        "_browser_task_runtime",
        lambda: fake_browser_runtime,
    )

    result = await runtime_router.pause_runtime_run(
        SimpleNamespace(),
        "run-continue",
        runtime_router.RuntimeRunControlRequest(
            expected_attempt_id="run-continue:attempt-1",
            expected_revision=4,
            workspace_dir=str(topic_workspace),
        ),
    )

    saved = await RuntimeRunStore(topic_workspace).load_contract("run-continue")
    assert result["paused"] is True
    assert result["status"] == "paused"
    assert result["revision"] == 5
    assert result["browser_status"] == "paused"
    assert calls == ["run-continue"]
    assert saved is not None
    assert saved.status == "paused"
    assert saved.revision == 5


@pytest.mark.asyncio
async def test_runtime_pause_does_not_touch_browser_when_store_fence_conflicts(
    monkeypatch,
    tmp_path,
):
    runtime_router, workspace, _chat, topic_workspace = await _make_workspace(
        tmp_path,
        active_snapshot=None,
    )
    await _write_runtime_contract(
        topic_workspace,
        source="browser",
        status="running",
        revision=4,
    )
    calls: list[str] = []

    fake_browser_runtime = SimpleNamespace(
        get_run=lambda run_id: SimpleNamespace(run_id=run_id, status="running"),
        pause=lambda run_id: calls.append(run_id)
        or SimpleNamespace(status="paused"),
        resume=lambda run_id: SimpleNamespace(status="queued"),
    )

    async def fake_workspace(_request):
        return workspace

    async def conflicting_save_contract(self, contract, **kwargs):
        raise RuntimeRunStoreConflictError("simulated concurrent update")

    monkeypatch.setattr(runtime_router, "_workspace_for_request", fake_workspace)
    monkeypatch.setattr(
        runtime_router,
        "_browser_task_runtime",
        lambda: fake_browser_runtime,
    )
    monkeypatch.setattr(
        RuntimeRunStore,
        "save_contract",
        conflicting_save_contract,
    )

    with pytest.raises(HTTPException) as exc_info:
        await runtime_router.pause_runtime_run(
            SimpleNamespace(),
            "run-continue",
            runtime_router.RuntimeRunControlRequest(
                expected_attempt_id="run-continue:attempt-1",
                expected_revision=4,
                workspace_dir=str(topic_workspace),
            ),
        )

    assert exc_info.value.status_code == 409
    assert exc_info.value.detail["code"] == "stale_attempt"
    assert calls == []


@pytest.mark.asyncio
async def test_runtime_pause_rolls_back_contract_when_browser_run_disappears(
    monkeypatch,
    tmp_path,
):
    runtime_router, workspace, _chat, topic_workspace = await _make_workspace(
        tmp_path,
        active_snapshot=None,
    )
    await _write_runtime_contract(
        topic_workspace,
        source="browser",
        status="running",
        revision=4,
    )

    fake_browser_runtime = SimpleNamespace(
        get_run=lambda run_id: SimpleNamespace(run_id=run_id, status="running"),
        pause=lambda run_id: (_ for _ in ()).throw(
            KeyError(f"Browser task not found: {run_id}"),
        ),
        resume=lambda run_id: SimpleNamespace(status="queued"),
    )

    async def fake_workspace(_request):
        return workspace

    monkeypatch.setattr(runtime_router, "_workspace_for_request", fake_workspace)
    monkeypatch.setattr(
        runtime_router,
        "_browser_task_runtime",
        lambda: fake_browser_runtime,
    )

    with pytest.raises(HTTPException) as exc_info:
        await runtime_router.pause_runtime_run(
            SimpleNamespace(),
            "run-continue",
            runtime_router.RuntimeRunControlRequest(
                expected_attempt_id="run-continue:attempt-1",
                expected_revision=4,
                workspace_dir=str(topic_workspace),
            ),
        )

    saved = await RuntimeRunStore(topic_workspace).load_contract("run-continue")
    assert exc_info.value.status_code == 404
    assert exc_info.value.detail["code"] == "browser_task_not_found"
    assert saved is not None
    assert saved.status == "running"
    assert saved.revision == 4


@pytest.mark.asyncio
async def test_runtime_pause_rejects_terminal_browser_run_without_mutating_contract(
    monkeypatch,
    tmp_path,
):
    runtime_router, workspace, _chat, topic_workspace = await _make_workspace(
        tmp_path,
        active_snapshot=None,
    )
    await _write_runtime_contract(
        topic_workspace,
        source="browser",
        status="running",
        revision=4,
    )
    calls: list[str] = []

    fake_browser_runtime = SimpleNamespace(
        get_run=lambda run_id: SimpleNamespace(run_id=run_id, status="failed"),
        pause=lambda run_id: calls.append(run_id)
        or SimpleNamespace(status="failed"),
        resume=lambda run_id: SimpleNamespace(status="failed"),
    )

    async def fake_workspace(_request):
        return workspace

    monkeypatch.setattr(runtime_router, "_workspace_for_request", fake_workspace)
    monkeypatch.setattr(
        runtime_router,
        "_browser_task_runtime",
        lambda: fake_browser_runtime,
    )

    with pytest.raises(HTTPException) as exc_info:
        await runtime_router.pause_runtime_run(
            SimpleNamespace(),
            "run-continue",
            runtime_router.RuntimeRunControlRequest(
                expected_attempt_id="run-continue:attempt-1",
                expected_revision=4,
                workspace_dir=str(topic_workspace),
            ),
        )

    saved = await RuntimeRunStore(topic_workspace).load_contract("run-continue")
    assert exc_info.value.status_code == 409
    assert exc_info.value.detail["code"] == "browser_task_terminal"
    assert exc_info.value.detail["browser_status"] == "failed"
    assert calls == []
    assert saved is not None
    assert saved.status == "running"
    assert saved.revision == 4


@pytest.mark.asyncio
async def test_runtime_resume_browser_run_updates_canonical_contract(
    monkeypatch,
    tmp_path,
):
    runtime_router, workspace, _chat, topic_workspace = await _make_workspace(
        tmp_path,
        active_snapshot=None,
    )
    await _write_runtime_contract(
        topic_workspace,
        source="browser",
        status="paused",
        revision=5,
    )
    calls: list[str] = []

    fake_browser_runtime = SimpleNamespace(
        pause=lambda run_id: SimpleNamespace(status="paused"),
        resume=lambda run_id: calls.append(run_id)
        or SimpleNamespace(status="queued"),
    )

    async def fake_workspace(_request):
        return workspace

    monkeypatch.setattr(runtime_router, "_workspace_for_request", fake_workspace)
    monkeypatch.setattr(
        runtime_router,
        "_browser_task_runtime",
        lambda: fake_browser_runtime,
    )

    result = await runtime_router.resume_runtime_run(
        SimpleNamespace(),
        "run-continue",
        runtime_router.RuntimeRunControlRequest(
            expected_attempt_id="run-continue:attempt-1",
            expected_revision=5,
            workspace_dir=str(topic_workspace),
        ),
    )

    saved = await RuntimeRunStore(topic_workspace).load_contract("run-continue")
    assert result["resumed"] is True
    assert result["status"] == "running"
    assert result["revision"] == 6
    assert result["browser_status"] == "queued"
    assert calls == ["run-continue"]
    assert saved is not None
    assert saved.status == "running"
    assert saved.revision == 6


@pytest.mark.asyncio
async def test_runtime_resume_beta_denial_does_not_wake_browser_run(
    monkeypatch,
    tmp_path,
):
    runtime_router, workspace, _chat, topic_workspace = await _make_workspace(
        tmp_path,
        active_snapshot=None,
    )
    await _write_runtime_contract(
        topic_workspace,
        source="browser",
        status="paused",
        revision=5,
    )
    _deny_beta_access(workspace)
    before = await RuntimeRunStore(topic_workspace).load_contract("run-continue")

    async def fake_workspace(_request):
        return workspace

    monkeypatch.setattr(runtime_router, "_workspace_for_request", fake_workspace)
    monkeypatch.setattr(
        runtime_router,
        "_browser_task_runtime",
        lambda: pytest.fail("denied resume must not wake the browser runtime"),
    )

    with pytest.raises(HTTPException) as raised:
        await runtime_router.resume_runtime_run(
            SimpleNamespace(),
            "run-continue",
            runtime_router.RuntimeRunControlRequest(
                expected_attempt_id="run-continue:attempt-1",
                expected_revision=5,
                workspace_dir=str(topic_workspace),
            ),
        )

    after = await RuntimeRunStore(topic_workspace).load_contract("run-continue")
    assert raised.value.status_code == 403
    assert raised.value.detail["code"] == "beta_access_denied"
    assert after == before


@pytest.mark.asyncio
async def test_runtime_pause_rejects_non_browser_contract(
    monkeypatch,
    tmp_path,
):
    runtime_router, workspace, _chat, topic_workspace = await _make_workspace(
        tmp_path,
        active_snapshot=None,
    )
    await _write_runtime_contract(
        topic_workspace,
        source="chat",
        status="running",
        revision=4,
    )

    async def fake_workspace(_request):
        return workspace

    monkeypatch.setattr(runtime_router, "_workspace_for_request", fake_workspace)

    with pytest.raises(HTTPException) as exc_info:
        await runtime_router.pause_runtime_run(
            SimpleNamespace(),
            "run-continue",
            runtime_router.RuntimeRunControlRequest(
                expected_attempt_id="run-continue:attempt-1",
                expected_revision=4,
                workspace_dir=str(topic_workspace),
            ),
        )

    assert exc_info.value.status_code == 409
    assert exc_info.value.detail["code"] == "runtime_control_not_supported"
    assert exc_info.value.detail["source"] == "chat"


@pytest.mark.asyncio
async def test_runtime_archive_terminal_run_snapshots_artifact_manifest(
    monkeypatch,
    tmp_path,
):
    runtime_router, workspace, _chat, topic_workspace = await _make_workspace(
        tmp_path,
        active_snapshot=None,
    )
    await _write_runtime_contract(
        topic_workspace,
        status="succeeded",
        revision=6,
    )
    artifact_path = topic_workspace / "report.md"
    artifact_path.write_text("# Report\n", encoding="utf-8")
    artifact = artifact_from_path(
        artifact_path,
        title="Report",
        run_id="run-continue",
        retention="run_lifetime",
    )
    register_artifact(topic_workspace, artifact)

    async def fake_workspace(_request):
        return workspace

    monkeypatch.setattr(runtime_router, "_workspace_for_request", fake_workspace)

    result = await runtime_router.archive_runtime_run(
        SimpleNamespace(),
        "run-continue",
        runtime_router.RuntimeRunControlRequest(
            expected_attempt_id="run-continue:attempt-1",
            expected_revision=6,
            workspace_dir=str(topic_workspace),
        ),
    )

    snapshot_path = Path(result["artifact_cascade"]["manifest_snapshot_path"])
    runtime_archive_path = Path(result["runtime_archive"]["archive_path"])
    active_contract = await RuntimeRunStore(topic_workspace).load_contract(
        "run-continue",
    )
    assert result["archived"] is True
    assert result["status"] == "succeeded"
    assert result["workspace_dir"] == str(topic_workspace)
    assert result["artifact_cascade"]["cascade"]["operation"] == "archive"
    assert result["artifact_cascade"]["cascade"]["matching_manifest_count"] == 1
    assert result["runtime_archive"]["kind"] == "runtime_run_archive"
    assert result["runtime_archive"]["active_contract_retained"] is True
    assert runtime_archive_path.exists()
    assert snapshot_path.exists()
    assert artifact_path.exists()
    assert active_contract is not None
    assert active_contract.status == "succeeded"


@pytest.mark.asyncio
async def test_runtime_archive_rejects_active_run(
    monkeypatch,
    tmp_path,
):
    runtime_router, workspace, _chat, topic_workspace = await _make_workspace(
        tmp_path,
        active_snapshot=None,
    )
    await _write_runtime_contract(
        topic_workspace,
        status="running",
        revision=6,
    )

    async def fake_workspace(_request):
        return workspace

    monkeypatch.setattr(runtime_router, "_workspace_for_request", fake_workspace)

    with pytest.raises(HTTPException) as exc_info:
        await runtime_router.archive_runtime_run(
            SimpleNamespace(),
            "run-continue",
            runtime_router.RuntimeRunControlRequest(
                expected_attempt_id="run-continue:attempt-1",
                expected_revision=6,
                workspace_dir=str(topic_workspace),
            ),
        )

    assert exc_info.value.status_code == 409
    assert exc_info.value.detail["code"] == "runtime_run_not_terminal"
    assert exc_info.value.detail["status"] == "running"


@pytest.mark.asyncio
async def test_runtime_delete_requires_explicit_approval(
    monkeypatch,
    tmp_path,
):
    runtime_router, workspace, _chat, topic_workspace = await _make_workspace(
        tmp_path,
        active_snapshot=None,
    )
    await _write_runtime_contract(
        topic_workspace,
        status="succeeded",
        revision=7,
    )

    async def fake_workspace(_request):
        return workspace

    monkeypatch.setattr(runtime_router, "_workspace_for_request", fake_workspace)

    with pytest.raises(HTTPException) as exc_info:
        await runtime_router.delete_runtime_run(
            SimpleNamespace(),
            "run-continue",
            runtime_router.RuntimeRunDeleteRequest(
                expected_attempt_id="run-continue:attempt-1",
                expected_revision=7,
                workspace_dir=str(topic_workspace),
            ),
        )

    assert exc_info.value.status_code == 403
    assert exc_info.value.detail["code"] == "runtime_run_delete_approval_required"
    assert await RuntimeRunStore(topic_workspace).load_contract("run-continue")


@pytest.mark.asyncio
async def test_runtime_delete_terminal_run_archives_and_removes_active_contract(
    monkeypatch,
    tmp_path,
):
    runtime_router, workspace, _chat, topic_workspace = await _make_workspace(
        tmp_path,
        active_snapshot=None,
    )
    await _write_runtime_contract(
        topic_workspace,
        status="succeeded",
        revision=7,
    )
    artifact_path = topic_workspace / "report.md"
    artifact_path.write_text("# Report\n", encoding="utf-8")
    artifact = artifact_from_path(
        artifact_path,
        title="Report",
        run_id="run-continue",
        retention="run_lifetime",
    )
    register_artifact(topic_workspace, artifact)

    async def fake_workspace(_request):
        return workspace

    monkeypatch.setattr(runtime_router, "_workspace_for_request", fake_workspace)
    grant_lifecycle_calls: list[tuple[str, bool, str]] = []

    def fake_file_grant_lifecycle(*, operation, dry_run, run_id):
        grant_lifecycle_calls.append((operation, dry_run, run_id))
        return {
            "kind": "browser_file_grant_lifecycle",
            "operation": operation,
            "dry_run": dry_run,
            "run_id": run_id,
            "matched_count": 1,
            "revoked_count": 1,
            "remaining_count": 0,
        }

    monkeypatch.setattr(
        runtime_router,
        "_browser_file_grant_lifecycle",
        fake_file_grant_lifecycle,
    )

    result = await runtime_router.delete_runtime_run(
        SimpleNamespace(),
        "run-continue",
        runtime_router.RuntimeRunDeleteRequest(
            approved=True,
            reason="User requested cleanup",
            expected_attempt_id="run-continue:attempt-1",
            expected_revision=7,
            workspace_dir=str(topic_workspace),
        ),
    )

    tombstone_path = Path(result["runtime_delete"]["tombstone_path"])
    assert result["deleted"] is True
    assert result["delete_artifacts"] is False
    assert result["delete_approval"]["reason"] == "User requested cleanup"
    assert result["artifact_cascade"]["dry_run"] is True
    assert result["artifact_cascade"]["candidate_count"] == 1
    assert result["runtime_delete"]["kind"] == "runtime_run_delete"
    assert result["file_grant_lifecycle"]["revoked_count"] == 1
    assert grant_lifecycle_calls == [("delete", False, "run-continue")]
    assert tombstone_path.exists()
    assert artifact_path.exists()
    assert await RuntimeRunStore(topic_workspace).load_contract("run-continue") is None


@pytest.mark.asyncio
async def test_runtime_delete_can_apply_artifact_retention_cascade(
    monkeypatch,
    tmp_path,
):
    runtime_router, workspace, _chat, topic_workspace = await _make_workspace(
        tmp_path,
        active_snapshot=None,
    )
    await _write_runtime_contract(
        topic_workspace,
        status="succeeded",
        revision=7,
    )
    transient_path = topic_workspace / "transient.md"
    saved_path = topic_workspace / "saved.md"
    transient_path.write_text("# Delete\n", encoding="utf-8")
    saved_path.write_text("# Keep\n", encoding="utf-8")
    transient = artifact_from_path(
        transient_path,
        title="Transient",
        run_id="run-continue",
        retention="run_lifetime",
    )
    saved = artifact_from_path(
        saved_path,
        title="Saved",
        run_id="run-continue",
        retention="user_saved",
    )
    register_artifact(topic_workspace, transient)
    register_artifact(topic_workspace, saved)

    async def fake_workspace(_request):
        return workspace

    monkeypatch.setattr(runtime_router, "_workspace_for_request", fake_workspace)

    result = await runtime_router.delete_runtime_run(
        SimpleNamespace(),
        "run-continue",
        runtime_router.RuntimeRunDeleteRequest(
            approved=True,
            reason="Delete terminal run and eligible artifacts",
            delete_artifacts=True,
            expected_attempt_id="run-continue:attempt-1",
            expected_revision=7,
            workspace_dir=str(topic_workspace),
        ),
    )

    assert result["delete_artifacts"] is True
    assert result["artifact_cascade"]["dry_run"] is False
    assert result["artifact_cascade"]["deleted_count"] == 1
    assert not transient_path.exists()
    assert saved_path.exists()
    assert await RuntimeRunStore(topic_workspace).load_contract("run-continue") is None


@pytest.mark.asyncio
async def test_runtime_continue_returns_backend_continuation_prompt(
    monkeypatch,
    tmp_path,
):
    runtime_router, workspace, chat, topic_workspace = await _make_workspace(
        tmp_path,
        active_snapshot=None,
    )

    await append_chat_event(
        workspace_dir=topic_workspace,
        agent_id="default",
        chat_id=chat.id,
        session_id=chat.session_id,
        run_id="run-continue",
        kind="chat.turn.started",
        status="running",
        summary="开始处理请求",
        payload={"text_preview": "请继续实现任务"},
    )

    await append_chat_event(
        workspace_dir=topic_workspace,
        agent_id="default",
        chat_id=chat.id,
        session_id=chat.session_id,
        run_id="run-continue",
        kind="chat.tool_call.started",
        status="running",
        summary="开始测试",
        payload={
            "tool_call_id": "tool-test",
            "trace_title": "运行测试：pnpm",
        },
    )

    await append_chat_event(
        workspace_dir=topic_workspace,
        agent_id="default",
        chat_id=chat.id,
        session_id=chat.session_id,
        run_id="run-continue",
        kind="chat.tool_call.finished",
        status="failed",
        summary="测试失败",
        payload={
            "tool_call_id": "tool-test",
            "trace_title": "运行测试：pnpm",
            "trace_detail": "前端单测未通过",
            "command": "pnpm test -- runtime",
            "cwd": "/tmp/topic-workspace",
            "exit_code": 1,
            "failure_reason": "断言失败：没有保存续跑草稿",
        },
    )

    await append_chat_event(
        workspace_dir=topic_workspace,
        agent_id="default",
        chat_id=chat.id,
        session_id=chat.session_id,
        run_id="run-continue",
        kind="chat.tool_call.started",
        status="running",
        summary="开始修改文件",
        payload={
            "tool_call_id": "tool-files",
            "trace_title": "修改运行逻辑",
        },
    )

    await append_chat_event(
        workspace_dir=topic_workspace,
        agent_id="default",
        chat_id=chat.id,
        session_id=chat.session_id,
        run_id="run-continue",
        kind="chat.tool_call.finished",
        status="completed",
        summary="文件变更",
        payload={
            "tool_call_id": "tool-files",
            "trace_title": "修改运行逻辑",
            "files_changed": [
                {
                    "path": "console/src/pages/Control/TaskCenter/index.tsx",
                    "added_lines": 12,
                    "deleted_lines": 2,
                },
            ],
        },
    )

    await append_chat_event(
        workspace_dir=topic_workspace,
        agent_id="default",
        chat_id=chat.id,
        session_id=chat.session_id,
        run_id="run-continue",
        kind="chat.tool_call.started",
        status="running",
        summary="开始构建",
        payload={
            "tool_call_id": "tool-build",
            "trace_title": "构建项目：npm",
            "trace_detail": "npm run build",
        },
    )

    async def fake_workspace(_request):
        return workspace

    monkeypatch.setattr(runtime_router, "_workspace_for_request", fake_workspace)

    result = await runtime_router.continue_runtime_run(
        SimpleNamespace(),
        "run-continue",
        runtime_router.RuntimeRunContinueRequest(
            chat_id=chat.id,
            session_id=chat.session_id,
            after_seq=1,
            stage_label="构建项目：npm",
            workspace_dir=str(topic_workspace),
        ),
    )

    assert result["reconnected"] is False
    assert result["mode"] == "takeover"
    assert result["reason"] == "takeover_requires_frontend_resubmit"
    assert result["chat_id"] == chat.id
    assert "上一轮任务似乎中途停住了" in result["text"]
    assert "- run_id: run-continue" in result["text"]
    assert "- 停住阶段: 构建项目：npm" in result["text"]
    assert "- 最后事件详情: npm run build" in result["text"]
    assert "失败归因建议：" in result["text"]
    assert "运行测试：pnpm -> 测试失败 [test]" in result["text"]
    assert "建议: 先重跑同一最小测试" in result["text"]
    assert "最近运行断点：" in result["text"]
    assert "运行测试：pnpm" in result["text"]
    assert "命令: pnpm test -- runtime" in result["text"]
    assert "cwd: /tmp/topic-workspace" in result["text"]
    assert "exit_code: 1" in result["text"]
    assert "失败原因: 断言失败：没有保存续跑草稿" in result["text"]
    assert "文件: console/src/pages/Control/TaskCenter/index.tsx (+12 / -2)" in result[
        "text"
    ]

    events = build_runtime_run_events_response([topic_workspace], "run-continue")
    assert events.events[-1].type == "turn.continue_requested"
    assert events.events[-1].payload["mode"] == "takeover"
    assert events.events[-1].payload["active_run"] is False
    assert events.events[-1].status == "info"


@pytest.mark.asyncio
async def test_runtime_takeover_continue_prepares_new_queued_attempt_contract(
    monkeypatch,
    tmp_path,
):
    runtime_router, workspace, chat, topic_workspace = await _make_workspace(
        tmp_path,
        active_snapshot=None,
    )
    await _write_runtime_contract(
        topic_workspace,
        revision=4,
        status="stalled",
    )

    async def fake_workspace(_request):
        return workspace

    monkeypatch.setattr(runtime_router, "_workspace_for_request", fake_workspace)

    result = await runtime_router.continue_runtime_run(
        SimpleNamespace(),
        "run-continue",
        runtime_router.RuntimeRunContinueRequest(
            expected_attempt_id="run-continue:attempt-1",
            expected_revision=4,
            chat_id=chat.id,
            session_id=chat.session_id,
            after_seq=1,
            stage_label="构建项目：npm",
            workspace_dir=str(topic_workspace),
        ),
    )

    saved = await RuntimeRunStore(topic_workspace).load_contract("run-continue")
    events = build_runtime_run_events_response([topic_workspace], "run-continue")

    assert result["queued"] is False
    assert result["mode"] == "takeover"
    assert result["attempt_prepared"] is True
    assert result["attempt_id"] != "run-continue:attempt-1"
    assert result["previous_attempt_id"] == "run-continue:attempt-1"
    assert result["revision"] == 0
    assert saved is not None
    assert saved.attempt_id == result["attempt_id"]
    assert saved.status == "queued"
    assert saved.revision == 0
    assert getattr(saved, "attempt_reason") == "takeover"
    assert getattr(saved, "previous_attempt_id") == "run-continue:attempt-1"
    assert "上一轮任务似乎中途停住了" in result["text"]
    assert events.events[-1].type == "turn.continue_requested"
    assert events.events[-1].payload["mode"] == "takeover"
    assert events.events[-1].payload["attempt_id"] == result["attempt_id"]
    assert events.events[-1].payload["revision"] == 0


@pytest.mark.asyncio
async def test_runtime_reconnect_mode_does_not_prepare_resubmit_prompt(
    monkeypatch,
    tmp_path,
):
    runtime_router, workspace, chat, topic_workspace = await _make_workspace(
        tmp_path,
        active_snapshot=None,
    )

    async def fake_workspace(_request):
        return workspace

    monkeypatch.setattr(runtime_router, "_workspace_for_request", fake_workspace)

    result = await runtime_router.continue_runtime_run(
        SimpleNamespace(),
        "run-continue",
        runtime_router.RuntimeRunContinueRequest(
            mode="reconnect",
            chat_id=chat.id,
            session_id=chat.session_id,
            workspace_dir=str(topic_workspace),
        ),
    )

    assert result["reconnected"] is False
    assert result["mode"] == "reconnect"
    assert result["reason"] == "reconnect_requires_active_run"
    assert result["text"] is None

    events = build_runtime_run_events_response([topic_workspace], "run-continue")
    assert events.events[-1].type == "turn.continue_requested"
    assert events.events[-1].payload["mode"] == "reconnect"
    assert events.events[-1].payload["active_run"] is False


@pytest.mark.asyncio
async def test_runtime_continue_blocks_takeover_for_ambiguous_tool_effect(
    monkeypatch,
    tmp_path,
):
    """A crash during a tool call must be resolved before chat takeover."""

    runtime_router, workspace, chat, topic_workspace = await _make_workspace(
        tmp_path,
        active_snapshot=None,
    )
    await _write_runtime_contract(
        topic_workspace,
        revision=4,
        status="stalled",
    )
    RuntimeEffectJournal(topic_workspace).append_effect(
        RuntimeEffectRecord(
            run_id="run-continue",
            attempt_id="run-continue:attempt-1",
            step_id="tool-call:call-publish",
            effect_id="effect:call-publish",
            source="chat",
            owner=RuntimeRunOwner(
                user_id="default",
                agent_id="default",
                session_id=chat.session_id,
                workspace_id=str(topic_workspace),
                channel="console",
            ),
            status="running",
            revision=1,
            tool_name="publish_release",
            args_hash="sha256:args",
            policy_snapshot_hash="sha256:policy",
            idempotency_key="run-continue:effect:call-publish",
            release_edition="official",
        ),
    )

    async def fake_workspace(_request):
        return workspace

    monkeypatch.setattr(runtime_router, "_workspace_for_request", fake_workspace)

    result = await runtime_router.continue_runtime_run(
        SimpleNamespace(),
        "run-continue",
        runtime_router.RuntimeRunContinueRequest(
            expected_attempt_id="run-continue:attempt-1",
            expected_revision=4,
            chat_id=chat.id,
            session_id=chat.session_id,
            workspace_dir=str(topic_workspace),
        ),
    )

    saved = await RuntimeRunStore(topic_workspace).load_contract("run-continue")
    events = build_runtime_run_events_response([topic_workspace], "run-continue")

    assert result["queued"] is False
    assert result["reconnected"] is False
    assert result["mode"] == "takeover"
    assert result["reason"] == "ambiguous_effect_requires_resolution"
    assert result["text"] is None
    assert result["blocking_effect_ids"] == ["effect:call-publish"]
    assert "attempt_prepared" not in result
    assert saved is not None
    assert saved.attempt_id == "run-continue:attempt-1"
    assert saved.status == "stalled"
    assert saved.revision == 4
    assert events.events[-1].type == "turn.continue_requested"
    assert events.events[-1].payload["reason"] == (
        "ambiguous_effect_requires_resolution"
    )


@pytest.mark.asyncio
async def test_runtime_continue_allows_committed_tool_effect_after_checkpoint(
    monkeypatch,
    tmp_path,
):
    """A checkpointed committed tool call can safely hand off to a new attempt."""

    runtime_router, workspace, chat, topic_workspace = await _make_workspace(
        tmp_path,
        active_snapshot=None,
    )
    await _write_runtime_contract(
        topic_workspace,
        revision=4,
        status="running",
    )
    await append_chat_event(
        workspace_dir=topic_workspace,
        agent_id="default",
        chat_id=chat.id,
        session_id=chat.session_id,
        run_id="run-continue",
        item_id="call-publish",
        kind="chat.tool_call.started",
        status="running",
        summary="开始发布",
        payload={"tool_call_id": "call-publish", "tool_name": "publish_release"},
    )
    completed = await append_chat_event(
        workspace_dir=topic_workspace,
        agent_id="default",
        chat_id=chat.id,
        session_id=chat.session_id,
        run_id="run-continue",
        item_id="call-publish",
        kind="chat.tool_call.finished",
        status="completed",
        summary="发布完成",
        payload={
            "attempt_id": "run-continue:attempt-1",
            "effect_id": "effect:call-publish",
            "tool_call_id": "call-publish",
            "tool_name": "publish_release",
        },
    )
    store = RuntimeRunStore(topic_workspace)
    checkpointed = await store.load_contract("run-continue")

    assert checkpointed is not None
    assert checkpointed.checkpoint is not None
    assert checkpointed.checkpoint.event_id == completed.id
    assert checkpointed.checkpoint.effect_ids == ("effect:call-publish",)

    stalled = checkpointed.model_copy(
        update={"status": "stalled", "revision": checkpointed.revision + 1},
    )
    await store.save_contract(
        stalled,
        expected_revision=checkpointed.revision,
        expected_attempt_id=checkpointed.attempt_id,
    )
    RuntimeEffectJournal(topic_workspace).append_effect(
        RuntimeEffectRecord(
            run_id="run-continue",
            attempt_id="run-continue:attempt-1",
            step_id="tool-call:call-publish",
            effect_id="effect:call-publish",
            source="chat",
            owner=RuntimeRunOwner(
                user_id="default",
                agent_id="default",
                session_id=chat.session_id,
                workspace_id=str(topic_workspace),
                channel="console",
            ),
            status="committed",
            revision=1,
            tool_name="publish_release",
            args_hash="sha256:args",
            policy_snapshot_hash="sha256:policy",
            idempotency_key="run-continue:effect:call-publish",
            release_edition="official",
        ),
    )

    async def fake_workspace(_request):
        return workspace

    monkeypatch.setattr(runtime_router, "_workspace_for_request", fake_workspace)

    result = await runtime_router.continue_runtime_run(
        SimpleNamespace(),
        "run-continue",
        runtime_router.RuntimeRunContinueRequest(
            expected_attempt_id=stalled.attempt_id,
            expected_revision=stalled.revision,
            chat_id=chat.id,
            session_id=chat.session_id,
            workspace_dir=str(topic_workspace),
        ),
    )

    saved = await store.load_contract("run-continue")

    assert result["reason"] == "takeover_requires_frontend_resubmit"
    assert result["attempt_prepared"] is True
    assert saved is not None
    assert saved.attempt_id == result["attempt_id"]
    assert saved.status == "queued"


@pytest.mark.asyncio
async def test_runtime_continue_requires_checkpoint_for_each_committed_effect(
    monkeypatch,
    tmp_path,
):
    """One completed tool cannot acknowledge a later independent effect."""

    runtime_router, workspace, chat, topic_workspace = await _make_workspace(
        tmp_path,
        active_snapshot=None,
    )
    await _write_runtime_contract(topic_workspace, revision=4, status="stalled")
    store = RuntimeRunStore(topic_workspace)
    contract = await store.load_contract("run-continue")

    assert contract is not None
    checkpointed = contract.model_copy(
        update={
            "revision": 5,
            "checkpoint": RuntimeCheckpoint(
                checkpoint_id="event:earlier-effect",
                event_id="event-earlier",
                seq=8,
                effect_ids=("effect:earlier",),
                summary="较早工具已完成",
            ),
        },
    )
    await store.save_contract(
        checkpointed,
        expected_revision=contract.revision,
        expected_attempt_id=contract.attempt_id,
    )
    RuntimeEffectJournal(topic_workspace).append_effect(
        RuntimeEffectRecord(
            run_id="run-continue",
            attempt_id="run-continue:attempt-1",
            step_id="tool-call:call-publish",
            effect_id="effect:call-publish",
            source="chat",
            owner=RuntimeRunOwner(
                user_id="default",
                agent_id="default",
                session_id=chat.session_id,
                workspace_id=str(topic_workspace),
                channel="console",
            ),
            status="committed",
            revision=1,
            tool_name="publish_release",
            args_hash="sha256:args",
            policy_snapshot_hash="sha256:policy",
            idempotency_key="run-continue:effect:call-publish",
            release_edition="official",
        ),
    )

    async def fake_workspace(_request):
        return workspace

    monkeypatch.setattr(runtime_router, "_workspace_for_request", fake_workspace)

    result = await runtime_router.continue_runtime_run(
        SimpleNamespace(),
        "run-continue",
        runtime_router.RuntimeRunContinueRequest(
            expected_attempt_id=checkpointed.attempt_id,
            expected_revision=checkpointed.revision,
            chat_id=chat.id,
            session_id=chat.session_id,
            workspace_dir=str(topic_workspace),
        ),
    )

    assert result["reason"] == "ambiguous_effect_requires_resolution"
    assert result["blocking_effect_ids"] == ["effect:call-publish"]
    assert "attempt_prepared" not in result


@pytest.mark.asyncio
async def test_runtime_continue_reconciles_checkpoint_from_completed_tool_event(
    monkeypatch,
    tmp_path,
):
    """A persisted completion event repairs the narrow post-commit crash window."""

    runtime_router, workspace, chat, topic_workspace = await _make_workspace(
        tmp_path,
        active_snapshot=None,
    )
    await _write_runtime_contract(topic_workspace, revision=4, status="running")
    RuntimeEffectJournal(topic_workspace).append_effect(
        RuntimeEffectRecord(
            run_id="run-continue",
            attempt_id="run-continue:attempt-1",
            step_id="tool-call:call-publish",
            effect_id="effect:call-publish",
            source="chat",
            owner=RuntimeRunOwner(
                user_id="default",
                agent_id="default",
                session_id=chat.session_id,
                workspace_id=str(topic_workspace),
                channel="console",
            ),
            status="committed",
            revision=1,
            tool_name="publish_release",
            args_hash="sha256:args",
            policy_snapshot_hash="sha256:policy",
            idempotency_key="run-continue:effect:call-publish",
            release_edition="official",
        ),
    )
    await append_chat_event(
        workspace_dir=topic_workspace,
        agent_id="default",
        chat_id=chat.id,
        session_id=chat.session_id,
        run_id="run-continue",
        item_id="call-publish",
        kind="chat.tool_call.started",
        status="running",
        summary="开始发布",
        payload={"tool_call_id": "call-publish", "tool_name": "publish_release"},
    )
    completed = await append_chat_event(
        workspace_dir=topic_workspace,
        agent_id="default",
        chat_id=chat.id,
        session_id=chat.session_id,
        run_id="run-continue",
        item_id="call-publish",
        kind="chat.tool_call.finished",
        status="completed",
        summary="发布完成",
        payload={
            "attempt_id": "run-continue:attempt-1",
            "effect_id": "effect:call-publish",
            "tool_call_id": "call-publish",
            "tool_name": "publish_release",
        },
    )
    store = RuntimeRunStore(topic_workspace)
    projected = await store.load_contract("run-continue")

    assert projected is not None
    assert projected.checkpoint is not None
    crashed = projected.model_copy(
        update={
            "status": "stalled",
            "revision": projected.revision + 1,
            "checkpoint": None,
        },
    )
    await store.save_contract(
        crashed,
        expected_revision=projected.revision,
        expected_attempt_id=projected.attempt_id,
    )

    async def fake_workspace(_request):
        return workspace

    monkeypatch.setattr(runtime_router, "_workspace_for_request", fake_workspace)

    result = await runtime_router.continue_runtime_run(
        SimpleNamespace(),
        "run-continue",
        runtime_router.RuntimeRunContinueRequest(
            expected_attempt_id=crashed.attempt_id,
            expected_revision=crashed.revision,
            chat_id=chat.id,
            session_id=chat.session_id,
            workspace_dir=str(topic_workspace),
        ),
    )

    assert result["reason"] == "takeover_requires_frontend_resubmit"
    assert result["attempt_prepared"] is True
    assert result["previous_attempt_id"] == "run-continue:attempt-1"
    assert completed.id


@pytest.mark.asyncio
async def test_runtime_resume_mode_prepares_checkpoint_resume_attempt(
    monkeypatch,
    tmp_path,
):
    runtime_router, workspace, chat, topic_workspace = await _make_workspace(
        tmp_path,
        active_snapshot=None,
    )
    await _write_runtime_contract(
        topic_workspace,
        revision=4,
        status="stalled",
    )

    async def fake_workspace(_request):
        return workspace

    monkeypatch.setattr(runtime_router, "_workspace_for_request", fake_workspace)

    result = await runtime_router.continue_runtime_run(
        SimpleNamespace(),
        "run-continue",
        runtime_router.RuntimeRunContinueRequest(
            mode="resume",
            expected_attempt_id="run-continue:attempt-1",
            expected_revision=4,
            chat_id=chat.id,
            session_id=chat.session_id,
            workspace_dir=str(topic_workspace),
        ),
    )

    saved = await RuntimeRunStore(topic_workspace).load_contract("run-continue")
    events = build_runtime_run_events_response([topic_workspace], "run-continue")

    assert result["queued"] is False
    assert result["reconnected"] is False
    assert result["mode"] == "resume"
    assert result["reason"] == "resume_requires_frontend_resubmit"
    assert result["text"]
    assert result["attempt_prepared"] is True
    assert result["attempt_id"] != "run-continue:attempt-1"
    assert result["previous_attempt_id"] == "run-continue:attempt-1"
    assert saved is not None
    assert saved.attempt_id == result["attempt_id"]
    assert saved.status == "queued"
    assert saved.revision == 0
    assert saved.attempt_reason == "resume"
    assert saved.previous_attempt_id == "run-continue:attempt-1"
    assert events.events[-1].type == "turn.continue_requested"
    assert events.events[-1].payload["mode"] == "resume"
    assert events.events[-1].payload["reason"] == "resume_requires_frontend_resubmit"
    assert events.events[-1].payload["active_run"] is False


@pytest.mark.asyncio
async def test_runtime_retry_records_continue_request_not_started_turn(
    monkeypatch,
    tmp_path,
):
    runtime_router, workspace, chat, topic_workspace = await _make_workspace(
        tmp_path,
        active_snapshot=None,
    )

    async def fake_workspace(_request):
        return workspace

    monkeypatch.setattr(runtime_router, "_workspace_for_request", fake_workspace)

    result = await runtime_router.retry_runtime_run(
        SimpleNamespace(),
        "run-continue",
    )

    assert result["queued"] is False
    assert result["mode"] == "retry"
    assert result["reason"] == "retry_requires_frontend_resubmit"
    assert result["text"] == "请继续实现任务"

    events = build_runtime_run_events_response([topic_workspace], "run-continue")
    assert [event.type for event in events.events] == ["turn.continue_requested"]
    assert events.events[0].payload["mode"] == "retry"
    assert events.events[0].payload["run_id"] == "run-continue"
