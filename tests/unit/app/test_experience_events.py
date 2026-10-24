# -*- coding: utf-8 -*-
"""Experience event ledger tests."""

from __future__ import annotations

import json

import pytest

import lantai.app.experience.events as experience_events
from lantai.app.experience.events import (
    RuntimeEventSequenceAppendError,
    _event_index_path,
    _event_segments_manifest_path,
    _iter_events_from_file_after_seq,
    append_chat_event,
    build_runtime_events_response,
    build_runtime_run_events_response,
    build_turn_event_preview,
    events_path_for_workspace,
    legacy_events_path_for_workspace,
    resolve_effective_workspace_dir_from_meta,
)
from lantai.app.experience.schemas import ChatExperienceEvent
from lantai.runtime.sanitizer import RuntimePayloadSanitizationError


@pytest.mark.asyncio
async def test_experience_events_append_and_keep_recent_order(tmp_path):
    workspace_dir = tmp_path / "topic-workspace"

    for index in range(8):
        await append_chat_event(
            workspace_dir=workspace_dir,
            agent_id="default",
            chat_id="chat-1",
            session_id="console:test",
            run_id=f"run-{index}",
            item_id=f"item-{index}",
            kind="chat.turn.started",
            status="running",
            summary="本轮已开始",
            payload={
                "text_preview": f"message {index}",
                "client_turn_id": f"client-turn-{index}",
                "turn_id": f"turn-{index}",
            },
        )

    preview = build_turn_event_preview(workspace_dir, "chat-1")

    assert preview is not None
    assert preview.summary_line == "本轮已开始"
    assert len(preview.events) == 6
    assert preview.events[0].detail.endswith("message 2")
    assert preview.events[-1].detail.endswith("message 7")
    assert preview.events[-1].session_id == "console:test"
    assert preview.events[-1].run_id == "run-7"
    assert preview.events[-1].client_turn_id == "client-turn-7"
    assert preview.events[-1].turn_id == "turn-7"
    assert preview.events[-1].item_id == "item-7"
    assert preview.events[-1].payload is not None
    assert preview.events[-1].payload["client_turn_id"] == "client-turn-7"
    serialized_event = preview.model_dump(mode="json")["events"][-1]
    assert serialized_event["client_turn_id"] == "client-turn-7"


@pytest.mark.asyncio
async def test_experience_preview_summary_rules(tmp_path):
    workspace_dir = tmp_path / "topic-workspace"

    await append_chat_event(
        workspace_dir=workspace_dir,
        agent_id="default",
        chat_id="chat-2",
        session_id="console:test",
        kind="chat.topic_workspace.bound",
        status="info",
        summary="已绑定当前工作区",
        payload={
            "binding_mode": "topic",
            "effective_workspace_dir": str(workspace_dir),
        },
    )
    preview = build_turn_event_preview(workspace_dir, "chat-2")
    assert preview is not None
    assert preview.summary_line == "已绑定当前工作区"

    await append_chat_event(
        workspace_dir=workspace_dir,
        agent_id="default",
        chat_id="chat-2",
        session_id="console:test",
        kind="chat.turn.stop_requested",
        status="interrupted",
        summary="本轮已请求停止",
        payload={"requested_chat_id": "chat-2", "resolved_chat_id": "chat-2"},
    )
    preview = build_turn_event_preview(workspace_dir, "chat-2")
    assert preview is not None
    assert preview.summary_line == "本轮已请求停止"

    await append_chat_event(
        workspace_dir=workspace_dir,
        agent_id="default",
        chat_id="chat-2",
        session_id="console:test",
        run_id="run-1",
        kind="chat.turn.settled",
        status="finished",
        summary="本轮已结束",
        payload={
            "final_task_status": "finished",
            "pending_turn_cleared": True,
            "active_run_event_count": 3,
        },
    )
    preview = build_turn_event_preview(workspace_dir, "chat-2")
    assert preview is not None
    assert preview.summary_line == "本轮已结束"


def test_experience_preview_returns_none_without_matching_events(tmp_path):
    preview = build_turn_event_preview(tmp_path / "missing", "chat-none")
    assert preview is None


