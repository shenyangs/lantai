import hashlib
import json
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

import lantai.app.runner.api as chat_api
from lantai.app.runner.api import ChatBranchRequest, branch_chat
from lantai.app.runner.conversation_identity import (
    session_state_id_from_known_chats,
)
from lantai.app.runner.models import ChatSpec
from lantai.runtime.events import RuntimeEvent
from lantai.runtime.converters import legacy_empty_plan_notebook_state


def _message(
    message_id: str,
    role: str,
    text: str,
    *,
    client_turn_id: str = "",
) -> list[object]:
    return [
        {
            "id": message_id,
            "name": role,
            "role": role,
            "content": text,
            "metadata": (
                {"client_turn_id": client_turn_id} if client_turn_id else {}
            ),
            "timestamp": "2026-07-26 12:00:00.000",
        },
        [],
    ]


class FakeManager:
    def __init__(self, source: ChatSpec) -> None:
        self.source = source
        self.created: ChatSpec | None = None
        self.deleted: list[str] = []

    async def get_chat(self, chat_id: str):
        return self.source if chat_id == self.source.id else None

    async def list_chats(self, *, user_id: str, channel: str):
        chats = [self.source]
        if self.created is not None:
            chats.append(self.created)
        return [
            chat
            for chat in chats
            if chat.user_id == user_id and chat.channel == channel
        ]

    async def ensure_chat_session_state_id(self, chat_id: str):
        chat = await self.get_chat(chat_id)
        if chat is None:
            return None
        chats = await self.list_chats(
            user_id=chat.user_id,
            channel=chat.channel,
        )
        return session_state_id_from_known_chats(chat, chats)

    async def create_chat(self, spec: ChatSpec):
        self.created = spec
        return spec

    async def delete_chats(self, chat_ids: list[str]):
        self.deleted.extend(chat_ids)
        return True

    async def merge_chat_meta(self, chat_id: str, updates: dict, **_kwargs):
        if self.created is None or self.created.id != chat_id:
            return None
        self.created.meta = {**(self.created.meta or {}), **updates}
        return self.created


class FakeSession:
    def __init__(self, state: dict) -> None:
        self.state = state
        self.saved: dict | None = None

    async def get_session_state_dict(self, *_args, **_kwargs):
        return self.state

    async def update_session_state(self, **kwargs):
        self.saved = kwargs


class FakeTracker:
    def __init__(self, status: str = "idle") -> None:
        self.status = status

    async def get_status(self, _chat_id: str) -> str:
        return self.status

    async def get_snapshot(self, _chat_id: str):
        return None


@pytest.mark.asyncio
async def test_branch_chat_clones_only_prefix_and_resets_future_state():
    source = ChatSpec(
        id="source-chat",
        name="原会话",
        session_id="console:source",
        user_id="default",
        channel="console",
        meta={"pending_turn": {"text": "future"}, "theme": "plain"},
    )
    original_content = [
        _message("user-1", "user", "第一问"),
        _message("assistant-1", "assistant", "第一答"),
        _message("user-2", "user", "第二问"),
        _message("assistant-2", "assistant", "第二答"),
    ]
    state = {
        "agent": {
            "name": "Friday",
            "memory": {
                "_compressed_summary": "包含第二轮的摘要",
                "content": original_content,
            },
            "plan_notebook": {"current_plan": {"title": "未来计划"}},
            "last_context_manifest": {"messages": 4},
        },
    }
    manager = FakeManager(source)
    session = FakeSession(state)

    result = await branch_chat(
        "source-chat",
        ChatBranchRequest(user_message_id="user-2"),
        mgr=manager,
        session=session,
        workspace=SimpleNamespace(task_tracker=FakeTracker()),
    )

    assert result.chat_id == manager.created.id
    assert result.session_id.startswith("console:branch:")
    assert [message.role for message in result.messages] == ["user", "assistant"]
    assert result.meta["branched_from_chat_id"] == "source-chat"
    assert result.meta["session_state_id"] == f"console:chat:{result.chat_id}"
    assert result.meta["session_state_id"] != source.session_id
    assert "pending_turn" not in result.meta

    saved_agent = session.saved["value"]
    assert saved_agent["memory"]["content"] == original_content[:2]
    assert saved_agent["memory"]["_compressed_summary"] == ""
    assert saved_agent["plan_notebook"] == legacy_empty_plan_notebook_state()
    assert saved_agent["last_context_manifest"] is None
    assert state["agent"]["memory"]["content"] == original_content
    assert state["agent"]["memory"]["_compressed_summary"] == "包含第二轮的摘要"


