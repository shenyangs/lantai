# -*- coding: utf-8 -*-
from __future__ import annotations

import json
from datetime import timedelta, timezone
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from lantai.app.capabilities.api import task_runs_router
from lantai.app.capabilities.collaboration_distillation import (
    DistillationBlueprint,
    _runtime_workspace_for_chat,
    build_collaboration_evidence,
    build_draft_from_blueprint,
    build_fallback_blueprint,
    redact_sensitive_text,
)
from lantai.app.capabilities.models import TaskRun, TaskStep
from lantai.app.experience.schemas import ChatExperienceEvent


def _event(
    seq: int,
    kind: str,
    run_id: str,
    *,
    payload: dict | None = None,
    source: str = "system",
) -> ChatExperienceEvent:
    return ChatExperienceEvent(
        id=f"event-{seq}",
        seq=seq,
        ts=f"2026-07-31T08:00:{seq:02d}+00:00",
        agent_id="default",
        chat_id="chat-1",
        session_id="session-1",
        run_id=run_id,
        kind=kind,
        status="completed" if kind == "chat.final_answer" else "running",
        source=source,
        summary=kind,
        payload=payload or {},
    )


def _message(
    role: str,
    text: str,
    turn_id: str,
    *,
    original_id: str,
) -> dict:
    return {
        "id": f"rendered-{original_id}",
        "type": "message",
        "role": role,
        "content": [{"type": "text", "text": text}],
        "metadata": {
            "original_id": original_id,
            "metadata": {"runtime_turn_id": turn_id},
        },
    }


def test_redact_sensitive_text_removes_credentials_and_normalizes_home_path():
    text, count = redact_sensitive_text(
        "Authorization: Bearer super-secret\n"
        "api_key = sk-proj-abcdefghijklmnop\n"
        "path=/Users/example/work/report.md"
    )

    assert "super-secret" not in text
    assert "sk-proj" not in text
    assert "~/work/report.md" in text
    assert count >= 2


def test_chat_runtime_workspace_cannot_escape_the_agent_root(tmp_path):
    agent_root = tmp_path / "agent"
    outside = tmp_path / "outside"
    agent_root.mkdir()
    outside.mkdir()
    workspace = SimpleNamespace(workspace_dir=agent_root)
    chat = SimpleNamespace(meta={"topic_workspace_dir": str(outside)})

    assert _runtime_workspace_for_chat(workspace, chat) == agent_root.resolve()


def test_evidence_stops_at_clicked_reply_and_excludes_hidden_reasoning(monkeypatch):
    monkeypatch.setattr(
        "lantai.app.capabilities.collaboration_distillation.LOCAL_TIMEZONE",
        timezone(timedelta(hours=8)),
    )
    events = [
        _event(1, "chat.turn.started", "run-1", payload={"text_preview": "先做初稿"}),
        _event(2, "chat.final_answer", "run-1", payload={"text": "初稿"}),
        _event(3, "chat.turn.started", "run-2", payload={"text_preview": "不要营销腔"}),
        _event(4, "chat.tool_call.started", "run-2", payload={"tool_name": "write_file"}),
        _event(5, "chat.final_answer", "run-2", payload={"text": "改好的交付"}),
        _event(6, "chat.turn.started", "run-3", payload={"text_preview": "后续私密话题"}),
        _event(7, "chat.final_answer", "run-3", payload={"text": "不应该被吸收"}),
    ]
    events[3].ts = "legacy-timestamp-without-a-parseable-date"
    messages = [
        _message("user", "先做初稿", "run-1", original_id="user-1"),
        _message("assistant", "初稿", "run-1", original_id="assistant-1"),
        {
            **_message(
                "assistant",
                "这是不可展示的推理",
                "run-2",
                original_id="reasoning-2",
            ),
            "type": "reasoning",
        },
        _message(
            "user",
            "不要营销腔，API_KEY=sk-proj-abcdefghijklmnop",
            "run-2",
            original_id="user-2",
        ),
        _message("assistant", "改好的交付", "run-2", original_id="assistant-2"),
        _message("user", "后续私密话题", "run-3", original_id="user-3"),
        _message("assistant", "不应该被吸收", "run-3", original_id="assistant-3"),
    ]
    messages[0]["created_at"] = "2026-07-31T16:00:00"

    evidence = build_collaboration_evidence(
        agent_id="default",
        chat_id="chat-1",
        chat_title="文案调整",
        anchor_run_id="run-2",
        events=events,
        messages=messages,
    )

    transcript = "\n".join(item.text for item in evidence.messages)
    assert evidence.turn_count == 2
    assert evidence.tool_names == ["write_file"]
    assert "改好的交付" in transcript
    assert "后续私密话题" not in transcript
    assert "不可展示的推理" not in transcript
    assert "sk-proj" not in transcript
    assert evidence.redacted_count >= 1


def test_evidence_refuses_to_invent_a_skill_when_visible_chat_is_missing():
    events = [
        _event(1, "chat.turn.started", "run-1"),
        _event(2, "chat.final_answer", "run-1"),
    ]

    with pytest.raises(ValueError, match="可见协作内容不完整"):
        build_collaboration_evidence(
            agent_id="default",
            chat_id="chat-1",
            chat_title="缺失正文的旧会话",
            anchor_run_id="run-1",
            events=events,
            messages=[],
        )