@pytest.mark.asyncio
async def test_append_chat_event_redacts_sensitive_payload_before_write(tmp_path):
    workspace_dir = tmp_path / "topic-workspace"

    await append_chat_event(
        workspace_dir=workspace_dir,
        agent_id="default",
        chat_id="chat-redaction",
        session_id="console:redaction",
        run_id="run-redaction",
        kind="chat.turn.started",
        status="running",
        summary="本轮已开始",
        payload={
            "text_preview": "hello",
            "authorization": "Bearer super-secret-token",
            "request": {
                "headers": {
                    "cookie": "sid=secret-cookie",
                    "x-safe": "ok",
                },
            },
        },
    )

    event_path = events_path_for_workspace(workspace_dir)
    raw_text = event_path.read_text(encoding="utf-8")
    stored = json.loads(raw_text.splitlines()[0])
    payload = stored["payload"]

    assert "super-secret-token" not in raw_text
    assert "secret-cookie" not in raw_text
    assert "authorization" not in payload
    assert payload["request"]["headers"]["cookie"]["redacted"] is True
    assert payload["redaction_version"] == 1
    assert payload["data_classification"] == "sensitive"
    assert payload["redactions"][0]["path"] == "authorization"


@pytest.mark.asyncio
async def test_append_chat_event_fails_closed_for_non_json_payload(tmp_path):
    workspace_dir = tmp_path / "topic-workspace"

    with pytest.raises(RuntimePayloadSanitizationError):
        await append_chat_event(
            workspace_dir=workspace_dir,
            agent_id="default",
            chat_id="chat-redaction",
            session_id="console:redaction",
            run_id="run-redaction",
            kind="chat.turn.started",
            status="running",
            summary="本轮已开始",
            payload={"text_preview": object()},
        )

    assert not events_path_for_workspace(workspace_dir).exists()


@pytest.mark.asyncio
async def test_event_ledger_index_tracks_next_seq_and_offsets(tmp_path):
    workspace_dir = tmp_path / "topic-workspace"

    await append_chat_event(
        workspace_dir=workspace_dir,
        agent_id="default",
        chat_id="chat-index",
        session_id="console:index",
        run_id="run-index-1",
        kind="chat.turn.started",
        status="running",
        summary="本轮已开始",
        payload={"text_preview": "first"},
    )
    await append_chat_event(
        workspace_dir=workspace_dir,
        agent_id="default",
        chat_id="chat-index",
        session_id="console:index",
        run_id="run-index-2",
        kind="chat.turn.started",
        status="running",
        summary="本轮已开始",
        payload={"text_preview": "second"},
    )

    event_path = events_path_for_workspace(workspace_dir)
    index = json.loads(_event_index_path(event_path).read_text(encoding="utf-8"))

    assert index["schema_version"] == 1
    assert index["latest_seq"] == 2
    assert index["next_seq"] == 3
    assert index["ledger_size"] == event_path.stat().st_size
    assert index["offset_by_seq"]["1"] == 0
    assert isinstance(index["offset_by_seq"]["2"], int)
    assert index["offset_by_seq"]["2"] > index["offset_by_seq"]["1"]


@pytest.mark.asyncio
async def test_consecutive_appends_reuse_the_fresh_process_index(
    tmp_path,
    monkeypatch,
):
    workspace_dir = tmp_path / "topic-workspace"
    await append_chat_event(
        workspace_dir=workspace_dir,
        agent_id="default",
        chat_id="chat-index-cache",
        session_id="console:index-cache",
        run_id="run-index-cache-1",
        kind="chat.turn.started",
        status="running",
        summary="本轮已开始",
        payload={"text_preview": "first"},
    )

    index_path = _event_index_path(events_path_for_workspace(workspace_dir)).resolve()
    original_read = experience_events.read_text_no_follow
    original_rebuild = experience_events._rebuild_event_index

    def reject_fresh_index_read(path):
        if path.resolve() == index_path:
            raise AssertionError("fresh process index was read back from disk")
        return original_read(path)

    monkeypatch.setattr(
        experience_events,
        "read_text_no_follow",
        reject_fresh_index_read,
    )

    def reject_fresh_index_rebuild(path):
        if path.resolve() == events_path_for_workspace(workspace_dir).resolve():
            raise AssertionError("fresh process index was rebuilt from the ledger")
        return original_rebuild(path)

    monkeypatch.setattr(
        experience_events,
        "_rebuild_event_index",
        reject_fresh_index_rebuild,
    )

    event = await append_chat_event(
        workspace_dir=workspace_dir,
        agent_id="default",
        chat_id="chat-index-cache",
        session_id="console:index-cache",
        run_id="run-index-cache-2",
        kind="chat.turn.started",
        status="running",
        summary="本轮已开始",
        payload={"text_preview": "second"},
    )

    assert event.seq == 2


