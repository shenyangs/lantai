# -*- coding: utf-8 -*-
"""Behavior locks for the unified runtime statistics data."""

from __future__ import annotations

import json
from pathlib import Path

from lantai.agent_stats import service
from lantai.app.experience import ChatExperienceEvent
from lantai.app.runner.models import ChatSpec, ChatsFile
from lantai.token_usage.manager import TokenUsageSummary


def _event(
    *,
    event_id: str,
    ts: str,
    kind: str = "chat.tool_call.finished",
    status: str = "completed",
    payload: dict | None = None,
    usage: dict | None = None,
    run_id: str = "run-1",
) -> ChatExperienceEvent:
    return ChatExperienceEvent(
        id=event_id,
        seq=1,
        ts=ts,
        agent_id="agent-1",
        chat_id="chat-1",
        session_id="session-1",
        run_id=run_id,
        item_id=(payload or {}).get("tool_call_id"),
        kind=kind,
        status=status,
        summary="tool event",
        payload=payload or {},
        usage=usage,
    )


def test_runtime_health_aggregates_tools_failures_and_cache_tokens():
    events = [
        _event(
            event_id="event-1",
            ts="2026-07-26T10:00:00Z",
            payload={
                "tool_call_id": "call-1",
                "tool_name": "read_file",
                "duration_ms": 120,
            },
            usage={"cache_tokens": 300},
        ),
        _event(
            event_id="event-2",
            ts="2026-07-26T10:01:00Z",
            status="failed",
            payload={
                "tool_call_id": "call-2",
                "tool_name": "bash",
                "duration_ms": 80,
                "error": "command failed",
            },
        ),
        _event(
            event_id="event-3",
            ts="2026-07-26T10:02:00Z",
            kind="chat.tool_result",
            payload={
                "tool_call_id": "call-1",
                "tool_name": "read_file",
                "duration_ms": 999,
            },
        ),
        _event(
            event_id="event-outside-range",
            ts="2026-06-01T10:00:00Z",
            payload={"tool_call_id": "call-3", "tool_name": "write_file"},
            usage={"cache_tokens": 999},
        ),
    ]

    health, cache_tokens = service._runtime_health_from_events(  # pylint: disable=protected-access
        events,
        "2026-07-20",
        "2026-07-27",
    )

    assert cache_tokens == 300
    assert health.total_calls == 2
    assert health.completed_calls == 1
    assert health.failed_calls == 1
    assert health.interrupted_calls == 0
    assert health.success_rate == 50.0
    assert health.average_duration_ms == 100
    assert [item.name for item in health.by_tool] == ["bash", "read_file"]
    assert health.by_tool[0].last_error == "command failed"


def test_runtime_health_derives_duration_from_started_event():
    events = [
        _event(
            event_id="start-1",
            ts="2026-08-01T10:00:00Z",
            kind="chat.tool_call.started",
            status="running",
            payload={
                "tool_call_id": "call-1",
                "tool_name": "guanlan_search",
            },
        ),
        _event(
            event_id="finish-1",
            ts="2026-08-01T10:00:03Z",
            payload={
                "tool_call_id": "call-1",
                "tool_name": "guanlan_search",
            },
        ),
    ]

    health, _ = service._runtime_health_from_events(  # pylint: disable=protected-access
        events,
        "2026-08-01",
        "2026-08-01",
    )

    assert health.average_duration_ms == 3_000
    assert health.by_tool[0].average_duration_ms == 3_000


def test_runtime_health_does_not_merge_reused_call_ids_across_runs():
    events = [
        _event(
            event_id="finish-1",
            ts="2026-08-01T10:00:00Z",
            payload={"tool_call_id": "call-1", "tool_name": "search"},
            run_id="run-1",
        ),
        _event(
            event_id="finish-2",
            ts="2026-08-01T10:01:00Z",
            payload={"tool_call_id": "call-1", "tool_name": "search"},
            run_id="run-2",
        ),
    ]

    health, _ = service._runtime_health_from_events(  # pylint: disable=protected-access
        events,
        "2026-08-01",
        "2026-08-01",
    )

    assert health.total_calls == 2


def test_session_content_range_handles_unsorted_memories():
    session_data = {
        "agent": {
            "memory": {
                "memories": [
                    {"timestamp": "2026-09-02T10:00:00Z"},
                    {"timestamp": "2026-08-01T10:00:00Z"},
                    {"timestamp": "2026-09-01T10:00:00Z"},
                ],
            },
        },
    }

    should_skip = service._should_skip_by_content_range(  # pylint: disable=protected-access
        session_data,
        "2026-08-01",
        "2026-08-01",
    )

    assert should_skip is False


