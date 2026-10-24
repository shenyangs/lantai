from __future__ import annotations

import asyncio
import json

import pytest

import lantai.app.post_answer_reflection_service as reflection_service_module
from lantai.agents.persona.post_answer_reflection import (
    PostAnswerReflectionRequest,
)
from lantai.app.experience.events import append_chat_event, collect_workspace_events
from lantai.app.post_answer_reflection_service import (
    PostAnswerReflectionService,
)
from lantai.app.utility_model import ResolvedUtilityModel


class _TextResponse:
    def __init__(self, text: str) -> None:
        self.text = text


def test_reflection_model_prefers_dedicated_then_falls_back_to_active(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    dedicated = ResolvedUtilityModel(
        model=object(),
        provider_id="fast",
        model_id="helper",
    )
    active = ResolvedUtilityModel(
        model=object(),
        provider_id="primary",
        model_id="chat",
    )
    config = type("Config", (), {"mode": "dedicated"})()
    monkeypatch.setattr(
        reflection_service_module,
        "load_config",
        lambda: type("Root", (), {"utility_intelligence": config})(),
    )
    monkeypatch.setattr(
        reflection_service_module,
        "resolve_utility_model",
        lambda _config, _agent_id: dedicated,
    )
    monkeypatch.setattr(
        reflection_service_module,
        "resolve_active_model",
        lambda _agent_id: active,
    )
    assert reflection_service_module.resolve_reflection_model("default") is dedicated

    monkeypatch.setattr(
        reflection_service_module,
        "resolve_utility_model",
        lambda _config, _agent_id: None,
    )
    assert reflection_service_module.resolve_reflection_model("default") is active


def _request(*, session_id: str = "session-a") -> PostAnswerReflectionRequest:
    return PostAnswerReflectionRequest(
        agent_id="default",
        workspace_dir="",
        chat_id="chat-a",
        session_id=session_id,
        run_id="run-a",
        client_turn_id="client-turn-a",
        answer_message_id="answer-a",
        user_message="我今天有点累。",
        final_answer="那今天就别再硬撑了。",
        plan={
            "causal_anchor": "用户说累",
            "selected_memory_ids": ["memory-1"],
            "experience_sources": ["conversation"],
        },
        subject_context={"attention": "他把累说得很轻"},
        need_persona=True,
        need_ququ=True,
        assistant_history=["上一轮正文。"],
    )


def test_background_off_rejects_reflection_before_persisting(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = type(
        "Config",
        (),
        {"mode": "follow_active", "background_processing_enabled": False},
    )()
    monkeypatch.setattr(
        reflection_service_module,
        "load_config",
        lambda: type("Root", (), {"utility_intelligence": config})(),
    )
    service = PostAnswerReflectionService(tmp_path, agent_id="default")

    assert service.schedule(_request()) is None
    assert service.jobs_dir.exists() is False


@pytest.mark.asyncio
async def test_sidecar_uses_dedicated_model_and_delivers_after_main_turn(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    release = asyncio.Event()
    calls: list[list[dict[str, str]]] = []

    async def model(messages):
        calls.append(messages)
        await release.wait()
        return _TextResponse(
            json.dumps(
                {
                    "persona_line": "",
                    "ququ_line": "~>[echo] 你把累说得这么轻，我反而更不放心。",
                    "attention": "他把疲惫说得很轻。",
                    "self_observation": "我没有被这句轻描淡写糊弄过去。",
                    "unresolved_thought": "今天到底发生了什么。",
                    "relationship_stance": "concerned",
                    "inner_world_note": "想替他把速度压下来。",
                    "carry_forward": "留意这份疲惫。",
                },
                ensure_ascii=False,
            )
        )

    monkeypatch.setattr(
        "lantai.app.post_answer_reflection_service.resolve_reflection_model",
        lambda _agent_id: ResolvedUtilityModel(
            model=model,
            provider_id="fast-provider",
            model_id="fast-model",
        ),
    )
    persisted = []
    request = _request().with_workspace_dir(str(tmp_path))
    service = PostAnswerReflectionService(tmp_path, agent_id="default")

    task = service.schedule(request, persist_message=persisted.append)
    assert task is not None
    await asyncio.sleep(0)
    assert not task.done()
    assert collect_workspace_events(tmp_path) == []

    release.set()
    await task

    assert len(calls) == 1
    assert request.user_message in calls[0][1]["content"]
    assert request.final_answer in calls[0][1]["content"]
    events = collect_workspace_events(tmp_path)
    assert len(events) == 1
    event = events[0]
    assert event.kind == "chat.message.completed"
    assert event.run_id != request.run_id
    assert event.session_id == request.session_id
    assert event.payload["client_turn_id"] == request.client_turn_id
    assert event.payload["answer_message_id"] == request.answer_message_id
    assert event.payload["details"]["reflection_model"] == ("fast-provider/fast-model")
    assert persisted[0].metadata["client_turn_id"] == request.client_turn_id


@pytest.mark.asyncio
async def test_sidecar_event_is_ordered_after_the_original_terminal_event(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def model(_messages):
        return _TextResponse(
            '{"ququ_line":"~>[echo] 正文结束了，我这句才慢半拍冒出来。"}'
        )

    monkeypatch.setattr(
        "lantai.app.post_answer_reflection_service.resolve_reflection_model",
        lambda _agent_id: ResolvedUtilityModel(
            model=model,
            provider_id="fast",
            model_id="ordered",
        ),
    )
    request = _request().with_workspace_dir(str(tmp_path))
    await append_chat_event(
        workspace_dir=tmp_path,
        agent_id=request.agent_id,
        chat_id=request.chat_id,
        session_id=request.session_id,
        run_id=request.run_id,
        kind="chat.turn.settled",
        type="turn.completed",
        status="completed",
        source="system",
        summary="正文终态",
        payload={"final_task_status": "finished"},
    )

    task = PostAnswerReflectionService(
        tmp_path,
        agent_id="default",
    ).schedule(request)
    assert task is not None
    await task

    events = collect_workspace_events(tmp_path)
    assert [event.type for event in events] == [
        "turn.completed",
        "message.completed",
    ]
    assert events[1].seq > events[0].seq
    assert events[1].payload["details"]["original_run_id"] == request.run_id


@pytest.mark.asyncio
async def test_sidecar_is_idempotent_and_session_scoped(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = 0

    async def model(_messages):
        nonlocal calls
        calls += 1
        return _TextResponse('{"ququ_line":"~>[echo] 这一句只属于这一轮。"}')

    monkeypatch.setattr(
        "lantai.app.post_answer_reflection_service.resolve_reflection_model",
        lambda _agent_id: ResolvedUtilityModel(
            model=model,
            provider_id="fast",
            model_id="one",
        ),
    )
    service = PostAnswerReflectionService(tmp_path, agent_id="default")
    request_a = _request().with_workspace_dir(str(tmp_path))
    first = service.schedule(request_a)
    second = service.schedule(request_a)
    assert first is second
    assert first is not None
    await first
    assert service.schedule(request_a) is None

    request_b = _request(session_id="session-b").with_workspace_dir(str(tmp_path))
    third = service.schedule(request_b)
    assert third is not None
    await third

    assert calls == 2
    events = collect_workspace_events(tmp_path)
    assert [event.session_id for event in events] == ["session-a", "session-b"]
    assert (
        events[0].payload["details"]["job_id"] != events[1].payload["details"]["job_id"]
    )


@pytest.mark.asyncio
async def test_failed_sidecar_remains_recoverable_without_local_fallback(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    attempts = 0

    async def model(_messages):
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise RuntimeError("temporary provider failure")
        return _TextResponse('{"ququ_line":"~>[echo] 这次终于把真正想说的送到了。"}')

    monkeypatch.setattr(
        "lantai.app.post_answer_reflection_service.resolve_reflection_model",
        lambda _agent_id: ResolvedUtilityModel(
            model=model,
            provider_id="fast",
            model_id="retry",
        ),
    )
    request = _request().with_workspace_dir(str(tmp_path))
    first_service = PostAnswerReflectionService(tmp_path, agent_id="default")
    first = first_service.schedule(request)
    assert first is not None
    await first
    assert collect_workspace_events(tmp_path) == []

    recovered_service = PostAnswerReflectionService(
        tmp_path,
        agent_id="default",
    )
    recovered = recovered_service.recover_pending()
    assert len(recovered) == 1
    await recovered[0]

    events = collect_workspace_events(tmp_path)
    assert len(events) == 1
    assert "真正想说的" in events[0].summary
    assert attempts == 2


@pytest.mark.asyncio
async def test_recovery_reuses_the_persisted_reflection_without_regeneration(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fail_if_model_is_resolved(_agent_id):
        raise AssertionError("a persisted reflection must not be regenerated")

    monkeypatch.setattr(
        "lantai.app.post_answer_reflection_service.resolve_reflection_model",
        fail_if_model_is_resolved,
    )
    request = _request().with_workspace_dir(str(tmp_path))
    service = PostAnswerReflectionService(tmp_path, agent_id="default")
    job = service._new_job(request)
    job["status"] = "running"
    job["attempts"] = 1
    job["reflection_model"] = "fast/original"
    job["reflection"] = {
        "persona_line": "",
        "ququ_line": "~>[echo] 刚才那一点担心，没有因为进程重启变样。",
        "attention": "用户把累说得很轻。",
        "self_observation": "我仍然在意这件事。",
        "unresolved_thought": "他有没有休息。",
        "relationship_stance": "concerned",
        "inner_world_note": "想把速度压下来。",
        "carry_forward": "留意疲惫。",
    }
    service._write_job(job)

    recovered = service.recover_pending()
    assert len(recovered) == 1
    await recovered[0]

    events = collect_workspace_events(tmp_path)
    assert len(events) == 1
    assert events[0].summary == job["reflection"]["ququ_line"]
    saved = json.loads(service._job_path(request.job_id).read_text(encoding="utf-8"))
    assert saved["status"] == "completed"
    assert saved["attempts"] == 2


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("final_answer", "history", "generated"),
    [
        (
            "只有正文。",
            ["~>[echo] 这一步我还是想替你多盯一眼。"],
            "~>[echo] 这一步我还是想替你多盯一眼。",
        ),
        ("只有正文。", [], "~>[echo] 只有正文。"),
        (
            "RUNTIME_CONFIGURED_PROVIDER_OK",
            [],
            "~>[echo] 我还是想把这句留住："
            "RUNTIME_CONFIGURED_PROVIDER_OK，确实放心了一点。",
        ),
    ],
)
async def test_sidecar_rejects_ququ_that_recites_delivered_text(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
    final_answer: str,
    history: list[str],
    generated: str,
) -> None:
    async def model(_messages):
        return _TextResponse(json.dumps({"ququ_line": generated}, ensure_ascii=False))

    monkeypatch.setattr(
        "lantai.app.post_answer_reflection_service.resolve_reflection_model",
        lambda _agent_id: ResolvedUtilityModel(
            model=model,
            provider_id="fast",
            model_id="repeat-guard",
        ),
    )
    base = _request().with_workspace_dir(str(tmp_path))
    request = PostAnswerReflectionRequest(
        **{
            **base.to_dict(),
            "final_answer": final_answer,
            "assistant_history": history,
        }
    )
    service = PostAnswerReflectionService(tmp_path, agent_id="default")
    task = service.schedule(request)
    assert task is not None
    await task

    assert collect_workspace_events(tmp_path) == []
    job = json.loads(service._job_path(request.job_id).read_text(encoding="utf-8"))
    assert job["status"] == "failed"
    assert "repeated" in job["last_error"]