@pytest.mark.asyncio
async def test_append_uses_runtime_projection_after_legacy_ledger(tmp_path):
    workspace_dir = tmp_path / "topic-workspace"
    legacy_path = legacy_events_path_for_workspace(workspace_dir)
    legacy_path.parent.mkdir(parents=True)
    legacy = ChatExperienceEvent(
        id="legacy-event-1",
        seq=7,
        ts="2026-01-01T00:00:00Z",
        agent_id="default",
        chat_id="chat-migrated",
        session_id="console:migrated",
        run_id="run-migrated",
        kind="chat.turn.started",
        status="running",
        source="user",
        summary="legacy started",
        payload={"text_preview": "from legacy ledger"},
    )
    legacy_path.write_text(
        json.dumps(legacy.model_dump(mode="json"), ensure_ascii=False) + "\n",
        encoding="utf-8",
    )

    event = await append_chat_event(
        workspace_dir=workspace_dir,
        agent_id="default",
        chat_id="chat-migrated",
        session_id="console:migrated",
        run_id="run-migrated",
        kind="chat.final_answer",
        status="completed",
        summary="canonical completed",
        payload={"text_preview": "from runtime projection"},
    )

    assert event.seq == 8
    assert events_path_for_workspace(workspace_dir).is_file()
    assert legacy_path.is_file()

    response = build_runtime_events_response(workspace_dir, "chat-migrated")
    assert [item.seq for item in response.events] == [7, 8]
    assert response.events[0].payload["summary"] == "legacy started"
    assert response.events[1].payload["summary"] == "canonical completed"

    first_page = build_runtime_events_response(
        workspace_dir,
        "chat-migrated",
        limit=1,
    )
    second_page = build_runtime_events_response(
        workspace_dir,
        "chat-migrated",
        after_seq=first_page.next_after_seq or 0,
        limit=1,
    )

    assert [item.seq for item in first_page.events] == [7]
    assert first_page.has_more is True
    assert first_page.next_after_seq == 7
    assert [item.seq for item in second_page.events] == [8]
    assert second_page.has_more is False
    assert second_page.next_after_seq == 8


@pytest.mark.asyncio
async def test_event_ledger_reader_rebuilds_stale_index_without_persisting(tmp_path):
    workspace_dir = tmp_path / "topic-workspace"

    await append_chat_event(
        workspace_dir=workspace_dir,
        agent_id="default",
        chat_id="chat-index",
        session_id="console:index",
        run_id="run-index-1",
        kind="chat.turn.started",
        status="running",
        summary="本轮已开始",
        payload={"text_preview": "first"},
    )
    await append_chat_event(
        workspace_dir=workspace_dir,
        agent_id="default",
        chat_id="other-chat",
        session_id="console:index",
        run_id="run-index-2",
        kind="chat.turn.started",
        status="running",
        summary="本轮已开始",
        payload={"text_preview": "other"},
    )
    await append_chat_event(
        workspace_dir=workspace_dir,
        agent_id="default",
        chat_id="chat-index",
        session_id="console:index",
        run_id="run-index-3",
        kind="chat.turn.started",
        status="running",
        summary="本轮已开始",
        payload={"text_preview": "third"},
    )

    event_path = events_path_for_workspace(workspace_dir)
    _event_index_path(event_path).write_text(
        json.dumps(
            {
                "schema_version": 1,
                "writer_version": "test/stale",
                "ledger_size": 0,
                "line_count": 0,
                "latest_seq": 999,
                "next_seq": 1000,
                "offset_by_seq": {"999": 0},
            },
        ),
        encoding="utf-8",
    )

    response = build_runtime_events_response(
        workspace_dir,
        "chat-index",
        after_seq=1,
    )
    reader_sidecar = json.loads(
        _event_index_path(event_path).read_text(encoding="utf-8")
    )
    after_first = _iter_events_from_file_after_seq(event_path, 1)

    assert [event.seq for event in response.events] == [3]
    assert response.latest_seq == 3
    assert reader_sidecar["writer_version"] == "test/stale"
    assert reader_sidecar["latest_seq"] == 999
    assert [event.seq for event in after_first] == [2, 3]

    appended = await append_chat_event(
        workspace_dir=workspace_dir,
        agent_id="default",
        chat_id="chat-index",
        session_id="console:index",
        run_id="run-index-4",
        kind="chat.turn.started",
        status="running",
        summary="writer repairs the sidecar",
        payload={"text_preview": "fourth"},
    )
    writer_sidecar = json.loads(
        _event_index_path(event_path).read_text(encoding="utf-8")
    )
    assert appended.seq == 4
    assert writer_sidecar["latest_seq"] == 4
    assert writer_sidecar["next_seq"] == 5
    assert writer_sidecar["ledger_size"] == event_path.stat().st_size