@pytest.mark.asyncio
async def test_branch_chat_copies_only_visible_completed_ququ_with_provenance():
    def digest(text: str) -> str:
        payload = json.dumps((text,), ensure_ascii=False, separators=(",", ":"))
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()

    def continuation(
        source_message_id: str,
        continuation_id: str,
        source_text: str,
    ) -> dict:
        return {
            "continuation_id": continuation_id,
            "request_id": f"request:{continuation_id}",
            "agent_id": "default",
            "workspace_scope_id": "workspace:one",
            "chat_id": "source-chat",
            "session_id": "console:source",
            "user_id": "default",
            "channel": "console",
            "source_message_id": source_message_id,
            "source_message_digest": digest(source_text),
            "source_ququ_id": f"ququ:{source_message_id}",
            "ququ_ordinal": 0,
            "placement": "echo",
            "text": f"续念:{source_message_id}",
            "status": "completed",
            "created_at": "2026-09-20T01:00:00Z",
            "updated_at": "2026-09-20T01:00:01Z",
        }

    first_text = "第一答\n\n~> [echo] 第一念"
    future_text = "第二答\n\n~> [echo] 第二念"
    first = continuation("assistant-1", "continuation:first", first_text)
    future = continuation("assistant-2", "continuation:future", future_text)
    malformed = {
        **first,
        "continuation_id": "continuation:malformed",
        "request_id": "request:malformed",
        "source_ququ_id": "ququ:malformed",
        "ququ_ordinal": "not-an-ordinal",
    }
    source = ChatSpec(
        id="source-chat",
        name="原会话",
        session_id="console:source",
        user_id="default",
        channel="console",
        meta={"_ququ_continuations_v1": [first, future, malformed]},
    )
    state = {
        "agent": {
            "memory": {
                "_compressed_summary": "",
                "content": [
                    _message("user-1", "user", "第一问"),
                    _message("assistant-1", "assistant", first_text),
                    _message("user-2", "user", "第二问"),
                    _message("assistant-2", "assistant", future_text),
                ],
            }
        }
    }
    manager = FakeManager(source)
    session = FakeSession(state)

    result = await branch_chat(
        source.id,
        ChatBranchRequest(user_message_id="user-2"),
        mgr=manager,
        session=session,
        workspace=SimpleNamespace(task_tracker=FakeTracker()),
    )

    assert "_ququ_continuations_v1" not in result.meta
    assert len(result.ququ_continuations) == 1
    copied = result.ququ_continuations[0]
    assert copied["text"] == first["text"]
    assert copied["source_continuation_id"] == first["continuation_id"]
    assert copied["chat_id"] == result.chat_id
    assert copied["session_id"] == result.session_id
    assert copied["continuation_id"] != first["continuation_id"]
    assert copied["source_ququ_id"] != first["source_ququ_id"]
    identity_payload = json.dumps(
        (
            "workspace:one",
            result.chat_id,
            "assistant-1",
            digest(first_text),
            "0",
            "echo",
            "第一念",
        ),
        ensure_ascii=False,
        separators=(",", ":"),
    )
    expected_source_ququ_id = (
        "ququ:"
        + hashlib.sha256(identity_payload.encode("utf-8")).hexdigest()[:32]
    )
    assert copied["source_ququ_id"] == expected_source_ququ_id