async def test_summary_aggregates_topic_ledgers_and_nested_sessions(
    tmp_path: Path,
    monkeypatch,
):
    workspace_dir = tmp_path / "workspace"
    topic_workspace = workspace_dir / "topic-workspaces" / "deleted-chat"
    legacy_workspace = workspace_dir / "legacy-topic"
    outside_workspace = tmp_path / "outside"
    for path in (topic_workspace, legacy_workspace, outside_workspace):
        path.mkdir(parents=True)

    chat = ChatSpec(
        name="统计回归",
        session_id="session-1",
        user_id="user-1",
        channel="console",
        created_at="2026-08-01T09:00:00Z",
        updated_at="2026-08-01T09:00:00Z",
        meta={
            "topic_workspace_dir": str(legacy_workspace),
            "manual_workspace_dir": str(outside_workspace),
        },
    )
    workspace_dir.mkdir(exist_ok=True)
    (workspace_dir / "chats.json").write_text(
        json.dumps(
            ChatsFile(chats=[chat]).model_dump(mode="json"),
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    nested_session = workspace_dir / "sessions" / "console" / "session.json"
    nested_session.parent.mkdir(parents=True)
    nested_session.write_text(
        json.dumps(
            {
                "agent": {
                    "memory": {
                        "memories": [
                            {
                                "timestamp": "2026-08-01T09:01:00Z",
                                "role": "user",
                                "content": "请帮我检索",
                            },
                            {
                                "timestamp": "2026-08-01T09:02:00Z",
                                "role": "assistant",
                                "content": "已经完成",
                            },
                        ],
                    },
                },
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    started = _event(
        event_id="start-1",
        ts="2026-08-01T10:00:00Z",
        kind="chat.tool_call.started",
        status="running",
        payload={"tool_call_id": "call-1", "tool_name": "search"},
    )
    finished = _event(
        event_id="finish-1",
        ts="2026-08-01T10:00:02Z",
        payload={"tool_call_id": "call-1", "tool_name": "search"},
    )
    cache_event = _event(
        event_id="cache-1",
        ts="2026-08-01T10:00:03Z",
        kind="chat.final_answer",
        usage={"cache_tokens": 300},
    )
    failed = _event(
        event_id="finish-2",
        ts="2026-08-01T11:00:00Z",
        status="failed",
        payload={
            "tool_call_id": "call-2",
            "tool_name": "browser",
            "error": "navigation failed",
        },
    )
    by_workspace = {
        workspace_dir.resolve(): [finished, cache_event],
        topic_workspace.resolve(): [started, finished, cache_event],
        legacy_workspace.resolve(): [failed],
    }
    visited: set[Path] = set()

    def fake_collect(path: Path) -> list[ChatExperienceEvent]:
        resolved = Path(path).resolve()
        visited.add(resolved)
        return by_workspace.get(resolved, [])

    class EmptyTokenManager:
        async def get_summary(self, **_kwargs):
            return TokenUsageSummary()

        async def get_details(self, **_kwargs):
            return []

    monkeypatch.setattr(service, "collect_workspace_events", fake_collect)
    monkeypatch.setattr(
        service,
        "get_token_usage_manager",
        lambda: EmptyTokenManager(),
    )
    monkeypatch.setattr(service, "_scan_storage", lambda _path: None)

    summary = await service.AgentStatsService().get_summary(
        workspace_dir,
        start_date=chat.created_at.date(),
        end_date=chat.created_at.date(),
    )

    assert visited == {
        workspace_dir.resolve(),
        topic_workspace.resolve(),
        legacy_workspace.resolve(),
    }
    assert outside_workspace.resolve() not in visited
    assert summary.total_active_sessions == 1
    assert summary.total_messages == 2
    assert summary.total_user_messages == 1
    assert summary.total_assistant_messages == 1
    assert summary.total_tool_calls == 2
    assert summary.total_cache_tokens == 300
    assert summary.tool_health.completed_calls == 1
    assert summary.tool_health.failed_calls == 1
    assert summary.tool_health.success_rate == 50.0
    assert summary.tool_health.average_duration_ms == 2_000
    assert summary.by_date[0].tool_calls == 2


def test_storage_scan_is_bounded_and_categorizes_app_data(
    tmp_path: Path,
    monkeypatch,
):
    working_dir = tmp_path / ".lantai"
    backup_dir = tmp_path / ".lantai.backups"
    workspace_dir = working_dir / "workspaces" / "agent-1"
    (workspace_dir / "sessions").mkdir(parents=True)
    (workspace_dir / ".lantai" / "experience").mkdir(parents=True)
    (working_dir / "cache").mkdir(parents=True)
    (working_dir / "memory").mkdir(parents=True)
    backup_dir.mkdir()

    (workspace_dir / "sessions" / "one.json").write_bytes(b"a" * 10)
    (
        workspace_dir / ".lantai" / "experience" / "events.jsonl"
    ).write_bytes(b"b" * 20)
    (working_dir / "cache" / "preview.bin").write_bytes(b"c" * 30)
    (working_dir / "memory" / "profile.md").write_bytes(b"d" * 40)
    (workspace_dir / "result.html").write_bytes(b"e" * 50)
    (backup_dir / "snapshot.zip").write_bytes(b"f" * 60)

    monkeypatch.setattr(service, "WORKING_DIR", working_dir)
    monkeypatch.setattr(service, "BACKUP_DIR", backup_dir)

    summary = service._scan_storage(workspace_dir)  # pylint: disable=protected-access
    by_key = {item.key: item for item in summary.categories}

    assert summary.used_bytes == 210
    assert summary.root_path == str(working_dir)
    assert summary.workspace_path == str(workspace_dir)
    assert by_key["conversations"].bytes == 10
    assert by_key["runtime"].bytes == 20
    assert by_key["logs_cache"].bytes == 30
    assert by_key["memory"].bytes == 40
    assert by_key["workspace_files"].bytes == 50
    assert by_key["backups"].bytes == 60
    assert by_key["logs_cache"].cleanable is True
    assert summary.truncated is False