@pytest.mark.asyncio
async def test_event_ledger_rebuild_preserves_duplicate_seq_boundary(tmp_path):
    workspace_dir = tmp_path / "topic-workspace"

    await append_chat_event(
        workspace_dir=workspace_dir,
        agent_id="default",
        chat_id="chat-index",
        session_id="console:index",
        run_id="run-index-1",
        kind="chat.turn.started",
        status="running",
        summary="first",
        payload={"text_preview": "first"},
    )
    second = await append_chat_event(
        workspace_dir=workspace_dir,
        agent_id="default",
        chat_id="chat-index",
        session_id="console:index",
        run_id="run-index-2",
        kind="chat.turn.started",
        status="running",
        summary="second",
        payload={"text_preview": "second"},
    )

    duplicate = second.model_copy(
        update={
            "id": "event-duplicate-seq",
            "summary": "duplicate",
            "payload": {"text_preview": "duplicate"},
        },
    )
    event_path = events_path_for_workspace(workspace_dir)
    with event_path.open("a", encoding="utf-8") as handle:
        handle.write(
            json.dumps(duplicate.model_dump(mode="json"), ensure_ascii=False) + "\n",
        )

    response = build_runtime_events_response(
        workspace_dir,
        "chat-index",
        after_seq=1,
    )
    reader_sidecar = json.loads(
        _event_index_path(event_path).read_text(encoding="utf-8")
    )

    assert [event.payload["summary"] for event in response.events] == [
        "second",
        "duplicate",
    ]
    assert [event.seq for event in response.events] == [2, 2]
    assert response.next_after_seq == 2
    assert reader_sidecar["offset_by_seq"]["2"] < event_path.stat().st_size

    page = build_runtime_events_response(
        workspace_dir,
        "chat-index",
        after_seq=1,
        limit=1,
    )
    assert [event.payload["summary"] for event in page.events] == [
        "second",
        "duplicate",
    ]
    assert page.has_more is False
    assert page.next_after_seq == 2

    run_page = build_runtime_run_events_response(
        [workspace_dir],
        "run-index-2",
        after_seq=1,
        limit=1,
    )
    assert [event.payload["summary"] for event in run_page.events] == [
        "second",
        "duplicate",
    ]
    assert run_page.has_more is False
    assert run_page.next_after_seq == 2


@pytest.mark.asyncio
async def test_event_ledger_rebuilds_legacy_duplicate_seq_index(tmp_path):
    workspace_dir = tmp_path / "topic-workspace"

    await append_chat_event(
        workspace_dir=workspace_dir,
        agent_id="default",
        chat_id="chat-index",
        session_id="console:index",
        run_id="run-index-1",
        kind="chat.turn.started",
        status="running",
        summary="first",
        payload={"text_preview": "first"},
    )
    second = await append_chat_event(
        workspace_dir=workspace_dir,
        agent_id="default",
        chat_id="chat-index",
        session_id="console:index",
        run_id="run-index-2",
        kind="chat.turn.started",
        status="running",
        summary="second",
        payload={"text_preview": "second"},
    )
    duplicate = second.model_copy(
        update={
            "id": "event-duplicate-seq",
            "summary": "duplicate",
            "payload": {"text_preview": "duplicate"},
        },
    )
    event_path = events_path_for_workspace(workspace_dir)
    with event_path.open("a", encoding="utf-8") as handle:
        duplicate_offset = handle.tell()
        handle.write(
            json.dumps(duplicate.model_dump(mode="json"), ensure_ascii=False) + "\n",
        )
    _event_index_path(event_path).write_text(
        json.dumps(
            {
                "schema_version": 1,
                "writer_version": "lantai-experience-events-index/1",
                "ledger_size": event_path.stat().st_size,
                "line_count": 3,
                "latest_seq": 2,
                "next_seq": 3,
                "offset_by_seq": {"1": 0, "2": duplicate_offset},
                "offsets_by_turn_id": {
                    "run-index-1": [0],
                    "run-index-2": [duplicate_offset],
                },
            },
        ),
        encoding="utf-8",
    )

    response = build_runtime_events_response(
        workspace_dir,
        "chat-index",
        after_seq=1,
    )
    reader_sidecar = json.loads(
        _event_index_path(event_path).read_text(encoding="utf-8")
    )

    assert [event.payload["summary"] for event in response.events] == [
        "second",
        "duplicate",
    ]
    assert reader_sidecar["writer_version"] == "lantai-experience-events-index/1"

    appended = await append_chat_event(
        workspace_dir=workspace_dir,
        agent_id="default",
        chat_id="chat-index",
        session_id="console:index",
        run_id="run-index-3",
        kind="chat.turn.started",
        status="running",
        summary="third",
        payload={"text_preview": "third"},
    )
    writer_sidecar = json.loads(
        _event_index_path(event_path).read_text(encoding="utf-8")
    )
    assert appended.seq == 3
    assert writer_sidecar["writer_version"] != "lantai-experience-events-index/1"
    assert writer_sidecar["offset_by_seq"]["2"] < duplicate_offset


