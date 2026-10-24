from types import SimpleNamespace

import pytest

from lantai.agents.memory.proactive import proactive_responder
from lantai.agents.memory.proactive.proactive_responder import (
    _execute_query,
    _extract_tasks_from_memory,
    _initialize_single_proactive_agent,
    _model_blocks_from_content,
)
from lantai.agents.persona.intervention_router import InterventionCandidate
from lantai.agents.persona.opportunity_queue import OpportunityStore
from lantai.app.beta_access.admission import BetaAccessAdmissionGuard
from lantai.runtime.models import ModelMessage, ModelResponse, ModelStreamEvent
from lantai.runtime.tools import LantaiToolRegistry, ToolCall, ToolSpec
from lantai.security.beta_access import AccessSnapshot, BetaAccessStatus


class _FakeProactiveAssistant:
    def __init__(self, text: str) -> None:
        self.text = text
        self.prompts: list[str] = []

    async def reply_text(self, prompt):
        self.prompts.append(prompt)
        return self.text

    async def run_tool_query(self, prompt: str) -> str:
        self.prompts.append(prompt)
        return self.text


@pytest.mark.asyncio
async def test_extract_tasks_uses_native_reply_text() -> None:
    assistant = _FakeProactiveAssistant(
        '{"tasks":[{"task":"check docs","query":"read README","why":"recent"}]}',
    )

    tasks = await _extract_tasks_from_memory("recent context", assistant)

    assert len(tasks) == 1
    assert tasks[0].task == "check docs"
    assert tasks[0].query == "read README"
    assert assistant.prompts


@pytest.mark.asyncio
async def test_execute_query_uses_native_runtime_query() -> None:
    assistant = _FakeProactiveAssistant("found the answer\n[SUCCESS]")

    result = await _execute_query("what changed?", assistant)

    assert result.success is True
    assert result.data == "found the answer\n[SUCCESS]"


def test_model_blocks_from_multimodal_content() -> None:
    blocks = _model_blocks_from_content(
        [
            {"type": "text", "text": "look"},
            {"type": "image", "source": {"type": "url", "url": "/tmp/a.png"}},
        ],
    )

    assert [block.type for block in blocks] == ["text", "image"]
    assert blocks[1].data["source"]["url"] == "/tmp/a.png"


@pytest.mark.asyncio
async def test_initialize_proactive_assistant_registers_native_tools(
    monkeypatch,
) -> None:
    monkeypatch.setattr(
        proactive_responder,
        "load_agent_config",
        lambda agent_id: SimpleNamespace(
            id=agent_id,
            running=SimpleNamespace(max_iters=5),
        ),
    )
    monkeypatch.setattr(
        "lantai.agents.prompt.get_active_model_supports_multimodal",
        lambda: False,
    )

    assistant = await _initialize_single_proactive_agent("agent-a")

    names = {
        spec.name for spec, _executor in assistant.tool_registry.tool_items()
    }
    assert assistant.agent_id == "agent-a"
    assert assistant.max_iters == 50
    assert {"browser_use", "read_file", "execute_shell_command"}.issubset(
        names
    )
    assert "desktop_screenshot" not in names


@pytest.mark.asyncio
async def test_proactive_native_runtime_receives_protected_write_admission(
    monkeypatch,
) -> None:
    protected_write_admission = lambda _capability: "blocked"
    background_wake_admission = lambda: None
    captured = {}

    monkeypatch.setattr(
        proactive_responder,
        "load_agent_config",
        lambda agent_id: SimpleNamespace(
            id=agent_id,
            running=SimpleNamespace(max_iters=5),
        ),
    )
    monkeypatch.setattr(
        "lantai.agents.prompt.get_active_model_supports_multimodal",
        lambda: False,
    )

    class _Runtime:
        def __init__(self, **kwargs):
            captured.update(kwargs)

        async def run_turn(self, _runtime_input):
            if False:
                yield None  # pragma: no cover

    monkeypatch.setattr(proactive_responder, "NativeLantaiRuntime", _Runtime)
    assistant = await _initialize_single_proactive_agent(
        "agent-a",
        protected_write_admission=protected_write_admission,
        background_wake_admission=background_wake_admission,
    )

    await assistant.run_tool_query("write something")

    assert captured["protected_write_admission"] is protected_write_admission
    assert captured["model_request_admission"] is background_wake_admission


@pytest.mark.asyncio
async def test_proactive_responder_stays_silent_without_an_eligible_opportunity(
    tmp_path,
    monkeypatch,
) -> None:
    monkeypatch.setattr(
        "lantai.app.agent_context.get_current_agent_id",
        lambda: "agent-a",
    )
    monkeypatch.setattr(
        proactive_responder,
        "load_agent_config",
        lambda _agent_id: SimpleNamespace(workspace_dir=str(tmp_path)),
    )

    async def must_not_initialize(_agent_id):
        raise AssertionError(
            "an idle timer must not manufacture an opportunity"
        )

    monkeypatch.setattr(
        proactive_responder,
        "_initialize_single_proactive_agent",
        must_not_initialize,
    )

    assert (
        await proactive_responder.generate_proactive_response(object()) is None
    )