@pytest.mark.asyncio
async def test_branch_chat_inherits_only_parent_runtime_prefix_for_display(
    monkeypatch,
    tmp_path,
):
    source = ChatSpec(
        id="source-chat",
        name="原会话",
        session_id="console:source",
        user_id="default",
        channel="console",
        meta={"topic_workspace_dir": str(tmp_path)},
    )
    original_content = [
        _message("user-1", "user", "第一问"),
        _message(
            "assistant-1",
            "assistant",
            "第一答",
            client_turn_id="turn-1",
        ),
        _message("user-2", "user", "第二问"),
        _message(
            "assistant-2",
            "assistant",
            "第二答",
            client_turn_id="turn-2",
        ),
    ]
    state = {
        "agent": {
            "memory": {"_compressed_summary": "", "content": original_content},
        },
    }
    events = [
        RuntimeEvent(
            id="tool-start",
            seq=7,
            type="tool.call.started",
            status="running",
            source="tool",
            item_id="call-1",
            payload={
                "id": "call-1",
                "name": "read_file",
                "client_turn_id": "turn-1",
            },
            created_at="2026-07-26T12:00:01Z",
        ),
        RuntimeEvent(
            id="tool-complete",
            seq=8,
            type="tool.call.completed",
            status="completed",
            source="tool",
            item_id="call-1",
            payload={
                "id": "call-1",
                "name": "read_file",
                "client_turn_id": "turn-1",
                "output": "ok",
            },
            created_at="2026-07-26T12:00:02Z",
        ),
        RuntimeEvent(
            id="target-turn",
            seq=10,
            type="turn.started",
            status="running",
            source="user",
            payload={"task_contract": {"client_turn_id": "turn-2"}},
            created_at="2026-07-26T12:01:00Z",
        ),
    ]

    def fake_runtime_events(_workspace_dir, chat_id, **_kwargs):
        return SimpleNamespace(events=events if chat_id == source.id else [])

    monkeypatch.setattr(chat_api, "build_runtime_events_response", fake_runtime_events)
    manager = FakeManager(source)
    session = FakeSession(state)

    result = await branch_chat(
        "source-chat",
        ChatBranchRequest(user_message_id="user-2"),
        mgr=manager,
        session=session,
        workspace=SimpleNamespace(
            task_tracker=FakeTracker(),
            workspace_dir=tmp_path,
        ),
    )

    assert [message.type for message in result.messages] == [
        "message",
        "plugin_call",
        "plugin_call_output",
        "message",
    ]
    assert result.meta["_branch_runtime_event_lineage_v1"] == [
        {"chat_id": "source-chat", "max_seq": 9},
    ]
    assert session.saved["value"]["memory"]["content"] == original_content[:2]
    assert all(
        row[0].get("type") not in {"plugin_call", "plugin_call_output"}
        for row in session.saved["value"]["memory"]["content"]
    )
    reloaded_events = await chat_api._load_persisted_runtime_control_events(
        manager.created,
        root_workspace_dir=tmp_path,
    )
    assert [event.id for event in reloaded_events] == [
        "tool-start",
        "tool-complete",
    ]


@pytest.mark.asyncio
async def test_branch_chat_rejects_running_source():
    source = ChatSpec(
        id="source-chat",
        name="原会话",
        session_id="console:source",
        user_id="default",
        channel="console",
    )

    with pytest.raises(HTTPException) as error:
        await branch_chat(
            "source-chat",
            ChatBranchRequest(user_message_id="user-1"),
            mgr=FakeManager(source),
            session=FakeSession({}),
            workspace=SimpleNamespace(task_tracker=FakeTracker("running")),
        )

    assert error.value.status_code == 409


@pytest.mark.asyncio
async def test_branch_chat_rejects_non_user_target():
    source = ChatSpec(
        id="source-chat",
        name="原会话",
        session_id="console:source",
        user_id="default",
        channel="console",
    )
    state = {
        "agent": {
            "memory": {
                "_compressed_summary": "",
                "content": [_message("assistant-1", "assistant", "回答")],
            },
        },
    }

    with pytest.raises(HTTPException) as error:
        await branch_chat(
            "source-chat",
            ChatBranchRequest(user_message_id="assistant-1"),
            mgr=FakeManager(source),
            session=FakeSession(state),
            workspace=SimpleNamespace(task_tracker=FakeTracker()),
        )

    assert error.value.status_code == 400