@pytest.mark.asyncio
async def test_runtime_events_response_paginates_by_cursor(tmp_path):
    workspace_dir = tmp_path / "topic-workspace"

    for index in range(3):
        await append_chat_event(
            workspace_dir=workspace_dir,
            agent_id="default",
            chat_id="chat-paged",
            session_id="console:paged",
            run_id=f"run-paged-{index}",
            kind="chat.turn.started",
            status="running",
            summary="本轮已开始",
            payload={"text_preview": f"page {index}"},
        )

    first_page = build_runtime_events_response(
        workspace_dir,
        "chat-paged",
        limit=2,
    )
    second_page = build_runtime_events_response(
        workspace_dir,
        "chat-paged",
        after_seq=first_page.next_after_seq or 0,
        limit=2,
    )

    assert [event.seq for event in first_page.events] == [1, 2]
    assert first_page.latest_seq == 2
    assert first_page.next_after_seq == 2
    assert first_page.has_more is True
    assert [event.seq for event in second_page.events] == [3]
    assert second_page.latest_seq == 3
    assert second_page.next_after_seq == 3
    assert second_page.has_more is False


@pytest.mark.asyncio
async def test_runtime_run_events_response_filters_attempt_id(tmp_path):
    workspace_dir = tmp_path / "topic-workspace"

    await append_chat_event(
        workspace_dir=workspace_dir,
        agent_id="default",
        chat_id="chat-attempt",
        session_id="console:attempt",
        run_id="run-attempt",
        kind="chat.turn.started",
        status="running",
        summary="attempt a started",
        payload={"turn_id": "turn-a", "attempt_id": "attempt-a"},
    )
    await append_chat_event(
        workspace_dir=workspace_dir,
        agent_id="default",
        chat_id="chat-attempt",
        session_id="console:attempt",
        run_id="run-attempt",
        kind="chat.turn.settled",
        status="finished",
        summary="attempt a finished",
        payload={
            "turn_id": "turn-a",
            "attempt_id": "attempt-a",
            "final_task_status": "finished",
            "pending_turn_cleared": True,
            "active_run_event_count": 2,
        },
    )
    await append_chat_event(
        workspace_dir=workspace_dir,
        agent_id="default",
        chat_id="chat-attempt",
        session_id="console:attempt",
        run_id="run-attempt",
        kind="chat.turn.started",
        status="running",
        summary="attempt b started",
        payload={"turn_id": "turn-b", "attempt_id": "attempt-b"},
    )

    response = build_runtime_run_events_response(
        [workspace_dir],
        "run-attempt",
        attempt_id="attempt-a",
        limit=1,
    )

    assert response.attempt_id == "attempt-a"
    assert [event.seq for event in response.events] == [1]
    assert response.next_after_seq == 1
    assert response.has_more is True