@pytest.mark.asyncio
async def test_proactive_responder_consumes_queue_and_marks_it_presented(
    tmp_path,
    monkeypatch,
) -> None:
    store = OpportunityStore(tmp_path)
    item = store.enqueue_candidate(
        InterventionCandidate(
            kind="future_follow_up",
            content="回来收尾人格化的真实模型测评",
            relevance=0.9,
            urgency=0.2,
            expected_utility=0.9,
            confidence=0.9,
            relationship_resonance=0.7,
            novelty=0.8,
            intrusion_cost=0.35,
        ),
        source_ids=("task:persona-eval",),
    )
    assert item is not None
    monkeypatch.setattr(
        "lantai.app.agent_context.get_current_agent_id",
        lambda: "agent-a",
    )
    monkeypatch.setattr(
        proactive_responder,
        "load_agent_config",
        lambda _agent_id: SimpleNamespace(workspace_dir=str(tmp_path)),
    )

    async def fake_initialize(_agent_id, **_kwargs):
        return object()

    async def never_interrupted(*_args, **_kwargs):
        return False

    async def fake_execute(query, _agent):
        return proactive_responder.ProactiveQueryResult(
            query=query,
            success=True,
            data="done [SUCCESS]",
        )

    async def fake_final(_result, _agent_id):
        return "sent"

    monkeypatch.setattr(
        proactive_responder,
        "_initialize_single_proactive_agent",
        fake_initialize,
    )
    monkeypatch.setattr(
        proactive_responder, "_was_interrupted", never_interrupted
    )
    monkeypatch.setattr(proactive_responder, "_execute_query", fake_execute)
    monkeypatch.setattr(
        proactive_responder, "_generate_final_message", fake_final
    )

    assert (
        await proactive_responder.generate_proactive_response(object())
        == "sent"
    )
    persisted = OpportunityStore(tmp_path).list_all()[0]
    assert persisted.presented_hits == 1


@pytest.mark.parametrize(
    "status",
    [BetaAccessStatus.EXPIRED, BetaAccessStatus.REVOKED],
)
@pytest.mark.asyncio
async def test_proactive_responder_blocks_background_model_when_access_denied(
    tmp_path,
    monkeypatch,
    status: BetaAccessStatus,
) -> None:
    store = OpportunityStore(tmp_path)
    item = store.enqueue_candidate(
        InterventionCandidate(
            kind="future_follow_up",
            content="continue the queued task",
            relevance=0.9,
            urgency=0.2,
            expected_utility=0.9,
            confidence=0.9,
            relationship_resonance=0.7,
            novelty=0.8,
            intrusion_cost=0.35,
        ),
        source_ids=("task:blocked",),
    )
    assert item is not None
    monkeypatch.setattr(
        "lantai.app.agent_context.get_current_agent_id",
        lambda: "agent-a",
    )
    monkeypatch.setattr(
        proactive_responder,
        "load_agent_config",
        lambda _agent_id: SimpleNamespace(workspace_dir=str(tmp_path)),
    )

    async def must_not_initialize(*_args, **_kwargs):
        raise AssertionError(
            "denied proactive wake must not initialize a model"
        )

    monkeypatch.setattr(
        proactive_responder,
        "_initialize_single_proactive_agent",
        must_not_initialize,
    )
    guard = BetaAccessAdmissionGuard(
        lambda: AccessSnapshot(
            True,
            status,
            "account-1",
            4,
            9,
        ),
    )
    workspace = SimpleNamespace(beta_access_admission_guard=guard)

    assert (
        await proactive_responder.generate_proactive_response(workspace)
        is None
    )
    persisted = OpportunityStore(tmp_path).list_all()[0]
    assert persisted.presented_hits == 0