def test_fallback_distills_user_corrections_into_skill_without_raw_transcript():
    events = [
        _event(1, "chat.turn.started", "run-1", payload={"text_preview": "写一份方案"}),
        _event(2, "chat.final_answer", "run-1", payload={"text": "第一版"}),
        _event(3, "chat.turn.started", "run-2", payload={"text_preview": "不要空话，补上验收标准"}),
        _event(4, "chat.tool_call.started", "run-2", payload={"tool_name": "write_file"}),
        _event(5, "chat.artifact.created", "run-2", payload={"path": "/tmp/plan.docx"}),
        _event(6, "chat.final_answer", "run-2", payload={"text": "已交付可验收方案"}),
    ]
    messages = [
        _message("user", "写一份方案", "run-1", original_id="user-1"),
        _message("assistant", "第一版", "run-1", original_id="assistant-1"),
        _message(
            "user",
            "不要空话，补上验收标准",
            "run-2",
            original_id="user-2",
        ),
        _message(
            "assistant",
            "已交付可验收方案",
            "run-2",
            original_id="assistant-2",
        ),
    ]
    evidence = build_collaboration_evidence(
        agent_id="default",
        chat_id="chat-1",
        chat_title="方案优化",
        anchor_run_id="run-2",
        events=events,
        messages=messages,
    )
    blueprint = build_fallback_blueprint(evidence)
    source_run = TaskRun(
        id="run-2",
        agent_id="default",
        source="chat",
        chat_id="chat-1",
        title="方案优化",
        user_intent="不要空话，补上验收标准",
        status="succeeded",
        steps=[
            TaskStep(
                index=0,
                kind="message",
                title="最终回复",
                detail="已交付可验收方案",
                status="succeeded",
            )
        ],
    )

    draft = build_draft_from_blueprint(
        evidence=evidence,
        blueprint=blueprint,
        source_run=source_run,
        used_utility_model=False,
    )

    assert draft.primary_asset == "both"
    assert draft.scope is not None
    assert draft.scope.anchor_run_id == "run-2"
    assert draft.scope.message_count == 4
    assert len(draft.suggested_skills) == 1
    skill = draft.suggested_skills[0]
    assert "用户纠正" in skill.content
    assert "不要空话" in skill.content
    assert "## Original request" not in skill.content
    assert "后续私密话题" not in skill.content
    assert any("本地规则" in warning for warning in draft.warnings)


@pytest.mark.asyncio
async def test_stream_api_emits_visible_stages_and_reviewable_result(
    tmp_path,
    monkeypatch,
):
    topic_workspace = tmp_path / "topic-workspace"
    topic_workspace.mkdir()
    events = [
        _event(1, "chat.turn.started", "run-1", payload={"text_preview": "写方案"}),
        _event(2, "chat.final_answer", "run-1", payload={"text": "已交付"}),
    ]
    messages = [
        _message("user", "写方案", "run-1", original_id="user-1"),
        _message("assistant", "已交付", "run-1", original_id="assistant-1"),
    ]

    class FakeChatManager:
        async def get_chat(self, chat_id):
            assert chat_id == "chat-1"
            return SimpleNamespace(
                id="chat-1",
                session_id="session-1",
                user_id="default",
                channel="console",
                name="方案会话",
                meta={"topic_workspace_dir": str(topic_workspace)},
            )

    class FakeSession:
        async def get_session_state_dict(self, *_args):
            return {"agent": {"memory": {}}}

    workspace = SimpleNamespace(
        workspace_dir=tmp_path,
        agent_id="default",
        chat_manager=FakeChatManager(),
        runner=SimpleNamespace(session=FakeSession()),
        cron_manager=None,
    )

    async def fake_workspace(_request):
        return workspace

    async def fake_messages(_state):
        return messages

    async def fake_distill(_evidence):
        return (
            DistillationBlueprint(
                title="方案交付方法",
                summary="从需求澄清到复核交付。",
                skill_name="proposal-delivery-method",
                skill_description="复用方案交付步骤。",
                reusable_goal="交付可验收方案",
                workflow_steps=["澄清目标", "验证后交付"],
                asset_kind="skill",
            ),
            True,
            None,
        )

    monkeypatch.setattr(
        "lantai.app.capabilities.api.get_agent_for_request",
        fake_workspace,
    )
    def fake_events(path):
        assert path == topic_workspace
        return events

    monkeypatch.setattr(
        "lantai.app.capabilities.collaboration_distillation._iter_events_for_workspace",
        fake_events,
    )
    monkeypatch.setattr(
        "lantai.app.capabilities.collaboration_distillation.legacy_memory_state_to_chat_messages",
        fake_messages,
    )
    monkeypatch.setattr(
        "lantai.app.capabilities.collaboration_distillation.distill_blueprint",
        fake_distill,
    )

    app = FastAPI()
    app.include_router(task_runs_router)
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
    ) as client:
        response = await client.post(
            "/task-runs/collaboration-drafts/stream",
            json={
                "chat_id": "chat-1",
                "anchor_run_id": "run-1",
                "anchor_response_id": "response-1",
            },
        )

    assert response.status_code == 200
    frames = [
        json.loads(line[6:])
        for line in response.text.splitlines()
        if line.startswith("data: ")
    ]
    assert [event["type"] for event in frames].count("stage") >= 5
    result = next(event for event in frames if event["type"] == "result")
    assert result["draft"]["primary_asset"] == "skill"
    assert result["draft"]["scope"]["anchor_run_id"] == "run-1"
    assert result["draft"]["suggested_skills"][0]["name"] == (
        "proposal-delivery-method"
    )