@pytest.mark.asyncio
async def test_event_ledger_rolls_segments_and_reads_across_them(
    tmp_path,
    monkeypatch,
):
    workspace_dir = tmp_path / "topic-workspace"
    monkeypatch.setattr(experience_events, "EVENT_LEDGER_SEGMENT_MAX_BYTES", 650)

    for index in range(6):
        await append_chat_event(
            workspace_dir=workspace_dir,
            agent_id="default",
            chat_id="chat-segmented",
            session_id="console:segmented",
            run_id=f"run-segmented-{index}",
            kind="chat.turn.started",
            status="running",
            summary="本轮已开始",
            payload={"text_preview": f"segment {index}", "trace_detail": "x" * 80},
        )

    event_path = events_path_for_workspace(workspace_dir)
    manifest = json.loads(
        _event_segments_manifest_path(event_path).read_text(encoding="utf-8")
    )
    full_response = build_runtime_events_response(workspace_dir, "chat-segmented")
    first_page = build_runtime_events_response(
        workspace_dir,
        "chat-segmented",
        limit=4,
    )
    second_page = build_runtime_events_response(
        workspace_dir,
        "chat-segmented",
        after_seq=first_page.next_after_seq or 0,
        limit=4,
    )
    final_event = await append_chat_event(
        workspace_dir=workspace_dir,
        agent_id="default",
        chat_id="chat-segmented",
        session_id="console:segmented",
        run_id="run-segmented-final",
        kind="chat.turn.started",
        status="running",
        summary="本轮已开始",
        payload={"text_preview": "final"},
    )

    assert manifest["segments"]
    assert [event.seq for event in full_response.events] == [1, 2, 3, 4, 5, 6]
    assert [event.seq for event in first_page.events] == [1, 2, 3, 4]
    assert first_page.has_more is True
    assert [event.seq for event in second_page.events] == [5, 6]
    assert second_page.has_more is False
    assert final_event.seq == 7


@pytest.mark.asyncio
async def test_append_sequence_validation_uses_turn_index(tmp_path, monkeypatch):
    workspace_dir = tmp_path / "topic-workspace"

    await append_chat_event(
        workspace_dir=workspace_dir,
        agent_id="default",
        chat_id="chat-turn-index",
        session_id="console:turn-index",
        run_id="run-turn-index",
        kind="chat.turn.started",
        status="running",
        summary="本轮已开始",
        payload={"text_preview": "indexed"},
    )

    def fail_full_scan(_path):
        raise AssertionError("append validation should use the turn offset index")

    monkeypatch.setattr(experience_events, "_iter_events_from_file", fail_full_scan)

    event = await append_chat_event(
        workspace_dir=workspace_dir,
        agent_id="default",
        chat_id="chat-turn-index",
        session_id="console:turn-index",
        run_id="run-turn-index",
        kind="chat.turn.settled",
        status="finished",
        summary="本轮已结束",
        payload={
            "final_task_status": "finished",
            "pending_turn_cleared": True,
            "active_run_event_count": 2,
        },
    )

    assert event.seq == 2


@pytest.mark.asyncio
async def test_new_turn_append_does_not_load_rolled_segment_indexes(
    tmp_path,
    monkeypatch,
):
    workspace_dir = tmp_path / "topic-workspace"
    monkeypatch.setattr(experience_events, "EVENT_LEDGER_SEGMENT_MAX_BYTES", 650)

    for index in range(6):
        await append_chat_event(
            workspace_dir=workspace_dir,
            agent_id="default",
            chat_id="chat-segment-hot-path",
            session_id="console:segment-hot-path",
            run_id=f"run-segment-hot-path-{index}",
            kind="chat.turn.started",
            status="running",
            summary="本轮已开始",
            payload={"text_preview": f"segment {index}", "trace_detail": "x" * 80},
        )

    event_path = events_path_for_workspace(workspace_dir)
    manifest = json.loads(
        _event_segments_manifest_path(event_path).read_text(encoding="utf-8")
    )
    rolled_paths = {
        event_path.with_name(item["path"]).resolve()
        for item in manifest["segments"]
    }
    assert rolled_paths

    original_load = experience_events._load_or_rebuild_event_index

    def reject_rolled_index_load(path, **kwargs):
        if path.resolve() in rolled_paths:
            raise AssertionError("new-turn append loaded a rolled segment index")
        return original_load(path, **kwargs)

    monkeypatch.setattr(
        experience_events,
        "_load_or_rebuild_event_index",
        reject_rolled_index_load,
    )

    event = await append_chat_event(
        workspace_dir=workspace_dir,
        agent_id="default",
        chat_id="chat-segment-hot-path",
        session_id="console:segment-hot-path",
        run_id="run-segment-hot-path-final",
        kind="chat.turn.started",
        status="running",
        summary="本轮已开始",
        payload={"text_preview": "final"},
    )

    assert event.seq == 7