@pytest.mark.asyncio
async def test_proactive_revocation_after_tool_blocks_next_model_round(
    tmp_path,
    monkeypatch,
) -> None:
    store = OpportunityStore(tmp_path)
    item = store.enqueue_candidate(
        InterventionCandidate(
            kind="future_follow_up",
            content="read once, then stop if beta access is revoked",
            relevance=0.9,
            urgency=0.2,
            expected_utility=0.9,
            confidence=0.9,
            relationship_resonance=0.7,
            novelty=0.8,
            intrusion_cost=0.35,
        ),
        source_ids=("task:revoked-after-tool",),
    )
    assert item is not None

    class _Guard:
        allowed = True

        def decide(self, **_kwargs):
            return SimpleNamespace(
                allowed=self.allowed,
                reason="beta_access_revoked:resume_or_wake",
            )

    guard = _Guard()
    workspace = SimpleNamespace(beta_access_admission_guard=guard)
    model_calls = 0
    external_write_calls = 0
    delivery_calls = 0

    class _Client:
        provider_id = "test"
        model_name = "test-model"

        async def stream(self, _request):
            nonlocal model_calls
            model_calls += 1
            if model_calls > 1:
                raise AssertionError(
                    "revocation must stop before the next provider request"
                )
            yield ModelStreamEvent(
                kind="tool_call_completed",
                message_id="msg-read",
                tool_call=ToolCall(
                    id="call-read",
                    name="read_then_revoke",
                ),
            )
            yield ModelStreamEvent(
                kind="message_completed",
                message_id="msg-read",
                response=ModelResponse(
                    message=ModelMessage(id="msg-read", role="assistant"),
                ),
            )

    async def read_then_revoke() -> str:
        guard.allowed = False
        return "read complete"

    async def external_write() -> str:
        nonlocal external_write_calls
        external_write_calls += 1
        return "written"

    registry = LantaiToolRegistry()
    registry.register_tool(
        ToolSpec(name="read_then_revoke", read_only=True),
        read_then_revoke,
    )
    registry.register_tool(
        ToolSpec(name="external_write", read_only=False),
        external_write,
    )
    assistant = proactive_responder._ProactiveAssistant(
        agent_id="agent-a",
        max_iters=5,
        tool_registry=registry,
        workspace_dir=str(tmp_path),
        protected_write_admission=lambda _capability: (
            None if guard.allowed else "beta_access_revoked:protected_write"
        ),
        background_wake_admission=lambda: (
            None if guard.allowed else "beta_access_revoked:resume_or_wake"
        ),
    )

    monkeypatch.setattr(
        "lantai.app.agent_context.get_current_agent_id",
        lambda: "agent-a",
    )
    monkeypatch.setattr(
        proactive_responder,
        "load_agent_config",
        lambda _agent_id: SimpleNamespace(workspace_dir=str(tmp_path)),
    )
    monkeypatch.setattr(
        "lantai.agents.model_factory.create_model_client",
        lambda _agent_id: _Client(),
    )

    async def fake_initialize(_agent_id, **_kwargs):
        return assistant

    async def never_interrupted(*_args, **_kwargs):
        return False

    async def must_not_deliver(*_args, **_kwargs):
        nonlocal delivery_calls
        delivery_calls += 1
        return "must not be delivered"

    monkeypatch.setattr(
        proactive_responder,
        "_initialize_single_proactive_agent",
        fake_initialize,
    )
    monkeypatch.setattr(
        proactive_responder,
        "_was_interrupted",
        never_interrupted,
    )
    monkeypatch.setattr(
        proactive_responder,
        "_generate_final_message",
        must_not_deliver,
    )

    assert (
        await proactive_responder.generate_proactive_response(workspace)
        is None
    )
    assert model_calls == 1
    assert external_write_calls == 0
    assert delivery_calls == 0
    assert OpportunityStore(tmp_path).list_all()[0].presented_hits == 0


@pytest.mark.asyncio
async def test_proactive_responder_rechecks_access_before_delivery(
    tmp_path,
    monkeypatch,
) -> None:
    store = OpportunityStore(tmp_path)
    item = store.enqueue_candidate(
        InterventionCandidate(
            kind="future_follow_up",
            content="continue only while the license is valid",
            relevance=0.9,
            urgency=0.2,
            expected_utility=0.9,
            confidence=0.9,
            relationship_resonance=0.7,
            novelty=0.8,
            intrusion_cost=0.35,
        ),
        source_ids=("task:revoked-during-response",),
    )
    assert item is not None
    monkeypatch.setattr(
        "lantai.app.agent_context.get_current_agent_id",
        lambda: "agent-a",
    )
    monkeypatch.setattr(
        proactive_responder,
        "load_agent_config",
        lambda _agent_id: SimpleNamespace(workspace_dir=str(tmp_path)),
    )

    class _Guard:
        allowed = True

        def decide(self, **_kwargs):
            return SimpleNamespace(
                allowed=self.allowed,
                reason="beta_access_revoked:resume_or_wake",
            )

    guard = _Guard()
    workspace = SimpleNamespace(beta_access_admission_guard=guard)

    async def fake_initialize(_agent_id, **_kwargs):
        return object()

    async def never_interrupted(*_args, **_kwargs):
        return False

    async def fake_execute(_query, _agent):
        return proactive_responder.ProactiveQueryResult(
            query="continue",
            success=True,
            data="done [SUCCESS]",
        )

    async def revoke_while_generating(_result, _agent_id):
        guard.allowed = False
        return "must not be delivered"

    monkeypatch.setattr(
        proactive_responder,
        "_initialize_single_proactive_agent",
        fake_initialize,
    )
    monkeypatch.setattr(
        proactive_responder,
        "_was_interrupted",
        never_interrupted,
    )
    monkeypatch.setattr(proactive_responder, "_execute_query", fake_execute)
    monkeypatch.setattr(
        proactive_responder,
        "_generate_final_message",
        revoke_while_generating,
    )

    assert (
        await proactive_responder.generate_proactive_response(workspace)
        is None
    )
    persisted = OpportunityStore(tmp_path).list_all()[0]
    assert persisted.presented_hits == 0