@pytest.mark.asyncio
async def test_runtime_event_tail_skips_segments_before_cursor(tmp_path, monkeypatch):
    workspace_dir = tmp_path / "topic-workspace"
    monkeypatch.setattr(experience_events, "EVENT_LEDGER_SEGMENT_MAX_BYTES", 650)

    for index in range(8):
        await append_chat_event(
            workspace_dir=workspace_dir,
            agent_id="default",
            chat_id="chat-tail-hot-path",
            session_id="console:tail-hot-path",
            run_id=f"run-tail-hot-path-{index}",
            kind="chat.turn.started",
            status="running",
            summary="本轮已开始",
            payload={"text_preview": f"segment {index}", "trace_detail": "x" * 80},
        )

    event_path = events_path_for_workspace(workspace_dir)
    manifest = json.loads(
        _event_segments_manifest_path(event_path).read_text(encoding="utf-8")
    )
    after_seq = manifest["segments"][-1]["latest_seq"]
    skipped_paths = {
        event_path.with_name(item["path"]).resolve()
        for item in manifest["segments"]
        if item["latest_seq"] <= after_seq
    }
    assert skipped_paths

    original_load = experience_events._load_or_rebuild_event_index

    def reject_skipped_index_load(path, **kwargs):
        if path.resolve() in skipped_paths:
            raise AssertionError("tail loaded a segment entirely before its cursor")
        return original_load(path, **kwargs)

    monkeypatch.setattr(
        experience_events,
        "_load_or_rebuild_event_index",
        reject_skipped_index_load,
    )

    response = build_runtime_events_response(
        workspace_dir,
        "chat-tail-hot-path",
        after_seq=after_seq,
    )

    assert all(event.seq > after_seq for event in response.events)


@pytest.mark.asyncio
async def test_append_chat_event_rejects_side_effect_after_terminal(tmp_path):
    workspace_dir = tmp_path / "topic-workspace"

    await append_chat_event(
        workspace_dir=workspace_dir,
        agent_id="default",
        chat_id="chat-sequence",
        session_id="console:sequence",
        run_id="run-sequence",
        kind="chat.turn.started",
        status="running",
        summary="本轮已开始",
        payload={"text_preview": "hello"},
    )
    await append_chat_event(
        workspace_dir=workspace_dir,
        agent_id="default",
        chat_id="chat-sequence",
        session_id="console:sequence",
        run_id="run-sequence",
        kind="chat.turn.settled",
        status="finished",
        summary="本轮已结束",
        payload={
            "final_task_status": "finished",
            "pending_turn_cleared": True,
            "active_run_event_count": 2,
        },
    )

    with pytest.raises(RuntimeEventSequenceAppendError) as exc_info:
        await append_chat_event(
            workspace_dir=workspace_dir,
            agent_id="default",
            chat_id="chat-sequence",
            session_id="console:sequence",
            run_id="run-sequence",
            item_id="tool-late",
            kind="chat.tool_call.started",
            status="running",
            summary="不应再调用工具",
            payload={"tool_name": "late_tool", "arguments": {}},
        )

    assert exc_info.value.codes == ["side_effect_after_terminal"]
    response = build_runtime_events_response(workspace_dir, "chat-sequence")
    assert [event.type for event in response.events] == [
        "turn.started",
        "turn.completed",
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("event_kwargs", "expected_codes"),
    [
        (
            {
                "item_id": "tool-orphan",
                "kind": "chat.tool_call.finished",
                "status": "completed",
                "summary": "孤儿工具完成",
                "payload": {"tool_name": "orphan", "ok": True},
            },
            ["tool_completed_without_start"],
        ),
        (
            {
                "item_id": "tool-bad-args",
                "kind": "chat.tool_call.started",
                "status": "running",
                "summary": "坏工具参数",
                "payload": {"tool_name": "bad", "arguments": ["not", "object"]},
            },
            ["tool_arguments_not_object"],
        ),
        (
            {
                "item_id": "approval-orphan",
                "kind": "chat.approval.resolved",
                "status": "completed",
                "summary": "孤儿审批解决",
                "payload": {
                    "request_id": "approval-orphan",
                    "decision": "approved",
                },
            },
            ["approval_resolved_without_request"],
        ),
    ],
)
async def test_append_chat_event_rejects_unrepairable_sequence_errors_immediately(
    tmp_path,
    event_kwargs,
    expected_codes,
):
    workspace_dir = tmp_path / "topic-workspace"

    await append_chat_event(
        workspace_dir=workspace_dir,
        agent_id="default",
        chat_id="chat-sequence",
        session_id="console:sequence",
        run_id="run-unrepairable",
        kind="chat.turn.started",
        status="running",
        summary="本轮已开始",
        payload={"text_preview": "hello"},
    )

    with pytest.raises(RuntimeEventSequenceAppendError) as exc_info:
        await append_chat_event(
            workspace_dir=workspace_dir,
            agent_id="default",
            chat_id="chat-sequence",
            session_id="console:sequence",
            run_id="run-unrepairable",
            **event_kwargs,
        )

    assert exc_info.value.codes == expected_codes
    response = build_runtime_events_response(workspace_dir, "chat-sequence")
    assert [event.type for event in response.events] == ["turn.started"]


@pytest.mark.asyncio
async def test_append_chat_event_rejects_terminal_with_open_tool_call(tmp_path):
    workspace_dir = tmp_path / "topic-workspace"

    await append_chat_event(
        workspace_dir=workspace_dir,
        agent_id="default",
        chat_id="chat-sequence",
        session_id="console:sequence",
        run_id="run-open-tool",
        kind="chat.turn.started",
        status="running",
        summary="本轮已开始",
        payload={"text_preview": "hello"},
    )
    await append_chat_event(
        workspace_dir=workspace_dir,
        agent_id="default",
        chat_id="chat-sequence",
        session_id="console:sequence",
        run_id="run-open-tool",
        item_id="tool-open",
        kind="chat.tool_call.started",
        status="running",
        summary="开始调用工具",
        payload={"tool_name": "search", "arguments": {}},
    )

    with pytest.raises(RuntimeEventSequenceAppendError) as exc_info:
        await append_chat_event(
            workspace_dir=workspace_dir,
            agent_id="default",
            chat_id="chat-sequence",
            session_id="console:sequence",
            run_id="run-open-tool",
            kind="chat.turn.settled",
            status="finished",
            summary="本轮已结束",
            payload={
                "final_task_status": "finished",
                "pending_turn_cleared": True,
                "active_run_event_count": 3,
            },
        )

    assert exc_info.value.codes == ["tool_started_without_completion"]
    response = build_runtime_events_response(workspace_dir, "chat-sequence")
    assert [event.type for event in response.events] == [
        "turn.started",
        "tool.started",
    ]


@pytest.mark.asyncio
async def test_replay_lifecycle_does_not_block_turn_terminal(tmp_path):
    workspace_dir = tmp_path / "topic-workspace"

    await append_chat_event(
        workspace_dir=workspace_dir,
        agent_id="default",
        chat_id="chat-replay",
        session_id="console:replay",
        run_id="run-replay",
        kind="chat.turn.started",
        status="running",
        summary="本轮已开始",
        payload={"text_preview": "replay"},
    )
    await append_chat_event(
        workspace_dir=workspace_dir,
        agent_id="default",
        chat_id="chat-replay",
        session_id="console:replay",
        run_id="run-replay",
        kind="chat.replay.started",
        status="running",
        summary="开始回放",
        payload={"source_checkpoint_id": "before-fix"},
    )
    await append_chat_event(
        workspace_dir=workspace_dir,
        agent_id="default",
        chat_id="chat-replay",
        session_id="console:replay",
        run_id="run-replay",
        kind="chat.replay.settled",
        status="completed",
        summary="回放结束",
        payload={
            "source_checkpoint_id": "before-fix",
            "replay_from_seq": 3,
            "replay_to_seq": 8,
        },
    )
    await append_chat_event(
        workspace_dir=workspace_dir,
        agent_id="default",
        chat_id="chat-replay",
        session_id="console:replay",
        run_id="run-replay",
        kind="chat.turn.settled",
        status="finished",
        summary="本轮已结束",
        payload={
            "final_task_status": "finished",
            "pending_turn_cleared": True,
            "active_run_event_count": 4,
        },
    )

    response = build_runtime_events_response(workspace_dir, "chat-replay")
    assert [event.type for event in response.events] == [
        "turn.started",
        "runtime.warning",
        "item.completed",
        "turn.completed",
    ]


def test_workspace_meta_resolver_rejects_paths_outside_agent_workspace(tmp_path):
    agent_workspace = tmp_path / "agent"
    agent_workspace.mkdir()
    attacker_workspace = tmp_path / "other-project"
    attacker_workspace.mkdir()

    resolved = resolve_effective_workspace_dir_from_meta(
        {"manual_workspace_dir": str(attacker_workspace)},
        root_workspace_dir=agent_workspace,
    )

    assert resolved is None


def test_workspace_meta_resolver_accepts_topic_workspace_under_agent_root(tmp_path):
    agent_workspace = tmp_path / "agent"
    topic_workspace = agent_workspace / "topic-workspaces" / "chat-1"
    topic_workspace.mkdir(parents=True)

    resolved = resolve_effective_workspace_dir_from_meta(
        {"topic_workspace_dir": str(topic_workspace)},
        root_workspace_dir=agent_workspace,
    )

    assert resolved == topic_workspace.resolve()
